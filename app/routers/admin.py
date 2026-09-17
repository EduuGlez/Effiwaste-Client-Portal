"""Administración de documentos, organizaciones y usuarios."""

import hashlib
import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.constants import DOCUMENT_CATEGORIES
from app.database import get_db
from app.models import AudienceType, Chain, Document, DocumentStatus, Hotel, User, UserRole
from app.security import hash_password, password_policy_error
from app.services.pdf_service import validate_pdf
from app.services.storage import document_path
from app.web import (
    ensure_csrf_token,
    get_user_from_session,
    redirect_with_message,
    require_admin,
    sanitize_original_filename,
    templates,
    verify_csrf,
)
from app.worker import process_document


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
settings = get_settings()


@router.get("", include_in_schema=False)
def admin_view(request: Request, db: Session = Depends(get_db)):
    """Renderiza el panel solo para administradores activos."""

    user = get_user_from_session(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != UserRole.ADMIN:
        return RedirectResponse("/assistant", status_code=303)

    documents = db.scalars(
        select(Document)
        .options(joinedload(Document.chain), joinedload(Document.hotel), joinedload(Document.uploaded_by))
        .order_by(Document.created_at.desc())
    ).all()
    chains = db.scalars(select(Chain).order_by(Chain.name)).all()
    hotels = db.scalars(select(Hotel).options(joinedload(Hotel.chain)).order_by(Hotel.name)).all()
    users = db.scalars(
        select(User).options(joinedload(User.chain), joinedload(User.hotel)).order_by(User.created_at.desc())
    ).all()
    counts = {
        "documents": db.scalar(select(func.count(Document.id))) or 0,
        "ready": db.scalar(select(func.count(Document.id)).where(Document.status == DocumentStatus.READY)) or 0,
        "users": db.scalar(select(func.count(User.id))) or 0,
        "hotels": db.scalar(select(func.count(Hotel.id))) or 0,
    }
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "user": user,
            "documents": documents,
            "chains": chains,
            "hotels": hotels,
            "users": users,
            "counts": counts,
            "csrf_token": ensure_csrf_token(request),
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("error"),
            "max_upload_mb": settings.max_upload_mb,
            "category_labels": DOCUMENT_CATEGORIES,
        },
    )


def _resolve_audience(
    db: Session, audience: str, chain_id: str | None, hotel_id: str | None
) -> tuple[AudienceType, uuid.UUID | None, uuid.UUID | None]:
    """Valida la audiencia y deriva una combinación coherente de claves foráneas."""

    try:
        audience_type = AudienceType(audience)
    except ValueError as exc:
        raise HTTPException(400, "Audiencia no válida") from exc

    if audience_type == AudienceType.GLOBAL:
        return audience_type, None, None
    if audience_type == AudienceType.CHAIN:
        try:
            selected_chain = db.get(Chain, uuid.UUID(chain_id or ""))
        except ValueError:
            selected_chain = None
        if selected_chain is None:
            raise HTTPException(400, "Selecciona una cadena válida")
        return audience_type, selected_chain.id, None

    try:
        selected_hotel = db.get(Hotel, uuid.UUID(hotel_id or ""))
    except ValueError:
        selected_hotel = None
    if selected_hotel is None:
        raise HTTPException(400, "Selecciona un hotel válido")
    return audience_type, selected_hotel.chain_id, selected_hotel.id


@router.post("/documents", include_in_schema=False)
async def upload_document(
    request: Request,
    pdf: UploadFile = File(...),
    audience: str = Form(...),
    category: str = Form("general"),
    report_month: str | None = Form(None),
    chain_id: str | None = Form(None),
    hotel_id: str | None = Form(None),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Valida y almacena un PDF antes de enviarlo al worker de ingesta."""

    admin = require_admin(request, db)
    verify_csrf(request, csrf_token)
    if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
        return redirect_with_message("/admin", error="Solo se admiten archivos PDF")

    try:
        audience_type, resolved_chain_id, resolved_hotel_id = _resolve_audience(
            db, audience, chain_id, hotel_id
        )
    except HTTPException as exc:
        return redirect_with_message("/admin", error=str(exc.detail))

    if category not in DOCUMENT_CATEGORIES:
        return redirect_with_message("/admin", error="Categoría de documento no válida")
    resolved_report_month = None
    if category == "monthly_report":
        if audience_type != AudienceType.HOTEL or resolved_hotel_id is None:
            return redirect_with_message(
                "/admin", error="Los informes mensuales deben asignarse a un hotel concreto"
            )
        try:
            resolved_report_month = date.fromisoformat(f"{report_month}-01")
        except (TypeError, ValueError):
            return redirect_with_message(
                "/admin", error="Selecciona el mes correspondiente al informe"
            )

    original_name = sanitize_original_filename(pdf.filename)
    stored_name = f"{uuid.uuid4()}.pdf"
    destination = document_path(stored_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with destination.open("xb") as output:
            while chunk := await pdf.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise ValueError(f"El PDF supera el límite de {settings.max_upload_mb} MB")
                digest.update(chunk)
                output.write(chunk)
        validate_pdf(destination)
    except (OSError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        return redirect_with_message("/admin", error=str(exc))
    except Exception:
        destination.unlink(missing_ok=True)
        logger.exception("Fallo inesperado al recibir un PDF")
        return redirect_with_message("/admin", error="No se pudo procesar el archivo recibido")
    finally:
        await pdf.close()

    document = Document(
        original_name=original_name,
        stored_name=stored_name,
        size_bytes=total,
        sha256=digest.hexdigest(),
        category=category,
        report_month=resolved_report_month,
        audience_type=audience_type,
        chain_id=resolved_chain_id,
        hotel_id=resolved_hotel_id,
        uploaded_by_id=admin.id,
    )
    db.add(document)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        destination.unlink(missing_ok=True)
        logger.exception("No se pudieron guardar los metadatos del PDF")
        return redirect_with_message("/admin", error="No se pudo registrar el documento")
    db.refresh(document)
    try:
        process_document.delay(str(document.id))
    except Exception:
        logger.exception("No se pudo encolar el documento %s", document.id)
        document.status = DocumentStatus.FAILED
        document.error_message = "No se pudo encolar la ingesta."
        db.commit()
        return redirect_with_message("/admin", error="PDF guardado, pero el worker no está disponible")
    return redirect_with_message("/admin", ok="PDF recibido; la indexación se ejecuta en segundo plano")


@router.post("/documents/{document_id}/retry", include_in_schema=False)
def retry_document(
    document_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Reencola un documento fallido y conserva el registro original."""

    require_admin(request, db)
    verify_csrf(request, csrf_token)
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Documento no encontrado")
    document.status = DocumentStatus.QUEUED
    document.error_message = None
    db.commit()
    try:
        process_document.delay(str(document.id))
    except Exception:
        logger.exception("No se pudo reencolar el documento %s", document.id)
        document.status = DocumentStatus.FAILED
        document.error_message = "No se pudo reencolar la ingesta."
        db.commit()
        return redirect_with_message("/admin", error="El worker no está disponible")
    return redirect_with_message("/admin", ok="Documento reenviado a indexación")


@router.post("/documents/{document_id}/delete", include_in_schema=False)
def delete_document(
    document_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Elimina metadatos, fragmentos y el PDF físico de un documento."""

    require_admin(request, db)
    verify_csrf(request, csrf_token)
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Documento no encontrado")
    path = document_path(document.stored_name)
    db.delete(document)
    db.commit()
    path.unlink(missing_ok=True)
    return redirect_with_message("/admin", ok="Documento eliminado")


@router.post("/chains", include_in_schema=False)
def create_chain(
    request: Request,
    name: str = Form(..., max_length=160),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Crea una cadena con nombre único."""

    require_admin(request, db)
    verify_csrf(request, csrf_token)
    clean_name = name.strip()
    if len(clean_name) < 2:
        return redirect_with_message("/admin", error="El nombre de la cadena es demasiado corto")
    db.add(Chain(name=clean_name))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect_with_message("/admin", error="Ya existe esa cadena")
    return redirect_with_message("/admin", ok="Cadena creada")


@router.post("/hotels", include_in_schema=False)
def create_hotel(
    request: Request,
    name: str = Form(..., max_length=160),
    code: str = Form(..., max_length=50),
    chain_id: uuid.UUID = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Crea un hotel y lo asocia a una cadena existente."""

    require_admin(request, db)
    verify_csrf(request, csrf_token)
    clean_name = name.strip()
    clean_code = code.strip().upper()
    if len(clean_name) < 2 or len(clean_code) < 2:
        return redirect_with_message("/admin", error="Nombre y código deben tener al menos 2 caracteres")
    if db.get(Chain, chain_id) is None:
        return redirect_with_message("/admin", error="Cadena no válida")
    db.add(Hotel(name=clean_name, code=clean_code, chain_id=chain_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect_with_message("/admin", error="El hotel o su código ya existe")
    return redirect_with_message("/admin", ok="Hotel creado")


@router.post("/users", include_in_schema=False)
def create_user(
    request: Request,
    full_name: str = Form(..., max_length=160),
    email: str = Form(..., max_length=320),
    password: str = Form(..., max_length=1024),
    hotel_id: uuid.UUID = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Crea un usuario de hotel aplicando la política de contraseña."""

    require_admin(request, db)
    verify_csrf(request, csrf_token)
    hotel = db.get(Hotel, hotel_id)
    if hotel is None:
        return redirect_with_message("/admin", error="Hotel no válido")
    password_error = password_policy_error(password)
    if password_error:
        return redirect_with_message("/admin", error=password_error)
    clean_name = full_name.strip()
    clean_email = email.strip().lower()
    if len(clean_name) < 2 or "@" not in clean_email:
        return redirect_with_message("/admin", error="Nombre o correo no válido")
    db.add(
        User(
            email=clean_email,
            full_name=clean_name,
            password_hash=hash_password(password),
            role=UserRole.USER,
            chain_id=hotel.chain_id,
            hotel_id=hotel.id,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return redirect_with_message("/admin", error="Ya existe un usuario con ese correo")
    return redirect_with_message("/admin", ok="Usuario creado")
