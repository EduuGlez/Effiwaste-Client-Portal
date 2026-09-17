"""Reglas de nombres y rutas para los PDFs almacenados."""

import re
from pathlib import Path

from app.config import get_settings


STORED_PDF_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.pdf$",
    re.IGNORECASE,
)


def document_path(stored_name: str) -> Path:
    """Resuelve un nombre interno validado dentro del directorio de subidas."""

    if not STORED_PDF_PATTERN.fullmatch(stored_name):
        raise ValueError("Nombre interno de documento no válido")
    return get_settings().upload_dir / stored_name
