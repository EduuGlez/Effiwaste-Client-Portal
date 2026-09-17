"""Limitación distribuida de intentos de autenticación mediante Redis."""

import hashlib
import hmac
import logging

from redis import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings


logger = logging.getLogger(__name__)


class LoginRateLimiter:
    """Ventana móvil por cuenta/IP y por IP, sin identidades en claro en Redis."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Permite inyectar configuración en pruebas sin abrir conexiones."""

        self.settings = settings or get_settings()
        self._client: Redis | None = None

    def _redis(self) -> Redis:
        """Crea la conexión Redis de forma perezosa y reutilizable."""

        if self._client is None:
            self._client = Redis.from_url(
                self.settings.redis_url,
                socket_connect_timeout=1,
                socket_timeout=1,
                decode_responses=True,
            )
        return self._client

    def _key(self, scope: str, identity: str) -> str:
        """Seudonimiza una identidad para no persistir PII en la clave."""

        digest = hmac.new(
            self.settings.session_secret.encode("utf-8"), identity.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"auth:failures:{scope}:{digest}"

    def _account_key(self, email: str, client_ip: str) -> str:
        """Identifica una cuenta concreta observada desde una IP."""

        return self._key("account", f"{client_ip}|{email.strip().lower()}")

    def _ip_key(self, client_ip: str) -> str:
        """Identifica el volumen total de fallos originados por una IP."""

        return self._key("ip", client_ip)

    def is_blocked(self, email: str, client_ip: str) -> bool:
        """Indica si la identidad superó el máximo de fallos de la ventana."""

        try:
            account_value, ip_value = self._redis().mget(
                self._account_key(email, client_ip), self._ip_key(client_ip)
            )
            return (
                int(account_value or 0) >= self.settings.login_max_attempts
                or int(ip_value or 0) >= self.settings.login_ip_max_attempts
            )
        except (RedisError, ValueError):
            logger.warning("Redis no disponible para consultar el límite de login", exc_info=True)
            return False

    def record_failure(self, email: str, client_ip: str) -> None:
        """Incrementa el contador y fija su expiración de forma transaccional."""

        try:
            with self._redis().pipeline() as pipeline:
                for key in (self._account_key(email, client_ip), self._ip_key(client_ip)):
                    pipeline.incr(key)
                    pipeline.expire(key, self.settings.login_window_seconds)
                pipeline.execute()
        except RedisError:
            logger.warning("Redis no disponible para registrar un fallo de login", exc_info=True)

    def clear(self, email: str, client_ip: str) -> None:
        """Elimina el contador tras una autenticación correcta."""

        try:
            # El límite global por IP no se borra: evita que un único acceso
            # correcto permita reiniciar un ataque sobre muchas cuentas.
            self._redis().delete(self._account_key(email, client_ip))
        except RedisError:
            logger.warning("Redis no disponible para limpiar el límite de login", exc_info=True)


login_rate_limiter = LoginRateLimiter()
