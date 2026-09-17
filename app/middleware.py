"""Middleware de endurecimiento HTTP común a todas las respuestas."""

from starlette.types import ASGIApp


class SecurityHeadersMiddleware:
    """Añade una política de navegador conservadora sin alterar el contenido.

    Se implementa como middleware ASGI pequeño para evitar la sobrecarga y los
    problemas de streaming asociados a ``BaseHTTPMiddleware``.
    """

    def __init__(self, app: ASGIApp, *, enable_hsts: bool = False) -> None:
        """Guarda la siguiente aplicación ASGI y si debe anunciarse HSTS."""

        self.app = app
        self.enable_hsts = enable_hsts

    async def __call__(self, scope, receive, send) -> None:
        """Intercepta el inicio de cada respuesta HTTP para añadir cabeceras."""

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            """Decora el mensaje inicial sin interferir con el streaming."""

            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in headers}

                def add(name: str, value: str) -> None:
                    """Añade una cabecera solo si una capa interior no la fijó."""

                    encoded_name = name.lower().encode("latin-1")
                    if encoded_name not in existing:
                        headers.append((encoded_name, value.encode("latin-1")))

                add("X-Content-Type-Options", "nosniff")
                add("X-Frame-Options", "DENY")
                add("Referrer-Policy", "strict-origin-when-cross-origin")
                add("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                add(
                    "Content-Security-Policy",
                    "default-src 'self'; base-uri 'self'; form-action 'self'; "
                    "frame-ancestors 'none'; object-src 'none'; img-src 'self' data:; "
                    "script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                    "font-src 'self' https://fonts.gstatic.com; connect-src 'self'",
                )
                if self.enable_hsts:
                    add("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
