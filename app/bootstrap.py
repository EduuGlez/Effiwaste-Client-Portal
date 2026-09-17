"""Inicialización del esquema y de las cuentas de arranque."""

import logging

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, init_database
from app.models import Chain, Hotel, User, UserRole
from app.security import hash_password


logger = logging.getLogger(__name__)


def initialize_application() -> None:
    """Prepara almacenamiento, esquema y usuarios iniciales.

    Una contraseña existente nunca se reemplaza durante el arranque. Rotar una
    variable de entorno no debe cambiar credenciales de forma silenciosa.
    """

    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_database()

    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.email == settings.admin_email.lower()))
        if admin is None:
            db.add(
                User(
                    email=settings.admin_email.lower(),
                    full_name="Administrador",
                    password_hash=hash_password(settings.admin_password),
                    role=UserRole.ADMIN,
                )
            )
        elif admin.role != UserRole.ADMIN:
            raise RuntimeError("ADMIN_EMAIL pertenece a un usuario que no es administrador")

        if settings.seed_demo_data:
            chain = db.scalar(select(Chain).where(Chain.name == "Cadena Demo"))
            if chain is None:
                chain = Chain(name="Cadena Demo")
                db.add(chain)
                db.flush()

            hotel = db.scalar(select(Hotel).where(Hotel.code == "DEMO-001"))
            if hotel is None:
                hotel = Hotel(name="Hotel Demo", code="DEMO-001", chain_id=chain.id)
                db.add(hotel)
                db.flush()

            demo = db.scalar(select(User).where(User.email == settings.demo_user_email.lower()))
            if demo is None:
                db.add(
                    User(
                        email=settings.demo_user_email.lower(),
                        full_name="Usuario Demo",
                        password_hash=hash_password(settings.demo_user_password),
                        role=UserRole.USER,
                        chain_id=chain.id,
                        hotel_id=hotel.id,
                    )
                )
        else:
            logger.info("La creación de datos de demostración está desactivada")
        db.commit()
