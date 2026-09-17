from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.models import Document, DocumentChunk, DocumentStatus, QueryLog, User
from app.services.access import document_access_filter
from app.services.openai_service import answer_with_context, create_embeddings


@dataclass(slots=True)
class RetrievalResult:
    chunk: DocumentChunk
    score: float


def retrieve(db: Session, user: User, question: str) -> list[RetrievalResult]:
    settings = get_settings()
    query_embedding = create_embeddings([question])[0]
    eligible = document_access_filter(user)
    base_conditions = (Document.status == DocumentStatus.READY, eligible)

    distance = DocumentChunk.embedding.cosine_distance(query_embedding)
    semantic_rows = db.execute(
        select(DocumentChunk, distance.label("distance"))
        .join(Document)
        .options(joinedload(DocumentChunk.document))
        .where(*base_conditions, distance <= settings.retrieval_max_distance)
        .order_by(distance)
        .limit(settings.retrieval_candidates)
    ).all()
    semantic = [row[0] for row in semantic_rows]

    spanish_query = func.plainto_tsquery("spanish", question)
    text_rank = func.ts_rank_cd(DocumentChunk.search_vector, spanish_query)
    lexical = db.scalars(
        select(DocumentChunk)
        .join(Document)
        .options(joinedload(DocumentChunk.document))
        .where(*base_conditions, DocumentChunk.search_vector.op("@@")(spanish_query))
        .order_by(text_rank.desc())
        .limit(settings.retrieval_candidates)
    ).all()

    # Reciprocal Rank Fusion combina coincidencias semánticas y literales sin comparar escalas incompatibles.
    combined: dict[object, RetrievalResult] = {}
    for rank, chunk in enumerate(semantic, start=1):
        combined[chunk.id] = RetrievalResult(chunk=chunk, score=1 / (60 + rank))
    for rank, chunk in enumerate(lexical, start=1):
        if chunk.id in combined:
            combined[chunk.id].score += 1 / (60 + rank)
        else:
            combined[chunk.id] = RetrievalResult(chunk=chunk, score=1 / (60 + rank))

    return sorted(combined.values(), key=lambda item: item.score, reverse=True)[: settings.retrieval_top_k]


def ask(db: Session, user: User, question: str) -> dict:
    results = retrieve(db, user, question)
    sources = [
        {
            "document_id": str(item.chunk.document_id),
            "document_name": item.chunk.document.original_name,
            "page_number": item.chunk.page_number,
            "content": item.chunk.content,
            "score": round(item.score, 6),
        }
        for item in results
    ]

    if not sources:
        answer = "No encontré información suficiente en los documentos disponibles."
    else:
        answer = answer_with_context(question, sources, str(user.id))

    db.add(QueryLog(user_id=user.id, question=question, answer=answer, source_count=len(sources)))
    db.commit()
    return {"answer": answer, "sources": sources}
