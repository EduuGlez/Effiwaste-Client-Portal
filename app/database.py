"""Motor SQLAlchemy, sesiones transaccionales y preparación del esquema."""

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


settings = get_settings()
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1_800,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


class Base(DeclarativeBase):
    """Base declarativa común para todos los modelos persistentes."""


def get_db() -> Generator[Session, None, None]:
    """Entrega una sesión aislada y garantiza su cierre al terminar la petición."""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_database() -> None:
    """Activa pgvector y crea el esquema requerido por una instalación nueva.

    Los ``ALTER TABLE`` mantienen compatibilidad con volúmenes de desarrollo
    anteriores. Los despliegues con cambios de esquema adicionales deben usar
    migraciones versionadas antes de iniciar la aplicación.
    """

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
        # Las restricciones se añaden también a volúmenes creados por versiones
        # anteriores. NOT VALID evita un bloqueo largo al crearlas; VALIDATE
        # comprueba inmediatamente los datos históricos y mantiene fail-closed.
        connection.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_document_audience_scope'
                          AND conrelid = 'documents'::regclass
                    ) THEN
                        ALTER TABLE documents
                        ADD CONSTRAINT ck_document_audience_scope CHECK (
                            (audience_type = 'GLOBAL' AND chain_id IS NULL AND hotel_id IS NULL) OR
                            (audience_type = 'CHAIN' AND chain_id IS NOT NULL AND hotel_id IS NULL) OR
                            (audience_type = 'HOTEL' AND chain_id IS NOT NULL AND hotel_id IS NOT NULL)
                        ) NOT VALID;
                    END IF;
                END $$;
                """
            )
        )
        connection.execute(
            text(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'ck_monthly_report_scope'
                          AND conrelid = 'documents'::regclass
                    ) THEN
                        ALTER TABLE documents
                        ADD CONSTRAINT ck_monthly_report_scope CHECK (
                            category != 'monthly_report' OR
                            (report_month IS NOT NULL AND hotel_id IS NOT NULL)
                        ) NOT VALID;
                    END IF;
                END $$;
                """
            )
        )
        connection.execute(text("ALTER TABLE documents VALIDATE CONSTRAINT ck_document_audience_scope"))
        connection.execute(text("ALTER TABLE documents VALIDATE CONSTRAINT ck_monthly_report_scope"))
