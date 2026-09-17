"""Modelo relacional de usuarios, documentos, fragmentos y recomendaciones."""

import enum
import uuid
from datetime import date, datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.database import Base


def utcnow() -> datetime:
    """Devuelve una fecha UTC consciente de zona horaria."""

    return datetime.now(timezone.utc)


class UserRole(str, enum.Enum):
    """Roles de autorización disponibles."""

    ADMIN = "admin"
    USER = "user"


class AudienceType(str, enum.Enum):
    """Ámbito organizativo al que pertenece un documento."""

    GLOBAL = "global"
    CHAIN = "chain"
    HOTEL = "hotel"


class DocumentStatus(str, enum.Enum):
    """Estados posibles de la ingesta asíncrona."""

    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class RecommendationStatus(str, enum.Enum):
    """Estados del ciclo de vida de una recomendación."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SUCCESSFUL = "successful"
    DISCARDED = "discarded"
    SUPERSEDED = "superseded"


class Chain(Base):
    """Grupo empresarial que contiene uno o más hoteles."""

    __tablename__ = "chains"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    hotels: Mapped[list["Hotel"]] = relationship(back_populates="chain")


class Hotel(Base):
    """Unidad operativa perteneciente a una cadena."""

    __tablename__ = "hotels"
    __table_args__ = (UniqueConstraint("chain_id", "name", name="uq_hotel_chain_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chain_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chains.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    chain: Mapped[Chain] = relationship(back_populates="hotels")


class User(Base):
    """Cuenta autenticable y su ámbito organizativo."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), default=UserRole.USER)
    chain_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chains.id", ondelete="SET NULL"))
    hotel_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hotels.id", ondelete="SET NULL"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    chain: Mapped[Chain | None] = relationship()
    hotel: Mapped[Hotel | None] = relationship()


class Document(Base):
    """PDF original, audiencia y estado de procesamiento."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "(audience_type = 'GLOBAL' AND chain_id IS NULL AND hotel_id IS NULL) OR "
            "(audience_type = 'CHAIN' AND chain_id IS NOT NULL AND hotel_id IS NULL) OR "
            "(audience_type = 'HOTEL' AND chain_id IS NOT NULL AND hotel_id IS NOT NULL)",
            name="ck_document_audience_scope",
        ),
        CheckConstraint(
            "category != 'monthly_report' OR (report_month IS NOT NULL AND hotel_id IS NOT NULL)",
            name="ck_monthly_report_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="general", index=True)
    report_month: Mapped[date | None] = mapped_column(nullable=True, index=True)
    audience_type: Mapped[AudienceType] = mapped_column(Enum(AudienceType, name="audience_type"), index=True)
    chain_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("chains.id", ondelete="RESTRICT"), index=True)
    hotel_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hotels.id", ondelete="RESTRICT"), index=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"), default=DocumentStatus.QUEUED, index=True
    )
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    uploaded_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chain: Mapped[Chain | None] = relationship()
    hotel: Mapped[Hotel | None] = relationship()
    uploaded_by: Mapped[User] = relationship()
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentChunk(Base):
    """Fragmento recuperable con vector y representación de texto completo."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number", "chunk_index", name="uq_document_page_chunk"),
        Index("ix_chunks_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(get_settings().embedding_dimensions), nullable=False)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('spanish', coalesce(content, ''))", persisted=True),
    )

    document: Mapped[Document] = relationship(back_populates="chunks")


class QueryLog(Base):
    """Registro auditable de una pregunta y su respuesta."""

    __tablename__ = "query_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Recommendation(Base):
    """Acción propuesta y, opcionalmente, su evaluación posterior."""

    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hotel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hotels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    evaluation_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    waste_category: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    expected_impact: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=RecommendationStatus.PROPOSED.value, index=True
    )
    evaluation_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evaluation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    hotel: Mapped[Hotel] = relationship()
    source_document: Mapped[Document | None] = relationship(foreign_keys=[source_document_id])
    evaluation_document: Mapped[Document | None] = relationship(foreign_keys=[evaluation_document_id])
