"""Rutas públicas, autenticación y comprobación de salud."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.security import new_csrf_token, verify_login_password
from app.services.login_limiter import login_rate_limiter
from app.web import (
    ensure_csrf_token,
    get_user_from_session,
    redirect_with_message,
    templates,
    verify_csrf,
)


router = APIRouter()
settings = get_settings()


def _client_ip(request: Request) -> str:
    """Obtiene la IP observada por ASGI sin confiar en cabeceras manipulables."""

    return request.client.host if request.client else "unknown"


@router.get("/health", include_in_schema=False)
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    """Confirma que el proceso web puede consultar la base de datos."""

    db.execute(select(1))
    return {"status": "ok"}


@router.get("/", include_in_schema=False)
def root(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Envía al usuario al login o a su espacio de trabajo."""

    destination = "/services" if get_user_from_session(request, db) else "/login"
    return RedirectResponse(destination, status_code=303)


@router.get("/login", include_in_schema=False)
def login_page(request: Request, db: Session = Depends(get_db)):
    """Muestra el formulario y crea su token anti-CSRF."""

    if get_user_from_session(request, db):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": request.query_params.get("error"),
            "app_name": settings.app_name,
            "csrf_token": ensure_csrf_token(request),
        },
    )


@router.post("/auth/login", include_in_schema=False)
def login(
    request: Request,
    email: str = Form(..., max_length=320),
    password: str = Form(..., max_length=1024),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Autentica con coste constante y limitación distribuida de intentos."""

    verify_csrf(request, csrf_token)
    normalized_email = email.strip().lower()
    client_ip = _client_ip(request)
    if login_rate_limiter.is_blocked(normalized_email, client_ip):
        return redirect_with_message(
            "/login",
            error="Demasiados intentos. Espera unos minutos antes de volver a intentarlo.",
        )

    user = db.scalar(select(User).where(func.lower(User.email) == normalized_email))
    encoded = user.password_hash if user and user.is_active else None
    if not verify_login_password(password, encoded):
        login_rate_limiter.record_failure(normalized_email, client_ip)
        return redirect_with_message("/login", error="Credenciales incorrectas")

    login_rate_limiter.clear(normalized_email, client_ip)
    request.session.clear()
    request.session["user_id"] = str(user.id)
    request.session["csrf"] = new_csrf_token()
    return RedirectResponse("/services", status_code=303)


@router.post("/auth/logout", include_in_schema=False)
def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    """Invalida la sesión actual después de comprobar CSRF."""

    verify_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
