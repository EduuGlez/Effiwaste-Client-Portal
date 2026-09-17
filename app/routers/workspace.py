"""Espacio autenticado: servicios, RAG, biblioteca y recomendaciones."""

import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.constants import DOCUMENT_CATEGORIES, EXTERNAL_SERVICES, LIBRARY_CATEGORY_LABELS
from app.database import get_db
from app.models import Document, DocumentStatus, Recommendation, RecommendationStatus, utcnow
from app.schemas import ChatRequest
from app.services.access import can_access_document, document_access_filter
from app.services.markdown_service import render_markdown
from app.services.email_service import (
    SupportAttachment,
    SupportEmailConfigurationError,
    attachment_extension,
    build_support_message,
    send_support_email,
    validate_support_text,
)
from app.services.rag_service import ask
from app.services.storage import document_path
from app.web import (
    ensure_csrf_token,
    get_user_from_session,
    redirect_with_message,
    require_user,
    templates,
    verify_csrf,
)


logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


def _authenticated_or_login(request: Request, db: Session):
    """Devuelve el usuario o una redirección lista para las vistas HTML."""

    user = get_user_from_session(request, db)
    if user is None:
        return None, RedirectResponse("/login", status_code=303)
    return user, None


@router.get("/app", include_in_schema=False)
def legacy_app_redirect(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Mantiene compatibilidad con el antiguo acceso ``/app``."""

    user, response = _authenticated_or_login(request, db)
    return response or RedirectResponse("/assistant", status_code=303)


@router.get("/assistant", include_in_schema=False)
def assistant_view(request: Request, db: Session = Depends(get_db)):
    """Muestra el chat y la biblioteca accesible para el usuario."""

    user, response = _authenticated_or_login(request, db)
    if response:
        return response
    documents = db.scalars(
        select(Document)
        .where(Document.status == DocumentStatus.READY, document_access_filter(user))
        .order_by(Document.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="user.html",
        context={
            "user": user,
            "documents": documents,
            "monthly_documents": [item for item in documents if item.category == "monthly_report"],
            "general_documents": [item for item in documents if item.category != "monthly_report"],
            "category_labels": DOCUMENT_CATEGORIES,
            "csrf_token": ensure_csrf_token(request),
        },
    )


@router.get("/library", include_in_schema=False)
def document_library_view(request: Request, db: Session = Depends(get_db)):
    """Muestra y clasifica todos los documentos visibles para la sesión."""

    user, response = _authenticated_or_login(request, db)
    if response:
        return response
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
            "csrf_token": ensure_csrf_token(request),
        },
    )


@router.get("/services", include_in_schema=False)
def services_view(request: Request, db: Session = Depends(get_db)):
    """Presenta los enlaces externos configurados para la Suite."""

    user, response = _authenticated_or_login(request, db)
    if response:
        return response
    return templates.TemplateResponse(
        request=request,
        name="services.html",
        context={
            "user": user,
            "services": EXTERNAL_SERVICES,
            "csrf_token": ensure_csrf_token(request),
        },
    )


@router.get("/contact", include_in_schema=False)
def contact_view(request: Request, db: Session = Depends(get_db)):
    """Muestra el centro de ayuda a cualquier usuario autenticado."""

    user, response = _authenticated_or_login(request, db)
    if response:
        return response
    return templates.TemplateResponse(
        request=request,
        name="contact.html",
        context={
            "user": user,
            "csrf_token": ensure_csrf_token(request),
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("error"),
            "attachment_max_mb": settings.support_attachment_max_mb,
            "attachment_max_count": settings.support_attachment_max_count,
        },
    )


@router.post("/contact", include_in_schema=False)
async def send_contact_request(
    request: Request,
    subject: str = Form(...),
    body: str = Form(...),
    csrf_token: str = Form(...),
    attachments: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """Envía una solicitud enriquecida con la identidad verificada de sesión."""

    user = require_user(request, db)
    verify_csrf(request, csrf_token)
    uploaded: list[SupportAttachment] = []
    total_size = 0
    max_bytes = settings.support_attachment_max_mb * 1024 * 1024
    try:
        clean_subject, clean_body = validate_support_text(subject, body)
        actual_files = [item for item in attachments if item.filename]
        if len(actual_files) > settings.support_attachment_max_count:
            raise ValueError(
                f"Puedes adjuntar un máximo de {settings.support_attachment_max_count} archivos"
            )
        for attachment in actual_files:
            attachment_extension(attachment.filename or "")
            content = await attachment.read(max_bytes + 1)
            total_size += len(content)
            if total_size > max_bytes:
                raise ValueError(
                    f"El conjunto de adjuntos no puede superar {settings.support_attachment_max_mb} MB"
                )
            uploaded.append(
                SupportAttachment(
                    filename=sanitize_original_filename(attachment.filename or "adjunto"),
                    content=content,
                )
            )

        hotel_name = user.hotel.name if user.hotel else "Sin hotel asignado"
        chain_name = (
            user.chain.name
            if user.chain
            else user.hotel.chain.name
            if user.hotel and user.hotel.chain
            else "Sin cadena asignada"
        )
        message = build_support_message(
            settings=settings,
            user_name=user.full_name,
            user_email=user.email,
            hotel_name=hotel_name,
            chain_name=chain_name,
            subject=clean_subject,
            body=clean_body,
            attachments=uploaded,
        )
        await run_in_threadpool(send_support_email, settings, message)
    except ValueError as exc:
        return redirect_with_message("/contact", error=str(exc))
    except SupportEmailConfigurationError:
        logger.error("El centro de ayuda no tiene SMTP configurado")
        return redirect_with_message(
            "/contact", error="El envío no está disponible temporalmente. Contacta por teléfono o correo."
        )
    except Exception:
        logger.exception("No se pudo enviar una solicitud de soporte")
        return redirect_with_message(
            "/contact", error="No se pudo enviar la solicitud. Inténtalo de nuevo o contacta por teléfono."
        )
    finally:
        for attachment in attachments:
            await attachment.close()

    return redirect_with_message(
        "/contact", ok="Tu solicitud se ha enviado correctamente. El equipo de soporte te responderá por correo."
    )


@router.get("/recommendations", include_in_schema=False)
def recommendations_view(request: Request, db: Session = Depends(get_db)):
    """Agrupa las recomendaciones del hotel por su estado de seguimiento."""

    user, response = _authenticated_or_login(request, db)
    if response:
        return response
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
            "csrf_token": ensure_csrf_token(request),
            "ok": request.query_params.get("ok"),
            "error": request.query_params.get("error"),
        },
    )


def _owned_proposal(db: Session, user, recommendation_id: uuid.UUID) -> Recommendation:
    """Obtiene una propuesta sin revelar recursos pertenecientes a otro hotel."""

    recommendation = db.get(Recommendation, recommendation_id)
    if recommendation is None or user.hotel_id is None or recommendation.hotel_id != user.hotel_id:
        raise HTTPException(404, "Recomendación no encontrada")
    return recommendation


@router.post("/recommendations/{recommendation_id}/accept", include_in_schema=False)
def accept_recommendation(
    recommendation_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Acepta una propuesta perteneciente al hotel de la sesión."""

    user = require_user(request, db)
    verify_csrf(request, csrf_token)
    recommendation = _owned_proposal(db, user, recommendation_id)
    if recommendation.status != RecommendationStatus.PROPOSED.value:
        return redirect_with_message(
            "/recommendations", error="Esta recomendación ya no está pendiente"
        )
    recommendation.status = RecommendationStatus.ACCEPTED.value
    recommendation.accepted_at = utcnow()
    db.commit()
    return redirect_with_message(
        "/recommendations",
        ok="Mejora aceptada. Se evaluará automáticamente con el próximo informe mensual.",
    )


@router.post("/recommendations/{recommendation_id}/discard", include_in_schema=False)
def discard_recommendation(
    recommendation_id: uuid.UUID,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Descarta una propuesta y conserva la decisión para evitar repeticiones."""

    user = require_user(request, db)
    verify_csrf(request, csrf_token)
    recommendation = _owned_proposal(db, user, recommendation_id)
    if recommendation.status != RecommendationStatus.PROPOSED.value:
        return redirect_with_message(
            "/recommendations", error="Esta recomendación ya no está pendiente"
        )
    recommendation.status = RecommendationStatus.DISCARDED.value
    recommendation.evaluation_result = "user_discarded"
    recommendation.evaluation_summary = "Descartada por el cliente porque no encajaba con su operativa."
    recommendation.evaluated_at = utcnow()
    db.commit()
    return redirect_with_message(
        "/recommendations", ok="Propuesta descartada. La IA la tendrá en cuenta en futuros informes."
    )


@router.post("/api/chat")
def chat(payload: ChatRequest, request: Request, db: Session = Depends(get_db)):
    """Responde mediante RAG y entrega una versión HTML sanitizada."""

    user = require_user(request, db)
    verify_csrf(request, request.headers.get("x-csrf-token"))
    try:
        result = ask(db, user, payload.question)
        result["answer_html"] = render_markdown(result["answer"])
        return result
    except Exception as exc:
        logger.exception("Falló una consulta del asistente")
        detail = (
            str(exc)
            if "OPENAI_API_KEY" in str(exc)
            else "No se pudo completar la consulta. Revisa el servicio de OpenAI y vuelve a intentarlo."
        )
        return JSONResponse({"detail": detail}, status_code=502)


def _authorized_document(db: Session, user, document_id: uuid.UUID) -> Document:
    """Carga un documento y aplica la política de audiencia en memoria."""

    document = db.get(Document, document_id)
    if document is None or not can_access_document(user, document):
        # Se usa 404 para no revelar la existencia de documentos de otra audiencia.
        raise HTTPException(404, "Documento no encontrado")
    return document


@router.get("/documents/{document_id}/download", include_in_schema=False)
def download_document(document_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """Descarga un PDF después de aplicar el filtro multiempresa."""

    user = require_user(request, db)
    document = _authorized_document(db, user, document_id)
    path = document_path(document.stored_name)
    if not path.is_file():
        raise HTTPException(404, "El archivo no está disponible")
    return FileResponse(path, media_type="application/pdf", filename=document.original_name)


@router.get("/documents/{document_id}/view", include_in_schema=False)
def view_document(document_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    """Sirve un PDF autorizado en modo inline."""

    user = require_user(request, db)
    document = _authorized_document(db, user, document_id)
    path = document_path(document.stored_name)
    if not path.is_file():
        raise HTTPException(404, "El archivo no está disponible")
    encoded_name = quote(document.original_name)
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{encoded_name}"},
    )
