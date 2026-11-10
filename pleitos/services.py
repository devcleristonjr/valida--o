import hashlib
import hmac
import io
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from .extensions import db
from .models import AccessLog, AuditLog, Participant, utcnow


def _fernet():
    try:
        return Fernet(current_app.config["APP_ENCRYPTION_KEY"].encode("ascii"))
    except (ValueError, TypeError) as error:
        raise RuntimeError("APP_ENCRYPTION_KEY inválida; gere uma chave Fernet.") from error


def encrypt(value):
    if value is None or value == "":
        return None
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value):
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as error:
        raise RuntimeError("Não foi possível descriptografar um dado armazenado.") from error


def digest(value):
    key = current_app.config["DATA_HASH_KEY"].encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def store_photo(upload):
    if not upload or not upload.filename:
        raise ValueError("A fotografia é obrigatória.")
    suffix = Path(secure_filename(upload.filename)).suffix.lower()
    allowed = {
        ".jpg": ("image/jpeg", "JPEG"),
        ".jpeg": ("image/jpeg", "JPEG"),
        ".png": ("image/png", "PNG"),
        ".webp": ("image/webp", "WEBP"),
    }
    if suffix not in allowed or upload.mimetype != allowed[suffix][0]:
        raise ValueError("Envie uma fotografia JPEG, PNG ou WebP.")
    content = upload.stream.read(5 * 1024 * 1024 + 1)
    if not content or len(content) > 5 * 1024 * 1024:
        raise ValueError("A fotografia deve ter até 5 MB.")
    try:
        image = Image.open(io.BytesIO(content))
        if image.format != allowed[suffix][1] or image.width * image.height > 20_000_000:
            raise ValueError("A fotografia tem formato ou dimensões inválidos.")
        image.verify()
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(content)))
        if image.mode != "RGB":
            background = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image)
            image = background
        normalized = io.BytesIO()
        image.save(normalized, format="JPEG", quality=90, optimize=True)
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("O arquivo não corresponde a uma fotografia válida.") from error
    except Image.DecompressionBombWarning as error:
        raise ValueError("A fotografia excede as dimensões permitidas.") from error
    except ValueError:
        raise
    else:
        content = normalized.getvalue()
    if not content:
        raise ValueError("O arquivo não corresponde a uma imagem válida.")
    filename = f"{uuid.uuid4().hex}.jpg"
    destination = Path(current_app.config["UPLOAD_FOLDER"]) / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return filename


def remove_photo(filename):
    if not filename:
        return
    path = (Path(current_app.config["UPLOAD_FOLDER"]) / filename).resolve()
    root = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    if path.parent == root and path.exists():
        path.unlink()


def validate_code(code, method, admin_id=None, manual_reason=None):
    token = str(code or "").strip()
    token_hash = digest(token) if token else None
    participant = Participant.query.filter_by(credential_hash=token_hash).first() if token_hash else None
    if not participant:
        db.session.add(
            AccessLog(
                attempted_code_hash=token_hash,
                outcome="invalid",
                method=method,
                detail="Credencial não encontrada",
            )
        )
        db.session.commit()
        return {"status": "invalid"}, 404
    if participant.status != "active":
        outcome, detail = "cancelled", "Credencial cancelada"
    elif participant.checked_at:
        outcome, detail = "duplicate", "Entrada já registrada"
    else:
        result = db.session.execute(
            update(Participant)
            .where(
                Participant.id == participant.id,
                Participant.checked_at.is_(None),
                Participant.status == "active",
            )
            .values(checked_at=utcnow(), entry_method=method)
        )
        if result.rowcount != 1:
            db.session.rollback()
            participant = db.session.get(Participant, participant.id)
            outcome, detail = "duplicate", "Entrada já registrada"
        else:
            db.session.add(
                AccessLog(
                    participant_id=participant.id,
                    attempted_code_hash=token_hash,
                    outcome="valid",
                    method=method,
                    detail="Entrada liberada" if method != "manual" else manual_reason or "",
                )
            )
            if admin_id:
                db.session.add(
                    AuditLog(
                        admin_id=admin_id,
                        action="manual_check_in" if method == "manual" else "check_in",
                        participant_id=participant.id,
                        details=manual_reason or method,
                    )
                )
            db.session.commit()
            return {"status": "valid", "participant": participant_summary(participant)}, 200
    db.session.add(
        AccessLog(
            participant_id=participant.id,
            attempted_code_hash=token_hash,
            outcome=outcome,
            method=method,
            detail=detail,
        )
    )
    db.session.commit()
    return {"status": outcome, "message": detail}, 409


def participant_summary(participant, include_private=False):
    data = {
        "id": participant.id,
        "name": participant.name,
        "category": participant.category,
        "city": participant.city,
        "role": participant.role,
        "organization": participant.organization,
        "audience": {
            "id": participant.audience.id,
            "name": participant.audience.name,
            "date": participant.audience.date.isoformat(),
            "time": participant.audience.time.strftime("%H:%M"),
        },
        "status": participant.status,
        "checked_at": participant.checked_at.isoformat() if participant.checked_at else None,
        "entry_method": participant.entry_method,
        "created_at": participant.created_at.isoformat(),
    }
    if include_private:
        data.update(
            cpf=decrypt(participant.cpf_encrypted),
            phone=decrypt(participant.phone_encrypted),
            email=decrypt(participant.email_encrypted),
        )
    return data


def send_lookup_code(email, code):
    host = current_app.config.get("SMTP_HOST")
    if not host:
        raise RuntimeError("A consulta por CPF exige configurar o envio de e-mail (SMTP).")
    message = EmailMessage()
    message["Subject"] = "Código para consultar credencial PLEITOS"
    message["From"] = current_app.config.get("SMTP_FROM") or current_app.config.get("SMTP_USERNAME")
    message["To"] = email
    message.set_content(
        f"Seu código de confirmação é {code}. Ele expira em 10 minutos. "
        "Se você não solicitou este código, ignore esta mensagem."
    )
    with smtplib.SMTP(host, int(current_app.config.get("SMTP_PORT", 587)), timeout=15) as server:
        if current_app.config.get("SMTP_USE_TLS", True):
            server.starttls()
        username = current_app.config.get("SMTP_USERNAME")
        if username:
            server.login(username, current_app.config.get("SMTP_PASSWORD", ""))
        server.send_message(message)


def new_lookup_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def lookup_expiry():
    return datetime.now(timezone.utc) + timedelta(minutes=10)
