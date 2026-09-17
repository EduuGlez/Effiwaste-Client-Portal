"""Validación, extracción visual y fragmentación controlada de documentos PDF."""

import base64
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import tiktoken

from app.config import get_settings
from app.services.openai_service import transcribe_page_image


@dataclass(slots=True)
class PageContent:
    """Contenido normalizado de una página y procedencia de su extracción."""

    page_number: int
    text: str
    used_vision: bool


def validate_pdf(path: Path) -> None:
    """Rechaza archivos sin firma, dañados, cifrados o fuera de límites."""

    with path.open("rb") as file:
        signature = file.read(5)
    if signature != b"%PDF-":
        raise ValueError("El archivo no tiene una firma PDF válida.")
    try:
        with pymupdf.open(path) as document:
            if document.page_count < 1:
                raise ValueError("El PDF no contiene páginas.")
            if document.page_count > get_settings().max_pdf_pages:
                raise ValueError(
                    f"El PDF supera el límite de {get_settings().max_pdf_pages} páginas."
                )
            if document.needs_pass:
                raise ValueError("No se admiten PDFs protegidos con contraseña.")
    except pymupdf.FileDataError as exc:
        raise ValueError("El archivo PDF está dañado o no es válido.") from exc


def _clean_text(value: str) -> str:
    """Elimina nulos y ruido de espaciado sin destruir párrafos."""

    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def extract_pdf_pages(path: Path) -> list[PageContent]:
    """Combina texto nativo y visión cuando una página lo requiere."""

    pages: list[PageContent] = []
    with pymupdf.open(path) as document:
        for index, page in enumerate(document, start=1):
            native_text = _clean_text(page.get_text("text", sort=True))
            has_images = bool(page.get_images(full=True))
            # Las gráficas vectoriales no aparecen en get_images; muchos trazados suelen
            # indicar tablas, diagramas o gráficos que también requieren visión.
            has_complex_drawings = len(page.get_drawings()) >= 18
            needs_vision = has_images or has_complex_drawings or len(native_text) < 220
            visual_text = ""

            if needs_vision:
                # 180 DPI conserva caracteres pequeños sin disparar innecesariamente el tamaño de entrada.
                scale = min(2.5, 3000 / max(page.rect.width, 1), 3000 / max(page.rect.height, 1))
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                visual_text = transcribe_page_image(
                    f"data:image/png;base64,{encoded}", index, native_text
                )

            if native_text and visual_text:
                combined = f"{native_text}\n\n[Contenido visual y OCR]\n{visual_text}"
            else:
                combined = native_text or visual_text

            if combined.strip():
                pages.append(PageContent(index, _clean_text(combined), needs_vision))
    return pages


def chunk_page(text: str, max_tokens: int = 850, overlap: int = 120) -> list[tuple[str, int]]:
    """Divide una página en ventanas solapadas medidas con el tokenizer."""

    if max_tokens <= 0 or overlap < 0 or overlap >= max_tokens:
        raise ValueError("max_tokens debe ser positivo y overlap menor que max_tokens")
    if not text.strip():
        return []
    encoding = tiktoken.get_encoding("cl100k_base")
    tokens = encoding.encode(text)
    chunks: list[tuple[str, int]] = []
    cursor = 0
    while cursor < len(tokens):
        token_slice = tokens[cursor : cursor + max_tokens]
        chunk = encoding.decode(token_slice).strip()
        if chunk:
            chunks.append((chunk, len(token_slice)))
        if cursor + max_tokens >= len(tokens):
            break
        cursor += max_tokens - overlap
    return chunks
