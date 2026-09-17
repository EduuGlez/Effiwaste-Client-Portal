import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from app.database import SessionLocal
from app.models import (
    Document,
    DocumentChunk,
    DocumentStatus,
    Recommendation,
    RecommendationStatus,
    utcnow,
)
from app.services.openai_service import evaluate_actions, generate_recommendations


logger = logging.getLogger(__name__)
MAX_REPORT_CONTEXT_CHARS = 60_000


def _report_context(chunks: list[DocumentChunk]) -> str:
    ordered = sorted(chunks, key=lambda item: (item.page_number, item.chunk_index))
    sections: list[str] = []
    size = 0
    for chunk in ordered:
        section = f"[Página {chunk.page_number}]\n{chunk.content}\n"
        if size + len(section) > MAX_REPORT_CONTEXT_CHARS:
            remaining = MAX_REPORT_CONTEXT_CHARS - size
            if remaining > 500:
                sections.append(section[:remaining])
            break
        sections.append(section)
        size += len(section)
    return "\n".join(sections)


def refresh_recommendations_for_report(document_id: uuid.UUID) -> dict:
    """Evalúa acciones aceptadas y crea propuestas desde un informe mensual listo."""
    with SessionLocal() as db:
        document = db.scalar(
            select(Document)
            .options(selectinload(Document.chunks))
            .where(Document.id == document_id)
        )
        if (
            document is None
            or document.status != DocumentStatus.READY
            or document.category != "monthly_report"
            or document.hotel_id is None
            or document.report_month is None
        ):
            return {"status": "skipped"}

        latest_document_id = db.scalar(
            select(Document.id)
            .where(
                Document.hotel_id == document.hotel_id,
                Document.category == "monthly_report",
                Document.status == DocumentStatus.READY,
            )
            .order_by(Document.report_month.desc().nullslast(), Document.created_at.desc())
            .limit(1)
        )
        if latest_document_id != document.id:
            return {"status": "superseded"}

        context = _report_context(list(document.chunks))
        if not context.strip():
            return {"status": "empty"}

        accepted = db.scalars(
            select(Recommendation)
            .options(joinedload(Recommendation.source_document))
            .where(
                Recommendation.hotel_id == document.hotel_id,
                Recommendation.status == RecommendationStatus.ACCEPTED.value,
                Recommendation.source_document_id != document.id,
            )
        ).all()
        eligible = [
            item
            for item in accepted
            if item.source_document
            and item.source_document.report_month
            and item.source_document.report_month < document.report_month
        ]

        evaluation_count = 0
        if eligible:
            evaluations = evaluate_actions(
                [
                    {
                        "id": str(item.id),
                        "title": item.title,
                        "action": item.suggested_action,
                        "expected_impact": item.expected_impact,
                    }
                    for item in eligible
                ],
                document.original_name,
                document.report_month.isoformat(),
                context,
            )
            by_id = {str(item.get("id")): item for item in evaluations}
            for recommendation in eligible:
                result = by_id.get(str(recommendation.id))
                if not result:
                    continue
                outcome = result.get("outcome")
                if outcome == "successful":
                    recommendation.status = RecommendationStatus.SUCCESSFUL.value
                elif outcome == "discarded":
                    recommendation.status = RecommendationStatus.DISCARDED.value
                else:
                    continue
                recommendation.evaluation_result = outcome
                recommendation.evaluation_summary = str(result.get("summary", ""))[:4000]
                recommendation.evaluation_document_id = document.id
                recommendation.evaluated_at = utcnow()
                evaluation_count += 1

        if evaluation_count:
            db.commit()

        existing = db.scalar(
            select(Recommendation.id).where(Recommendation.source_document_id == document.id).limit(1)
        )
        created_count = 0
        if existing is None:
            history = db.scalars(
                select(Recommendation)
                .where(
                    Recommendation.hotel_id == document.hotel_id,
                    Recommendation.status.in_(
                        [
                            RecommendationStatus.ACCEPTED.value,
                            RecommendationStatus.SUCCESSFUL.value,
                            RecommendationStatus.DISCARDED.value,
                        ]
                    ),
                )
                .order_by(Recommendation.created_at.desc())
                .limit(12)
            ).all()
            proposals = generate_recommendations(
                document.original_name,
                document.report_month.isoformat(),
                context,
                [
                    {
                        "title": item.title,
                        "action": item.suggested_action,
                        "outcome": item.status,
                        "evaluation": item.evaluation_summary or "",
                    }
                    for item in history
                ],
            )
            valid_proposals = [
                proposal
                for proposal in proposals[:4]
                if str(proposal.get("title", "")).strip()
                and str(proposal.get("suggested_action", "")).strip()
            ]
            if valid_proposals:
                previous_proposals = db.scalars(
                    select(Recommendation).where(
                        Recommendation.hotel_id == document.hotel_id,
                        Recommendation.status == RecommendationStatus.PROPOSED.value,
                        Recommendation.source_document_id != document.id,
                    )
                ).all()
                for previous in previous_proposals:
                    previous.status = RecommendationStatus.SUPERSEDED.value
                    previous.evaluation_result = "superseded"
                    previous.evaluation_summary = (
                        "Sustituida automáticamente por las recomendaciones del informe mensual más reciente."
                    )
                    previous.evaluated_at = utcnow()
            for proposal in valid_proposals:
                title = str(proposal.get("title", "")).strip()
                action = str(proposal.get("suggested_action", "")).strip()
                if not title or not action:
                    continue
                db.add(
                    Recommendation(
                        hotel_id=document.hotel_id,
                        source_document_id=document.id,
                        waste_category=str(proposal.get("waste_category", "Otros"))[:120],
                        title=title[:180],
                        description=str(proposal.get("description", ""))[:4000],
                        suggested_action=action[:4000],
                        expected_impact=str(proposal.get("expected_impact", ""))[:4000],
                    )
                )
                created_count += 1

        db.commit()
        logger.info(
            "Recomendaciones actualizadas para %s: %s evaluadas, %s creadas",
            document.id,
            evaluation_count,
            created_count,
        )
        return {"status": "ready", "evaluated": evaluation_count, "created": created_count}
