"""Dependencias y utilidades compartidas por los routers HTTP.

Este módulo concentra autenticación de sesión, autorización, CSRF y renderizado
para que los endpoints expresen la operación de negocio y no repitan controles
de seguridad en cada archivo.
"""

import re
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.models import User, UserRole
from app.security import csrf_matches, new_csrf_token


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_user_from_session(request: Request, db: Session) -> User | None:
    """Devuelve el usuario activo asociado a la sesión firmada, si existe."""

    value = request.session.get("user_id")
    if not value:
        return None
    try:
        user = db.get(User, uuid.UUID(str(value)))
    except (ValueError, TypeError):
        request.session.clear()
        return None
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def require_user(request: Request, db: Session) -> User:
    """Exige una sesión válida sin revelar si el usuario existe o está inactivo."""

    user = get_user_from_session(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Sesión no válida")
    return user


def require_admin(request: Request, db: Session) -> User:
    """Exige una sesión válida con rol administrativo."""

    user = require_user(request, db)
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Acceso reservado a administración")
    return user


def ensure_csrf_token(request: Request) -> str:
    """Crea el token CSRF de sesión cuando todavía no existe."""

    current = request.session.get("csrf")
    if isinstance(current, str) and current:
        return current
    token = new_csrf_token()
    request.session["csrf"] = token
    return token


def verify_csrf(request: Request, supplied: str | None) -> None:
    """Compara en tiempo constante el token recibido con el token de sesión."""

    if not csrf_matches(request.session.get("csrf"), supplied):
        raise HTTPException(status_code=403, detail="Token de seguridad no válido")


def redirect_with_message(
    path: str, *, ok: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Construye una redirección POST/Redirect/GET con un mensaje escapado."""

    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("La redirección debe apuntar a una ruta interna absoluta")
    if ok:
        path += f"?ok={quote(ok)}"
    elif error:
        path += f"?error={quote(error)}"
    return RedirectResponse(path, status_code=303)


def sanitize_original_filename(filename: str) -> str:
    """Normaliza un nombre de subida para mostrarlo y enviarlo en cabeceras.

    El archivo nunca se almacena con este nombre; en disco se usa un UUID. Esta
    limpieza elimina rutas de Unix/Windows y caracteres de control como defensa
    adicional para plantillas, logs y ``Content-Disposition``.
    """

    normalized = filename.replace("\\", "/")
    basename = PurePosixPath(normalized).name
    basename = re.sub(r"[\x00-\x1f\x7f]", "", basename).strip()
    return (basename or "documento.pdf")[:255]
