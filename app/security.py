"""Primitivas de contraseña y protección CSRF."""

import hmac
import secrets

from pwdlib import PasswordHash


password_hash = PasswordHash.recommended()
# Siempre verificamos un hash Argon2, incluso cuando el correo no existe. Esto
# reduce la diferencia temporal que permitiría enumerar usuarios válidos.
_dummy_password_hash = password_hash.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    """Genera un hash adaptativo usando la recomendación actual de ``pwdlib``."""

    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    """Verifica un hash y convierte entradas corruptas en un fallo seguro."""

    try:
        return password_hash.verify(password, encoded)
    except Exception:
        return False


def verify_login_password(password: str, encoded: str | None) -> bool:
    """Verifica credenciales con coste parecido para usuarios válidos e inexistentes."""

    candidate = encoded or _dummy_password_hash
    verified = verify_password(password, candidate)
    return bool(encoded) and verified


def password_policy_error(password: str) -> str | None:
    """Devuelve la causa de rechazo o ``None`` si la contraseña es suficiente."""

    if len(password) < 12:
        return "La contraseña debe tener al menos 12 caracteres"
    character_groups = (
        any(character.islower() for character in password),
        any(character.isupper() for character in password),
        any(character.isdigit() for character in password),
        any(not character.isalnum() for character in password),
    )
    if sum(character_groups) < 3:
        return "La contraseña debe combinar al menos tres tipos de caracteres"
    return None


def new_csrf_token() -> str:
    """Crea un token CSRF con 256 bits de entropía."""

    return secrets.token_urlsafe(32)


def csrf_matches(expected: str | None, supplied: str | None) -> bool:
    """Compara tokens CSRF en tiempo constante."""

    return bool(expected and supplied and hmac.compare_digest(expected, supplied))
