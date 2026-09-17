"""Política central de acceso multiempresa a los documentos."""

from sqlalchemy import and_, or_

from app.models import AudienceType, Document, User, UserRole


def document_access_filter(user: User):
    """Construye el predicado SQL de documentos visibles para un usuario."""

    if user.role == UserRole.ADMIN:
        return True

    clauses = [Document.audience_type == AudienceType.GLOBAL]
    if user.chain_id:
        clauses.append(
            and_(Document.audience_type == AudienceType.CHAIN, Document.chain_id == user.chain_id)
        )
    if user.hotel_id:
        clauses.append(
            and_(Document.audience_type == AudienceType.HOTEL, Document.hotel_id == user.hotel_id)
        )
    return or_(*clauses)


def can_access_document(user: User, document: Document) -> bool:
    """Aplica la misma política a un documento ya cargado."""

    if user.role == UserRole.ADMIN or document.audience_type == AudienceType.GLOBAL:
        return True
    if document.audience_type == AudienceType.CHAIN:
        return bool(user.chain_id and user.chain_id == document.chain_id)
    if document.audience_type == AudienceType.HOTEL:
        return bool(user.hotel_id and user.hotel_id == document.hotel_id)
    return False
