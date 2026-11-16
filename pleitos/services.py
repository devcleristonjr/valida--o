import hashlib
import hmac
import io
import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from .extensions import db
from .models import (
    AccessLog,
    AuditLog,
    Audience,
    EmailNotification,
    Event,
    EventParticipant,
    Participant,
    utcnow,
)


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
    event_participant = (
        EventParticipant.query.filter_by(credential_hash=token_hash).first()
        if token_hash and not participant
        else None
    )
    if not participant and not event_participant:
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
    attendee = participant or event_participant
    activity = participant.audience if participant else event_participant.event
    if activity.lifecycle_status == "cancelled":
        outcome, detail = "cancelled", "Atividade cancelada"
    elif attendee.status == "waitlisted":
        outcome, detail = "waitlisted", "Participante ainda está na lista de espera"
    elif attendee.status != "active":
        outcome, detail = "cancelled", "Credencial cancelada"
    elif attendee.checked_at:
        outcome, detail = "duplicate", "Entrada já registrada"
    else:
        attendee_model = Participant if participant else EventParticipant
        result = db.session.execute(
            update(attendee_model)
            .where(
                attendee_model.id == attendee.id,
                attendee_model.checked_at.is_(None),
                attendee_model.status == "active",
            )
            .values(checked_at=utcnow(), entry_method=method)
        )
        if result.rowcount != 1:
            db.session.rollback()
            attendee = db.session.get(attendee_model, attendee.id)
            outcome, detail = "duplicate", "Entrada já registrada"
        else:
            db.session.add(
                AccessLog(
                    participant_id=participant.id if participant else None,
                    event_participant_id=event_participant.id if event_participant else None,
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
                        participant_id=attendee.id,
                        details=manual_reason or method,
                    )
                )
            db.session.commit()
            summary = (
                participant_summary(attendee)
                if participant
                else event_participant_summary(attendee)
            )
            return {"status": "valid", "participant": summary}, 200
    db.session.add(
        AccessLog(
            participant_id=participant.id if participant else None,
            event_participant_id=event_participant.id if event_participant else None,
            attempted_code_hash=token_hash,
            outcome=outcome,
            method=method,
            detail=detail,
        )
    )
    db.session.commit()
    return {"status": outcome, "message": detail}, 409


def event_participant_summary(participant, include_private=False):
    data = {
        "id": participant.id,
        "name": participant.name,
        "category": participant.category,
        "city": participant.city,
        "role": participant.role,
        "event": {
            "id": participant.event.id,
            "name": participant.event.name,
            "date": participant.event.date.isoformat(),
            "time": participant.event.start_time.strftime("%H:%M"),
            "end_time": (
                participant.event.end_time.strftime("%H:%M")
                if participant.event.end_time
                else None
            ),
            "location": participant.event.location,
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
            instagram=decrypt(participant.instagram_encrypted),
        )
    return data


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


def send_notification_email(email, subject, body):
    host = current_app.config.get("SMTP_HOST")
    if not host:
        raise RuntimeError("O envio de notificações exige configurar o SMTP.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = current_app.config.get("SMTP_FROM") or current_app.config.get("SMTP_USERNAME")
    message["To"] = email
    message.set_content(body)
    with smtplib.SMTP(host, int(current_app.config.get("SMTP_PORT", 587)), timeout=15) as server:
        if current_app.config.get("SMTP_USE_TLS", True):
            server.starttls()
        username = current_app.config.get("SMTP_USERNAME")
        if username:
            server.login(username, current_app.config.get("SMTP_PASSWORD", ""))
        server.send_message(message)


def activity_today():
    timezone_name = current_app.config.get("ACTIVITY_TIMEZONE", "America/Bahia")
    return datetime.now(ZoneInfo(timezone_name)).date()


def activity_status(activity):
    if activity.lifecycle_status in {"cancelled", "archived", "preparing"}:
        return activity.lifecycle_status
    if activity.date < activity_today():
        return "completed"
    return activity.lifecycle_status


def registration_is_open(activity):
    return activity.active and activity.lifecycle_status == "open" and activity.date >= activity_today()


def registration_counts(activity):
    model = EventParticipant if isinstance(activity, Event) else Participant
    foreign_key = "event_id" if isinstance(activity, Event) else "audience_id"
    query = model.query.filter(getattr(model, foreign_key) == activity.id)
    return (
        query.filter_by(status="active").count(),
        query.filter_by(status="waitlisted").count(),
    )


def activity_capacity_payload(activity):
    confirmed, waiting = registration_counts(activity)
    available = None if activity.capacity is None else max(activity.capacity - confirmed, 0)
    return {
        "status": activity_status(activity),
        "capacity": activity.capacity,
        "confirmed": confirmed,
        "waitlisted": waiting,
        "available_spots": available,
        "registration_open": registration_is_open(activity),
    }


def queue_email(
    email,
    subject,
    body,
    notification_type,
    participant=None,
):
    if not email:
        return None
    notification = EmailNotification(
        participant_id=(
            participant.id
            if isinstance(participant, Participant)
            else None
        ),
        event_participant_id=(
            participant.id
            if isinstance(participant, EventParticipant)
            else None
        ),
        notification_type=notification_type,
        recipient_encrypted=encrypt(email),
        subject_encrypted=encrypt(subject),
        body_encrypted=encrypt(body),
    )
    db.session.add(notification)
    return notification


def deliver_notification(notification):
    notification.attempts += 1
    try:
        send_notification_email(
            decrypt(notification.recipient_encrypted),
            decrypt(notification.subject_encrypted),
            decrypt(notification.body_encrypted),
        )
    except (OSError, RuntimeError, smtplib.SMTPException, ValueError) as error:
        notification.last_error = str(error)[:240]
        current_app.logger.error(
            "Falha no envio de notificação %s (tentativa %s): %s",
            notification.id,
            notification.attempts,
            error,
        )
        db.session.commit()
        return False
    notification.sent_at = utcnow()
    notification.last_error = None
    db.session.commit()
    return True


def deliver_pending_notifications(limit=500):
    pending = (
        EmailNotification.query.filter_by(sent_at=None)
        .order_by(EmailNotification.created_at, EmailNotification.id)
        .limit(limit)
        .all()
    )
    sent = sum(deliver_notification(item) for item in pending)
    return {"sent": sent, "failed": len(pending) - sent, "pending": len(pending) - sent}


def _activity_message(activity):
    if isinstance(activity, Event):
        return (
            f"{activity.name}\nData: {activity.date.isoformat()} às "
            f"{activity.start_time.strftime('%H:%M')}\nLocal: {activity.location}"
        )
    return (
        f"{activity.name}\nData: {activity.date.isoformat()} às "
        f"{activity.time.strftime('%H:%M')}"
    )


def queue_confirmation(participant, waitlisted=False):
    activity = participant.event if isinstance(participant, EventParticipant) else participant.audience
    if waitlisted:
        model = EventParticipant if isinstance(participant, EventParticipant) else Participant
        foreign_key = "event_id" if isinstance(activity, Event) else "audience_id"
        with db.session.no_autoflush:
            position = model.query.filter(
                getattr(model, foreign_key) == activity.id,
                model.status == "waitlisted",
            ).count() + 1
        subject = f"Lista de espera: {activity.name}"
        body = (
            f"Olá, {participant.name}.\n\n"
            f"Sua inscrição foi registrada na lista de espera (posição {position}). "
            "Se uma vaga for liberada, avisaremos por e-mail.\n\n"
            f"{_activity_message(activity)}\n\nPLEITOS · SERIN"
        )
        notification_type = "waitlist"
    else:
        subject = f"Inscrição confirmada: {activity.name}"
        body = (
            f"Olá, {participant.name}.\n\n"
            f"Sua inscrição está confirmada.\n\n{_activity_message(activity)}\n"
            f"Credencial: {decrypt(participant.credential_encrypted)}\n\n"
            "Apresente a credencial na recepção para registrar sua presença.\n\nPLEITOS · SERIN"
        )
        notification_type = "confirmation"
    return queue_email(
        decrypt(participant.email_encrypted),
        subject,
        body,
        notification_type,
        participant,
    )


def promote_waitlist(activity):
    if not registration_is_open(activity):
        return []
    model = EventParticipant if isinstance(activity, Event) else Participant
    foreign_key = "event_id" if isinstance(activity, Event) else "audience_id"
    promoted = []
    while True:
        confirmed, _ = registration_counts(activity)
        if activity.capacity is not None and confirmed >= activity.capacity:
            break
        participant = (
            model.query.filter(
                getattr(model, foreign_key) == activity.id,
                model.status == "waitlisted",
            )
            .order_by(model.created_at, model.id)
            .with_for_update()
            .first()
        )
        if not participant:
            break
        participant.status = "active"
        queue_email(
            decrypt(participant.email_encrypted),
            f"Vaga confirmada: {activity.name}",
            (
                f"Olá, {participant.name}.\n\nUma vaga foi liberada e sua inscrição "
                f"está confirmada.\n\n{_activity_message(activity)}\n"
                f"Credencial: {decrypt(participant.credential_encrypted)}\n\n"
                "Apresente a credencial na recepção para registrar sua presença.\n\nPLEITOS · SERIN"
            ),
            "promoted",
            participant,
        )
        promoted.append(participant)
    return promoted


def queue_activity_cancellation(activity):
    model = EventParticipant if isinstance(activity, Event) else Participant
    foreign_key = "event_id" if isinstance(activity, Event) else "audience_id"
    participants = model.query.filter(
        getattr(model, foreign_key) == activity.id,
        model.status.in_(("active", "waitlisted")),
        model.email_encrypted.isnot(None),
    ).all()
    for participant in participants:
        queue_email(
            decrypt(participant.email_encrypted),
            f"Atividade cancelada: {activity.name}",
            (
                f"Olá, {participant.name}.\n\nA atividade abaixo foi cancelada. "
                "A equipe da SERIN entrará em contato caso haja novas orientações.\n\n"
                f"{_activity_message(activity)}\n\nPLEITOS · SERIN"
            ),
            "activity_cancelled",
            participant,
        )
    return len(participants)


def queue_due_reminders():
    today = activity_today()
    queued = 0
    for days_before in (7, 1):
        reminder_date = today + timedelta(days=days_before)
        notification_type = f"reminder_{days_before}d"
        activities = [
            (Audience, Participant, "audience_id", "audience"),
            (Event, EventParticipant, "event_id", "event"),
        ]
        for activity_model, participant_model, foreign_key, kind in activities:
            rows = (
                participant_model.query.join(activity_model)
                .filter(
                    getattr(activity_model, "date") == reminder_date,
                    getattr(activity_model, "lifecycle_status").in_(("open", "closed")),
                    getattr(participant_model, "status") == "active",
                    getattr(participant_model, "email_encrypted").isnot(None),
                )
                .all()
            )
            for participant in rows:
                exists = EmailNotification.query.filter_by(
                    participant_id=participant.id if kind == "audience" else None,
                    event_participant_id=participant.id if kind == "event" else None,
                    notification_type=notification_type,
                ).first()
                if exists:
                    continue
                activity = participant.audience if kind == "audience" else participant.event
                queue_email(
                    decrypt(participant.email_encrypted),
                    f"Lembrete: {activity.name} em {days_before} dia(s)",
                    (
                        f"Olá, {participant.name}.\n\nEste é um lembrete da sua inscrição. "
                        f"A atividade ocorrerá em {days_before} dia(s).\n\n"
                        f"{_activity_message(activity)}\n"
                        f"Credencial: {decrypt(participant.credential_encrypted)}\n\n"
                        "Apresente sua credencial na recepção.\n\nPLEITOS · SERIN"
                    ),
                    notification_type,
                    participant,
                )
                queued += 1
    if queued:
        db.session.commit()
    return queued


def new_lookup_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def lookup_expiry():
    return datetime.now(timezone.utc) + timedelta(minutes=10)
