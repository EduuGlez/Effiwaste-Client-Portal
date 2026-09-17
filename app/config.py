from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Effiwaste RAG"
    environment: str = "development"
    session_secret: str = "development-only-change-me"
    cookie_secure: bool = False

    database_url: str = "postgresql+psycopg://raghotel:raghotel@db:5432/raghotel"
    redis_url: str = "redis://redis:6379/0"
    upload_dir: Path = Path("/data/uploads")
    max_upload_mb: int = 50
    max_pdf_pages: int = 300

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
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
