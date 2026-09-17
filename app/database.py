from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database() -> None:
    # El contenedor pgvector incluye esta extensión, pero debe activarse por base de datos.
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        # Migración compatible con instalaciones existentes. create_all crea tablas
        # nuevas, pero no añade columnas a una tabla que ya existe.
        connection.execute(
            text(
                "ALTER TABLE IF EXISTS documents "
                "ADD COLUMN IF NOT EXISTS category VARCHAR(32) NOT NULL DEFAULT 'general'"
            )
        )
        connection.execute(
            text("ALTER TABLE IF EXISTS documents ADD COLUMN IF NOT EXISTS report_month DATE")
        )
    from app import models  # noqa: F401 - registra los modelos antes de create_all

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_documents_category ON documents (category)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_documents_report_month ON documents (report_month)")
        )
