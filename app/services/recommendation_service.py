"""Ciclo de propuestas, aceptación y evaluación de mejoras por hotel."""

import logging
import re
import unicodedata
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

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
MAX_BASELINE_EVIDENCE_CHARS = 16_000
MEASUREMENT_PATTERN = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>%|kg|g|t|litros?|unidades?|raciones?)(?!\w)",
    re.IGNORECASE,
)
REPORT_COVERAGE_MARKERS = (
    ("informe mensual", "informe de desperdicio"),
    ("desperdicio total", "total de desperdicio"),
    ("desperdicio por categoria", "top 10", "categorias de alimentos"),
    ("kg/mes", "kg/servicio", "kg/comensal", "kg por comensal"),
    ("comparacion mensual", "evolucion mensual", "serie historica"),
)
SEARCH_STOPWORDS = {
    "accion",
    "ajustar",
    "aplicar",
    "controlar",
    "desperdicio",
    "hotel",
    "mejora",
    "para",
    "produccion",
    "reducir",
    "servicio",
}


def _report_context(chunks: list[DocumentChunk]) -> str:
    """Construye contexto ordenado y acotado para controlar coste y latencia."""

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


def _normalize_search_text(value: str) -> str:
    """Normaliza acentos y mayúsculas para comparaciones léxicas internas."""

    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character)).lower()


def _report_has_comprehensive_waste_coverage(context: str) -> bool:
    """Detecta si el documento parece un informe mensual amplio de desperdicio.

    Esta barrera determinista complementa al modelo: una ausencia solo puede
    convertirse en éxito cuando el documento contiene varias secciones típicas
    de medición y no es, por ejemplo, una receta subida con categoría errónea.
    """

    normalized = _normalize_search_text(context)
    matched_groups = sum(
        1 for alternatives in REPORT_COVERAGE_MARKERS if any(item in normalized for item in alternatives)
    )
    return len(normalized) >= 2_000 and matched_groups >= 3


def _action_search_terms(recommendation: Recommendation) -> dict[str, int]:
    """Extrae términos ponderados para localizar la evidencia inicial."""

    weighted_sources = (
        (recommendation.waste_category, 5),
        (recommendation.title, 3),
        (recommendation.expected_impact, 2),
        (recommendation.suggested_action, 1),
    )
    terms: dict[str, int] = {}
    for value, weight in weighted_sources:
        for term in re.findall(r"[a-z0-9]+", _normalize_search_text(value)):
            if len(term) >= 4 and term not in SEARCH_STOPWORDS:
                terms[term] = max(terms.get(term, 0), weight)
    return terms


def _baseline_evidence(recommendation: Recommendation) -> str:
    """Selecciona fragmentos relevantes del informe que originó la acción."""

    source = recommendation.source_document
    if source is None:
        return ""
    terms = _action_search_terms(recommendation)
    scored: list[tuple[int, DocumentChunk]] = []
    for chunk in source.chunks:
        content = _normalize_search_text(chunk.content)
        score = sum(weight * min(content.count(term), 3) for term, weight in terms.items())
        if score:
            scored.append((score, chunk))
    if not scored:
        return _report_context(list(source.chunks))[:MAX_BASELINE_EVIDENCE_CHARS]

    selected: list[str] = []
    size = 0
    for _, chunk in sorted(scored, key=lambda item: (-item[0], item[1].page_number)):
        section = f"[Página {chunk.page_number}]\n{chunk.content}\n"
        if size + len(section) > MAX_BASELINE_EVIDENCE_CHARS:
            continue
        selected.append(section)
        size += len(section)
    return "\n".join(selected)


def _resolve_evaluation(result: dict, comprehensive_coverage: bool) -> tuple[str, str]:
    """Aplica reglas deterministas a la valoración propuesta por el modelo."""

    outcome = str(result.get("outcome", "inconclusive"))
    evidence_type = str(result.get("evidence_type", "insufficient"))
    comparable = result.get("report_is_comparable") is True
    baseline = str(result.get("baseline_evidence", "")).strip()
    current = str(result.get("current_evidence", "")).strip()
    summary = str(result.get("summary", "")).strip()

    baseline_measurement = _extract_measurement(baseline)
    current_measurement = _extract_measurement(current)

    if outcome == "successful" and evidence_type == "quantitative":
        if (
            comparable
            and baseline_measurement
            and current_measurement
            and baseline_measurement[0] == current_measurement[0]
            and current_measurement[1] < baseline_measurement[1]
        ):
            return outcome, summary
        return (
            "inconclusive",
            "La evaluación menciona una mejora, pero no aporta medidas comparables del informe inicial y posterior.",
        )

    if outcome == "successful" and evidence_type == "qualitative_absence":
        if comparable and comprehensive_coverage and baseline and current:
            return outcome, summary
        return (
            "inconclusive",
            "El informe posterior no ofrece cobertura mensual comparable suficiente; la ausencia de la categoría "
            "no permite confirmar todavía una reducción.",
        )

    if outcome == "discarded" and comparable and evidence_type in {
        "quantitative",
        "qualitative_presence",
    }:
        if evidence_type == "qualitative_presence" and current:
            return outcome, summary
        if (
            evidence_type == "quantitative"
            and baseline_measurement
            and current_measurement
            and baseline_measurement[0] == current_measurement[0]
            and current_measurement[1] >= baseline_measurement[1]
        ):
            return outcome, summary

    if outcome == "inconclusive":
        return outcome, summary or "El informe posterior no contiene evidencia comparable suficiente."

    return "inconclusive", "La evidencia devuelta no supera los criterios de validación."


def _extract_measurement(value: str) -> tuple[str, float] | None:
    """Extrae una medida comparable normalizando formatos y unidades comunes."""

    match = MEASUREMENT_PATTERN.search(value)
    if match is None:
        return None
    raw_number = match.group("value")
    if "," in raw_number:
        normalized_number = raw_number.replace(".", "").replace(",", ".")
    elif raw_number.count(".") == 1 and len(raw_number.rsplit(".", 1)[1]) == 3:
        normalized_number = raw_number.replace(".", "")
    else:
        normalized_number = raw_number
    number = float(normalized_number)
    unit = match.group("unit").lower()
    if unit == "t":
        return "mass", number * 1_000_000
    if unit == "kg":
        return "mass", number * 1_000
    if unit == "g":
        return "mass", number
    if unit.startswith("litro"):
        return "volume", number
    if unit.startswith("unidad"):
        return "units", number
    if unit.startswith("racion"):
        return "servings", number
    return "percentage", number


def refresh_recommendations_for_report(document_id: uuid.UUID) -> dict:
    """Evalúa acciones aceptadas y crea propuestas desde un informe mensual listo."""
    with SessionLocal() as db:
        document = db.scalar(
            select(Document)
            .options(selectinload(Document.chunks))
            .where(Document.id == document_id)
            .with_for_update()
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
        comprehensive_coverage = _report_has_comprehensive_waste_coverage(context)

        accepted = db.scalars(
            select(Recommendation)
            .options(
                selectinload(Recommendation.source_document).selectinload(Document.chunks)
            )
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
            and item.accepted_at
            and item.accepted_at < document.created_at
        ]

        evaluation_count = 0
        resolved_count = 0
        if eligible and not comprehensive_coverage:
            for recommendation in eligible:
                recommendation.evaluation_result = "inconclusive"
                recommendation.evaluation_summary = (
                    "El documento posterior no contiene una medición mensual completa y comparable de desperdicio; "
                    "que una categoría no aparezca en este archivo no demuestra por sí solo que se haya reducido."
                )
                recommendation.evaluation_document_id = document.id
                recommendation.evaluated_at = utcnow()
                evaluation_count += 1

        if eligible and comprehensive_coverage:
            evaluations = evaluate_actions(
                [
                    {
                        "id": str(item.id),
                        "title": item.title,
                        "waste_category": item.waste_category,
                        "action": item.suggested_action,
                        "expected_impact": item.expected_impact,
                        "baseline_report": item.source_document.original_name,
                        "baseline_month": item.source_document.report_month.isoformat(),
                        "baseline_evidence": _baseline_evidence(item),
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
                    result = {
                        "outcome": "inconclusive",
                        "evidence_type": "insufficient",
                        "report_is_comparable": False,
                        "summary": "El informe posterior no pudo evaluarse con suficiente fiabilidad.",
                    }
                outcome, summary = _resolve_evaluation(result, comprehensive_coverage)
                if outcome == "successful":
                    recommendation.status = RecommendationStatus.SUCCESSFUL.value
                    resolved_count += 1
                elif outcome == "discarded":
                    recommendation.status = RecommendationStatus.DISCARDED.value
                    resolved_count += 1
                recommendation.evaluation_result = outcome
                recommendation.evaluation_summary = summary[:4000]
                recommendation.evaluation_document_id = document.id
                recommendation.evaluated_at = utcnow()
                evaluation_count += 1

        if evaluation_count:
            db.commit()

        existing = db.scalar(
            select(Recommendation.id).where(Recommendation.source_document_id == document.id).limit(1)
        )
        created_count = 0
        if not comprehensive_coverage:
            invalid_proposals = db.scalars(
                select(Recommendation).where(
                    Recommendation.source_document_id == document.id,
                    Recommendation.status == RecommendationStatus.PROPOSED.value,
                )
            ).all()
            for proposal in invalid_proposals:
                proposal.status = RecommendationStatus.SUPERSEDED.value
                proposal.evaluation_result = "invalid_source"
                proposal.evaluation_summary = (
                    "El documento de origen no contiene un informe mensual completo de desperdicio."
                )
                proposal.evaluated_at = utcnow()
        elif existing is None:
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
        return {
            "status": "ready" if comprehensive_coverage else "insufficient_report",
            "evaluated": evaluation_count,
            "resolved": resolved_count,
            "created": created_count,
        }
