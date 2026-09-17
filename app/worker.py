"""Worker Celery para la ingesta documental fuera del proceso web."""

import logging
import uuid

from celery import Celery
from sqlalchemy import delete

from app.bootstrap import initialize_application
from app.config import get_settings
from app.database import SessionLocal
from app.models import Document, DocumentChunk, DocumentStatus, utcnow
from app.services.openai_service import OpenAIConfigurationError, create_embeddings
from app.services.pdf_service import chunk_page, extract_pdf_pages, validate_pdf
from app.services.recommendation_service import refresh_recommendations_for_report
from app.services.storage import document_path


settings = get_settings()
logger = logging.getLogger(__name__)
celery_app = Celery("raghotel", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_track_started=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=1_700,
    task_time_limit=1_800,
)


@celery_app.task(name="documents.process")
def process_document(document_id: str) -> dict:
    """Valida, extrae, fragmenta e indexa un PDF de forma idempotente."""

    initialize_application()
    with SessionLocal() as db:
        document = db.get(Document, uuid.UUID(document_id))
        if document is None:
            return {"status": "missing"}
        document.status = DocumentStatus.PROCESSING
        document.error_message = None
        db.commit()

        try:
            path = document_path(document.stored_name)
            validate_pdf(path)
            pages = extract_pdf_pages(path)
            pending: list[tuple[int, int, str, int]] = []
            for page in pages:
                for chunk_index, (content, token_count) in enumerate(chunk_page(page.text)):
                    pending.append((page.page_number, chunk_index, content, token_count))

            if not pending:
                raise ValueError("No se pudo extraer contenido útil del PDF.")

            embeddings: list[list[float]] = []
            batch_size = 64
            for start in range(0, len(pending), batch_size):
                embeddings.extend(create_embeddings([item[2] for item in pending[start : start + batch_size]]))

            db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
            for item, embedding in zip(pending, embeddings, strict=True):
                page_number, chunk_index, content, token_count = item
                db.add(
                    DocumentChunk(
                        document_id=document.id,
                        page_number=page_number,
                        chunk_index=chunk_index,
                        content=content,
                        token_count=token_count,
                        embedding=embedding,
                    )
                )

            document.page_count = max(page.page_number for page in pages)
            document.chunk_count = len(pending)
            document.status = DocumentStatus.READY
            document.processed_at = utcnow()
            db.commit()
            recommendation_result = {"status": "not_applicable"}
            if document.category == "monthly_report":
                try:
                    recommendation_result = refresh_recommendations_for_report(document.id)
                except Exception:
                    # El informe y sus vectores siguen siendo válidos aunque falle
                    # temporalmente la generación de recomendaciones.
                    logger.exception("No se pudieron actualizar las recomendaciones")
                    recommendation_result = {"status": "failed"}
            return {
                "status": "ready",
                "chunks": len(pending),
                "recommendations": recommendation_result,
            }
        except Exception as exc:
            db.rollback()
            logger.exception("Falló la ingesta del documento %s", document_id)
            document = db.get(Document, uuid.UUID(document_id))
            if document is not None:
                document.status = DocumentStatus.FAILED
                document.error_message = (
                    str(exc)[:2000]
                    if isinstance(exc, (ValueError, OpenAIConfigurationError))
                    else "La ingesta falló por un error interno. Consulta los logs del worker."
                )
                db.commit()
            raise
