"""Composición de la aplicación ASGI de Effiwaste Suite.

Las rutas viven en ``app.routers``. Este módulo solo configura ciclo de vida,
middleware, archivos estáticos y manejadores globales, lo que permite razonar
sobre cada capa de forma aislada y probarla sin un archivo monolítico.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.bootstrap import initialize_application
from app.config import get_settings
from app.middleware import SecurityHeadersMiddleware
from app.routers import admin, public, workspace
from app.web import BASE_DIR


settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Inicializa almacenamiento y base de datos antes de aceptar tráfico."""

    initialize_application()
    yield


def create_app() -> FastAPI:
    """Construye la aplicación y registra sus capas en un orden explícito."""

    application = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.cookie_secure,
        same_site="lax",
        max_age=settings.session_max_age_seconds,
    )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    application.add_middleware(SecurityHeadersMiddleware, enable_hsts=settings.cookie_secure)

    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    application.include_router(public.router)
    application.include_router(workspace.router)
    application.include_router(admin.router)

    @application.exception_handler(401)
    async def unauthorized_handler(_: Request, __: HTTPException):
        """Convierte sesiones caducadas en una navegación limpia al login."""

        return RedirectResponse("/login", status_code=303)

    return application


app = create_app()
