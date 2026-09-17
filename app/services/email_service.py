"""Construcción y envío seguro de solicitudes al soporte de Effiwaste."""

from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
import mimetypes
import smtplib
import ssl

from app.config import Settings


ALLOWED_SUPPORT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
}


class SupportEmailConfigurationError(RuntimeError):
    """Indica que el despliegue aún no tiene configurado un servidor SMTP."""


@dataclass(frozen=True)
class SupportAttachment:
    """Adjunto validado y mantenido únicamente en memoria durante el envío."""

    filename: str
    content: bytes


def validate_support_text(subject: str, body: str) -> tuple[str, str]:
    """Normaliza asunto y cuerpo y aplica límites antes de crear el correo."""

    clean_subject = " ".join(subject.split())
    clean_body = body.strip()
    if len(clean_subject) < 3:
        raise ValueError("Escribe un asunto de al menos 3 caracteres")
    if len(clean_subject) > 160:
        raise ValueError("El asunto no puede superar los 160 caracteres")
    if len(clean_body) < 10:
        raise ValueError("Describe la consulta con al menos 10 caracteres")
    if len(clean_body) > 10_000:
        raise ValueError("El mensaje no puede superar los 10.000 caracteres")
    return clean_subject, clean_body


def attachment_extension(filename: str) -> str:
    """Devuelve una extensión autorizada o rechaza el tipo de archivo."""

    dot = filename.rfind(".")
    extension = filename[dot:].lower() if dot >= 0 else ""
    if extension not in ALLOWED_SUPPORT_EXTENSIONS:
        raise ValueError(
            "Formato de adjunto no admitido. Usa imágenes, PDF, Word, Excel, PowerPoint, CSV o TXT"
        )
    return extension


def build_support_message(
    *,
    settings: Settings,
    user_name: str,
    user_email: str,
    hotel_name: str,
    chain_name: str,
    subject: str,
    body: str,
    attachments: list[SupportAttachment],
) -> EmailMessage:
    """Crea un correo con el contexto organizativo derivado por el servidor."""

    subject, body = validate_support_text(subject, body)
    message = EmailMessage()
    message["To"] = settings.support_email
    message["From"] = settings.smtp_from_email or settings.smtp_username or settings.support_email
    message["Reply-To"] = user_email
    message["Subject"] = f"[Effiwaste Suite] {subject}"

    context = (
        "Nueva solicitud desde el centro de ayuda de Effiwaste Suite\n\n"
        f"Usuario: {user_name}\n"
        f"Correo: {user_email}\n"
        f"Hotel: {hotel_name}\n"
        f"Cadena: {chain_name}\n\n"
        f"Asunto: {subject}\n\n"
        f"Mensaje:\n{body}\n"
    )
    message.set_content(context)
    message.add_alternative(
        """
        <html><body style="font-family:Arial,sans-serif;color:#102f4d">
          <h2 style="color:#073b6c">Nueva solicitud de soporte</h2>
          <table cellpadding="6" cellspacing="0" style="border-collapse:collapse">
            <tr><td><strong>Usuario</strong></td><td>{user}</td></tr>
            <tr><td><strong>Correo</strong></td><td>{email}</td></tr>
            <tr><td><strong>Hotel</strong></td><td>{hotel}</td></tr>
            <tr><td><strong>Cadena</strong></td><td>{chain}</td></tr>
          </table>
          <h3 style="margin-top:24px">{subject}</h3>
          <p style="white-space:pre-wrap;line-height:1.6">{body}</p>
        </body></html>
        """.format(
            user=escape(user_name),
            email=escape(user_email),
            hotel=escape(hotel_name),
            chain=escape(chain_name),
            subject=escape(subject),
            body=escape(body),
        ),
        subtype="html",
    )

    for attachment in attachments:
        extension = attachment_extension(attachment.filename)
        mime_type = mimetypes.types_map.get(extension, "application/octet-stream")
        main_type, sub_type = mime_type.split("/", 1)
        message.add_attachment(
            attachment.content,
            maintype=main_type,
            subtype=sub_type,
            filename=attachment.filename,
        )
    return message


def send_support_email(settings: Settings, message: EmailMessage) -> None:
    """Entrega el mensaje mediante SMTP con TLS y autenticación opcionales."""

    if not settings.smtp_host:
        raise SupportEmailConfigurationError("SMTP_HOST no está configurado")

    context = ssl.create_default_context()
    if settings.smtp_use_ssl:
        smtp_connection = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=20, context=context
        )
    else:
        smtp_connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with smtp_connection as smtp:
        if settings.smtp_use_tls and not settings.smtp_use_ssl:
            smtp.starttls(context=context)
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
