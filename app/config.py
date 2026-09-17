"""Configuración tipada y validación de invariantes de despliegue."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración cargada desde entorno con controles adicionales en producción."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Effiwaste RAG"
    environment: str = "development"
    session_secret: str = "development-only-change-me"
    cookie_secure: bool = False
    session_max_age_seconds: int = Field(default=43_200, ge=300, le=604_800)
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    login_max_attempts: int = Field(default=8, ge=3, le=50)
    login_ip_max_attempts: int = Field(default=40, ge=10, le=500)
    login_window_seconds: int = Field(default=900, ge=60, le=86_400)
    seed_demo_data: bool = True

    database_url: str = "postgresql+psycopg://raghotel:raghotel@db:5432/raghotel"
    redis_url: str = "redis://redis:6379/0"
    upload_dir: Path = Path("/data/uploads")
    max_upload_mb: int = 50
    max_pdf_pages: int = 300

    # Envío de solicitudes desde el centro de ayuda. Las credenciales SMTP se
    # configuran en el entorno y nunca se exponen al navegador.
    support_email: str = "soporte@effiwaste.es"
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65_535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    support_attachment_max_mb: int = Field(default=10, ge=1, le=25)
    support_attachment_max_count: int = Field(default=5, ge=1, le=10)

    openai_api_key: str = ""
    openai_chat_model: str = "gpt-6-astra"
    openai_vision_model: str = "gpt-5.6-luna"
    openai_embedding_model: str = "text-embedding-3-large"
    # pgvector HNSW admite hasta 2.000 dimensiones para el tipo vector.
    embedding_dimensions: int = Field(default=1536, ge=256, le=2000)
    retrieval_candidates: int = 24
    retrieval_top_k: int = 8
    retrieval_max_distance: float = 0.58

    admin_email: str = "admin@example.com"
    admin_password: str = "Admin123!"
    demo_user_email: str = "usuario@example.com"
    demo_user_password: str = "Usuario123!"

    @property
    def max_upload_bytes(self) -> int:
        """Tamaño máximo de subida convertido a bytes."""

        return self.max_upload_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        """Indica si deben aplicarse las invariantes estrictas de despliegue."""

        return self.environment.strip().lower() in {"prod", "production"}

    @property
    def allowed_host_list(self) -> list[str]:
        """Hosts aceptados por ``TrustedHostMiddleware``."""

        values = [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]
        return values or ["localhost", "127.0.0.1"]

    @model_validator(mode="after")
    def validate_production_safety(self) -> "Settings":
        """Impide arrancar producción con credenciales o cookies de desarrollo."""

        if not self.is_production:
            return self

        problems: list[str] = []
        if len(self.session_secret) < 32 or self.session_secret == "development-only-change-me":
            problems.append("SESSION_SECRET debe ser aleatorio y tener al menos 32 caracteres")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE debe estar activado")
        if self.seed_demo_data:
            problems.append("SEED_DEMO_DATA debe estar desactivado")
        if self.admin_password == "Admin123!" or len(self.admin_password) < 12:
            problems.append("ADMIN_PASSWORD debe tener al menos 12 caracteres y no usar el valor inicial")
        if "raghotel:raghotel@" in self.database_url:
            problems.append("DATABASE_URL no puede usar las credenciales de desarrollo")
        if "*" in self.allowed_host_list:
            problems.append("ALLOWED_HOSTS debe enumerar los hosts públicos")
        if not self.openai_api_key or self.openai_api_key in {"cambiar", "sk-cambia"}:
            problems.append("OPENAI_API_KEY debe estar configurada")
        if problems:
            raise ValueError("Configuración insegura para producción: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    """Devuelve una única instancia inmutable de configuración por proceso."""

    return Settings()
