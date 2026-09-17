from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, init_database
from app.models import Chain, Hotel, User, UserRole
from app.security import hash_password, verify_password


def initialize_application() -> None:
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_database()

    with SessionLocal() as db:
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
        elif not verify_password(settings.admin_password, admin.password_hash):
            # Las credenciales iniciales viven en el entorno Docker y permanecen
            # reproducibles incluso si ya existe el volumen de desarrollo.
            admin.password_hash = hash_password(settings.admin_password)
            admin.role = UserRole.ADMIN
            admin.is_active = True

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
        elif not verify_password(settings.demo_user_password, demo.password_hash):
            demo.password_hash = hash_password(settings.demo_user_password)
            demo.is_active = True
        db.commit()
