import hashlib
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.middleware.sessions import SessionMiddleware

from app.bootstrap import initialize_application
from app.config import get_settings
from app.database import get_db
from app.models import (
    AudienceType,
    Chain,
    Document,
    DocumentStatus,
    Hotel,
    Recommendation,
    RecommendationStatus,
    User,
    UserRole,
    utcnow,
)
from app.security import csrf_matches, hash_password, new_csrf_token, verify_password
from app.services.access import can_access_document, document_access_filter
from app.services.markdown_service import render_markdown
from app.services.rag_service import ask
from app.worker import process_document


settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
DOCUMENT_CATEGORIES = {
    "monthly_report": "Informe mensual",
    "general": "Documento general",
    "procedure": "Procedimiento",
    "manual": "Manual",
}
LIBRARY_CATEGORY_LABELS = {
    "monthly_report": "Informes mensuales",
    "procedure": "Procedimientos",
    "manual": "Manuales",
    "general": "Documentos generales",
}
EXTERNAL_SERVICES = (
    {
        "name": "Effiwaste",
        "category": "Desperdicio alimentario",
        "description": "Mide, controla y analiza el desperdicio alimentario de tu operativa.",
        "url": "https://weight.effiwaste.es/#/login",
        "style": "weight",
    },
    {
        "name": "Effilabel",
        "category": "Etiquetado electrónico",
        "description": "Gestiona la información del buffet y actualiza tus etiquetas en tiempo real.",
        "url": "https://esl.effiwaste.es/#/login",
        "style": "label",
    },
    {
        "name": "EffiChef",
        "category": "Gestión de cocina",
        "description": "Centraliza inventarios, escandallos, producción y logística de cocina.",
        "url": "https://www.effichef.es/",
        "style": "chef",
    },
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_application()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    https_only=settings.cookie_secure,
    same_site="lax",
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


def _user_from_session(request: Request, db: Session) -> User | None:
    value = request.session.get("user_id")
    if not value:
        return None
    try:
        user = db.get(User, uuid.UUID(value))
    except (ValueError, TypeError):
        return None
    return user if user and user.is_active else None


def _require_user(request: Request, db: Session) -> User:
    user = _user_from_session(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Sesión no válida")
    return user


def _require_admin(request: Request, db: Session) -> User:
    user = _require_user(request, db)
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Acceso reservado a administración")
    return user


def _verify_csrf(request: Request, supplied: str | None) -> None:
    if not csrf_matches(request.session.get("csrf"), supplied):
        raise HTTPException(status_code=403, detail="Token de seguridad no válido")


def _redirect_with_message(path: str, *, ok: str | None = None, error: str | None = None):
    if ok:
        path += f"?ok={quote(ok)}"
    elif error:
        path += f"?error={quote(error)}"
    return RedirectResponse(path, status_code=303)


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(select(1))
    return {"status": "ok"}


@app.get("/")
def root(request: Request, db: Session = Depends(get_db)):
    user = _user_from_session(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/assistant", status_code=303)


@app.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if _user_from_session(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": request.query_params.get("error"), "app_name": settings.app_name},
    )


@app.post("/auth/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return _redirect_with_message("/login", error="Credenciales incorrectas")
    request.session.clear()
    request.session["user_id"] = str(user.id)
    request.session["csrf"] = new_csrf_token()
    return RedirectResponse("/assistant", status_code=303)


@app.post("/auth/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    _verify_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/app")
def user_view(request: Request, db: Session = Depends(get_db)):
    user = _user_from_session(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/assistant", status_code=303)


@app.get("/assistant")
def assistant_view(request: Request, db: Session = Depends(get_db)):
    user = _user_from_session(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    documents = db.scalars(
        select(Document)
        .where(Document.status == DocumentStatus.READY, document_access_filter(user))
        .order_by(Document.created_at.desc())
    ).all()
    monthly_documents = [item for item in documents if item.category == "monthly_report"]
    general_documents = [item for item in documents if item.category != "monthly_report"]
    return templates.TemplateResponse(
        request=request,
        name="user.html",
        context={
            "user": user,
            "documents": documents,
            "monthly_documents": monthly_documents,
            "general_documents": general_documents,
            "category_labels": DOCUMENT_CATEGORIES,
            "csrf_token": request.session["csrf"],
        },
    )


@app.get("/library")
def document_library_view(request: Request, db: Session = Depends(get_db)):
    user = _user_from_session(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    documents = db.scalars(
        select(Document)
        .options(joinedload(Document.chain), joinedload(Document.hotel))
        .where(Document.status == DocumentStatus.READY, document_access_filter(user))
        .order_by(Document.created_at.desc())
    ).all()
    category_counts = {
        category: sum(1 for document in documents if document.category == category)
        for category in LIBRARY_CATEGORY_LABELS
    }
    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "user": user,
            "documents": documents,
            "category_labels": LIBRARY_CATEGORY_LABELS,
            "category_counts": category_counts,
            "csrf_token": request.session["csrf"],
        },
    )


@app.get("/services")
def services_view(request: Request, db: Session = Depends(get_db)):
    user = _user_from_session(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="services.html",
        context={
            "user": user,
            "services": EXTERNAL_SERVICES,
            "csrf_token": request.session["csrf"],
        },
    )


@app.get("/recommendations")
def recommendations_view(request: Request, db: Session = Depends(get_db)):
    user = _user_from_session(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    recommendations: list[Recommendation] = []
    latest_report = None
    if user.hotel_id:
        recommendations = db.scalars(
            select(Recommendation)
            .options(
                joinedload(Recommendation.source_document),
                joinedload(Recommendation.evaluation_document),
            )
            .where(Recommendation.hotel_id == user.hotel_id)
            .order_by(Recommendation.created_at.desc())
        ).all()
        latest_report = db.scalar(
            select(Document)
            .where(
                Document.hotel_id == user.hotel_id,
                Document.category == "monthly_report",
                Document.status == DocumentStatus.READY,
            )
            .order_by(Document.report_month.desc().nullslast(), Document.created_at.desc())
            .limit(1)
        )

    grouped = {
        status.value: [item for item in recommendations if item.status == status.value]
        for status in RecommendationStatus
    }
    return templates.TemplateResponse(
        request=request,
        name="recommendations.html",
        context={
            "user": user,
            "latest_report": latest_report,
            "recommendations": grouped,
            "csrf_token": request.session["csrf"],
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/recommendations/{recommendation_id}/accept")
def accept_recommendation(
    recommendation_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    _verify_csrf(request, csrf_token)
    recommendation = db.get(Recommendation, recommendation_id)
    if recommendation is None or user.hotel_id is None or recommendation.hotel_id != user.hotel_id:
        raise HTTPException(404, "Recomendación no encontrada")
    if recommendation.status != RecommendationStatus.PROPOSED.value:
        return _redirect_with_message(
            "/recommendations", error="Esta recomendación ya no está pendiente"
        )
    recommendation.status = RecommendationStatus.ACCEPTED.value
    recommendation.accepted_at = utcnow()
    db.commit()
    return _redirect_with_message(
        "/recommendations",
        ok="Mejora aceptada. Se evaluará automáticamente con el próximo informe mensual.",
    )


@app.post("/recommendations/{recommendation_id}/discard")
def discard_recommendation(
    recommendation_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _require_user(request, db)
    _verify_csrf(request, csrf_token)
    recommendation = db.get(Recommendation, recommendation_id)
    if recommendation is None or user.hotel_id is None or recommendation.hotel_id != user.hotel_id:
        raise HTTPException(404, "Recomendación no encontrada")
    if recommendation.status != RecommendationStatus.PROPOSED.value:
        return _redirect_with_message(
            "/recommendations", error="Esta recomendación ya no está pendiente"
        )
    recommendation.status = RecommendationStatus.DISCARDED.value
    recommendation.evaluation_result = "user_discarded"
    recommendation.evaluation_summary = "Descartada por el cliente porque no encajaba con su operativa."
    recommendation.evaluated_at = utcnow()
    db.commit()
    return _redirect_with_message(
        "/recommendations", ok="Propuesta descartada. La IA la tendrá en cuenta en futuros informes."
    )


@app.post("/api/chat")
def chat(payload: ChatRequest, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    _verify_csrf(request, request.headers.get("x-csrf-token"))
    question = payload.question.strip()
    try:
        result = ask(db, user, question)
        result["answer_html"] = render_markdown(result["answer"])
        return result
    except Exception as exc:
        # No exponemos claves, prompts ni trazas internas al navegador.
        message = str(exc)
        if "OPENAI_API_KEY" in message:
            detail = message
        else:
            detail = "No se pudo completar la consulta. Revisa el servicio de OpenAI y vuelve a intentarlo."
        return JSONResponse({"detail": detail}, status_code=502)


@app.get("/admin")
def admin_view(request: Request, db: Session = Depends(get_db)):
    user = _user_from_session(request, db)
    if not user:
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
            "csrf_token": request.session["csrf"],
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("error"),
            "max_upload_mb": settings.max_upload_mb,
            "category_labels": DOCUMENT_CATEGORIES,
        },
    )


def _resolve_audience(
    db: Session, audience: str, chain_id: str | None, hotel_id: str | None
) -> tuple[AudienceType, uuid.UUID | None, uuid.UUID | None]:
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


@app.post("/admin/documents")
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
    admin = _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    if not pdf.filename or not pdf.filename.lower().endswith(".pdf"):
        return _redirect_with_message("/admin", error="Solo se admiten archivos PDF")

    try:
        audience_type, resolved_chain_id, resolved_hotel_id = _resolve_audience(
            db, audience, chain_id, hotel_id
        )
    except HTTPException as exc:
        return _redirect_with_message("/admin", error=str(exc.detail))

    if category not in DOCUMENT_CATEGORIES:
        return _redirect_with_message("/admin", error="Categoría de documento no válida")
    resolved_report_month = None
    if category == "monthly_report":
        if audience_type != AudienceType.HOTEL or resolved_hotel_id is None:
            return _redirect_with_message(
                "/admin", error="Los informes mensuales deben asignarse a un hotel concreto"
            )
        try:
            resolved_report_month = date.fromisoformat(f"{report_month}-01")
        except (TypeError, ValueError):
            return _redirect_with_message(
                "/admin", error="Selecciona el mes correspondiente al informe"
            )

    safe_original_name = Path(pdf.filename).name[:255]
    stored_name = f"{uuid.uuid4()}.pdf"
    destination = settings.upload_dir / stored_name
    digest = hashlib.sha256()
    total = 0
    try:
        with destination.open("wb") as output:
            while chunk := await pdf.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise ValueError(f"El PDF supera el límite de {settings.max_upload_mb} MB")
                digest.update(chunk)
                output.write(chunk)
        from app.services.pdf_service import validate_pdf

        validate_pdf(destination)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        return _redirect_with_message("/admin", error=str(exc))
    finally:
        await pdf.close()

    document = Document(
        original_name=safe_original_name,
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
    db.commit()
    db.refresh(document)
    try:
        process_document.delay(str(document.id))
    except Exception as exc:
        document.status = DocumentStatus.FAILED
        document.error_message = f"No se pudo encolar la ingesta: {exc}"[:2000]
        db.commit()
        return _redirect_with_message("/admin", error="PDF guardado, pero el worker no está disponible")
    return _redirect_with_message("/admin", ok="PDF recibido; la indexación se ejecuta en segundo plano")


@app.post("/admin/documents/{document_id}/retry")
def retry_document(
    document_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Documento no encontrado")
    document.status = DocumentStatus.QUEUED
    document.error_message = None
    db.commit()
    process_document.delay(str(document.id))
    return _redirect_with_message("/admin", ok="Documento reenviado a indexación")


@app.post("/admin/documents/{document_id}/delete")
def delete_document(
    document_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Documento no encontrado")
    stored_name = document.stored_name
    db.delete(document)
    db.commit()
    (settings.upload_dir / stored_name).unlink(missing_ok=True)
    return _redirect_with_message("/admin", ok="Documento eliminado")


@app.get("/documents/{document_id}/download")
def download_document(document_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    document = db.get(Document, document_id)
    if document is None or not can_access_document(user, document):
        raise HTTPException(404, "Documento no encontrado")
    path = settings.upload_dir / document.stored_name
    if not path.exists():
        raise HTTPException(404, "El archivo no está disponible")
    return FileResponse(path, media_type="application/pdf", filename=document.original_name)


@app.get("/documents/{document_id}/view")
def view_document(document_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    user = _require_user(request, db)
    document = db.get(Document, document_id)
    if document is None or not can_access_document(user, document):
        raise HTTPException(404, "Documento no encontrado")
    path = settings.upload_dir / document.stored_name
    if not path.exists():
        raise HTTPException(404, "El archivo no está disponible")
    encoded_name = quote(document.original_name)
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{encoded_name}"},
    )


@app.post("/admin/chains")
def create_chain(
    request: Request,
    name: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    clean_name = name.strip()
    if len(clean_name) < 2:
        return _redirect_with_message("/admin", error="El nombre de la cadena es demasiado corto")
    db.add(Chain(name=clean_name))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _redirect_with_message("/admin", error="Ya existe esa cadena")
    return _redirect_with_message("/admin", ok="Cadena creada")


@app.post("/admin/hotels")
def create_hotel(
    request: Request,
    name: str = Form(...),
    code: str = Form(...),
    chain_id: uuid.UUID = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    if db.get(Chain, chain_id) is None:
        return _redirect_with_message("/admin", error="Cadena no válida")
    db.add(Hotel(name=name.strip(), code=code.strip().upper(), chain_id=chain_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _redirect_with_message("/admin", error="El hotel o su código ya existe")
    return _redirect_with_message("/admin", ok="Hotel creado")


@app.post("/admin/users")
def create_user(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    hotel_id: uuid.UUID = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    hotel = db.get(Hotel, hotel_id)
    if hotel is None:
        return _redirect_with_message("/admin", error="Hotel no válido")
    if len(password) < 10:
        return _redirect_with_message("/admin", error="La contraseña debe tener al menos 10 caracteres")
    db.add(
        User(
            email=email.strip().lower(),
            full_name=full_name.strip(),
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
        return _redirect_with_message("/admin", error="Ya existe un usuario con ese correo")
    return _redirect_with_message("/admin", ok="Usuario creado")


@app.exception_handler(401)
async def unauthorized_handler(_: Request, __: HTTPException):
    return RedirectResponse("/login", status_code=303)
