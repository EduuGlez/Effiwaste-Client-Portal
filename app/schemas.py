"""Esquemas de entrada para los endpoints JSON de la aplicación."""

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    """Pregunta enviada al asistente documental.

    La normalización ocurre antes de validar la longitud para impedir que una
    cadena formada solo por espacios llegue al pipeline de embeddings.
    """

    question: str = Field(min_length=2, max_length=2000)

    @field_validator("question", mode="before")
    @classmethod
    def normalize_question(cls, value: object) -> object:
        """Elimina espacios periféricos antes de medir la longitud."""

        return value.strip() if isinstance(value, str) else value
