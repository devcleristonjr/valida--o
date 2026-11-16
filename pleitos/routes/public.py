import re
import secrets
import io
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db
from ..models import (
    Audience,
    Event,
    EventParticipant,
    LookupChallenge,
    Participant,
    RegistrationInvite,
    utcnow,
)
from ..rate_limit import rate_limit_retry_after
from ..services import (
    activity_capacity_payload,
    decrypt,
    deliver_notification,
    digest,
    encrypt,
    event_participant_summary,
    lookup_expiry,
    new_lookup_code,
    participant_summary,
    queue_confirmation,
    registration_counts,
    registration_is_open,
    send_lookup_code,
    store_photo,
)
from ..validation import normalize_cpf, valid_cpf, valid_phone

public_bp = Blueprint("public", __name__, url_prefix="/api")
PARTICIPANT_CATEGORIES = {
    "Imprensa",
    "Convidado",
    "Prefeito",
    "Liderança",
    "Deputado",
    "Vereador",
    "Senador",
}


def _json_error(message, status):
    return jsonify({"error": message}), status


def _json_object():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def _registration_invite(token, lock=False):
    if not isinstance(token, str) or not token or len(token) > 100:
        return None
    query = RegistrationInvite.query.filter(
        RegistrationInvite.token_hash == digest(token),
        RegistrationInvite.expires_at > utcnow(),
        RegistrationInvite.used_at.is_(None),
        RegistrationInvite.invalidated_at.is_(None),
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def _invalid_registration_invite():
    return _json_error("Link inválido ou expirado. Solicite um novo convite à equipe responsável.", 404)


def _rate_limit_error(scope, limit, window_seconds):
    retry_after = rate_limit_retry_after(scope, limit, window_seconds)
    if not retry_after:
        return None
    response = jsonify({"error": "Muitas tentativas. Tente novamente mais tarde."})
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


@public_bp.get("/audiences")
def audiences():
    rows = Audience.query.order_by(Audience.date, Audience.time).all()
    return jsonify(
        [
            {
                "id": item.id,
                "name": item.name,
                "date": item.date.isoformat(),
                "time": item.time.strftime("%H:%M"),
                **activity_capacity_payload(item),
            }
            for item in rows
            if registration_is_open(item)
        ]
    )


@public_bp.get("/events")
def events():
    rows = Event.query.order_by(Event.date, Event.start_time).all()
    return jsonify([event_payload(item) for item in rows])


def event_payload(item):
    return {
        "id": item.id,
        "name": item.name,
        "date": item.date.isoformat(),
        "time": item.start_time.strftime("%H:%M"),
        "end_time": item.end_time.strftime("%H:%M") if item.end_time else None,
        "location": item.location,
        "active": registration_is_open(item),
        **activity_capacity_payload(item),
    }


@public_bp.get("/cadastro/<token>")
def get_invited_registration(token):
    limited = _rate_limit_error("registration-invite-validate", 30, 60)
    if limited:
        return limited
    invite = _registration_invite(token)
    if not invite:
        return _invalid_registration_invite()
    return jsonify({"cpf": decrypt(invite.cpf_encrypted)})


@public_bp.post("/cadastro/<token>")
def register_invited_participant(token):
    limited = _rate_limit_error("registration-invite-submit", 10, 60)
    if limited:
        return limited
    invite = _registration_invite(token, lock=True)
    if not invite:
        return _invalid_registration_invite()

    data = request.form
    name = data.get("name", "").strip()
    category = data.get("category", "").strip()
    city = data.get("city", "").strip()
    role = data.get("role", "").strip()
    phone = data.get("phone", "").strip()
    email = data.get("email", "").strip().lower()
    instagram = data.get("instagram", "").strip()
    activity_type = data.get("activity_type", "").strip()
    activity_id = data.get("activity_id", "").strip()
    if not name or len(name) > 150 or category not in PARTICIPANT_CATEGORIES:
        return _json_error("Nome ou tipo de participante inválido.", 400)
    if not city or len(city) > 100 or not role or len(role) > 120:
        return _json_error("Município e cargo/função são obrigatórios.", 400)
    if not valid_phone(phone):
        return _json_error("Informe um telefone com DDD válido.", 400)
    if not email or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _json_error("Informe um e-mail válido.", 400)
    if len(instagram) > 60:
        return _json_error("O Instagram deve ter até 60 caracteres.", 400)
    if data.get("consent") != "true":
        return _json_error("É necessário confirmar a ciência do aviso de privacidade.", 400)
    if activity_type == "audience":
        activity = Audience.query.filter_by(id=activity_id).with_for_update().first()
        duplicate_message = "Este CPF já está cadastrado nesta audiência."
        participant_model = Participant
        foreign_key = "audience_id"
    elif activity_type == "event":
        activity = Event.query.filter_by(id=activity_id).with_for_update().first()
        duplicate_message = "Este CPF já está cadastrado neste evento."
        participant_model = EventParticipant
        foreign_key = "event_id"
    else:
        return _json_error("Selecione uma audiência ou evento disponível.", 400)
    if not activity or not registration_is_open(activity):
        activity_label = "Audiência" if activity_type == "audience" else "Evento"
        return _json_error(f"{activity_label} indisponível para cadastro.", 400)

    if participant_model.query.filter_by(
        **{foreign_key: activity.id, "cpf_hash": invite.cpf_hash}
    ).first():
        return _json_error(duplicate_message, 409)

    confirmed, _ = registration_counts(activity)
    waitlisted = activity.capacity is not None and confirmed >= activity.capacity
    try:
        filename = store_photo(request.files.get("photo"))
    except ValueError as error:
        return _json_error(str(error), 400)

    credential = secrets.token_urlsafe(32)
    now = utcnow()
    participant_values = {
        "cpf_hash": invite.cpf_hash,
        "cpf_encrypted": invite.cpf_encrypted,
        "category": category,
        "name": name,
        "city": city,
        "role": role,
        "phone_encrypted": encrypt(phone),
        "email_encrypted": encrypt(email),
        "photo_filename": filename,
        "credential_hash": digest(credential),
        "credential_encrypted": encrypt(credential),
        "status": "waitlisted" if waitlisted else "active",
        "consent_at": now,
        "consent_version": current_app.config["PRIVACY_NOTICE_VERSION"],
    }
    if activity_type == "audience":
        participant_values["audience"] = activity
        participant_values["organization"] = data.get("organization", "").strip()[:160] or None
    else:
        participant_values["event"] = activity
        participant_values["instagram_encrypted"] = encrypt(instagram)
    participant = participant_model(**participant_values)

    try:
        claim = db.session.execute(
            update(RegistrationInvite)
            .where(
                RegistrationInvite.id == invite.id,
                RegistrationInvite.expires_at > now,
                RegistrationInvite.used_at.is_(None),
                RegistrationInvite.invalidated_at.is_(None),
            )
            .values(used_at=now)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            db.session.rollback()
            remove_photo(filename)
            return _invalid_registration_invite()
        db.session.add(participant)
        notification = queue_confirmation(participant, waitlisted)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        remove_photo(filename)
        if participant_model.query.filter_by(
            **{foreign_key: activity.id, "cpf_hash": invite.cpf_hash}
        ).first():
            return _json_error(duplicate_message, 409)
        if not _registration_invite(token):
            return _invalid_registration_invite()
        raise
    except SQLAlchemyError:
        db.session.rollback()
        remove_photo(filename)
        raise

    delivered = deliver_notification(notification)
    summary = (
        participant_summary(participant)
        if activity_type == "audience"
        else event_participant_summary(participant)
    )
    response = {
        "credential": None if waitlisted else credential,
        "registration_status": "waitlisted" if waitlisted else "confirmed",
        "notification_sent": delivered,
        "participant": summary,
    }
    if not delivered:
        response["notification_warning"] = (
            "Inscrição salva, mas o e-mail está pendente. Tente novamente mais tarde."
        )
    return jsonify(response), 201


@public_bp.post("/event-participants")
def register_event_participant():
    data = request.form
    cpf = normalize_cpf(data.get("cpf"))
    name = data.get("name", "").strip()
    category = data.get("category", "").strip()
    city = data.get("city", "").strip()
    role = data.get("role", "").strip()
    phone = data.get("phone", "").strip()
    email = data.get("email", "").strip().lower()
    instagram = data.get("instagram", "").strip()
    event_id = data.get("event_id", "").strip()
    if not valid_cpf(cpf):
        return _json_error("Informe um CPF válido.", 400)
    if not name or len(name) > 150 or category not in PARTICIPANT_CATEGORIES:
        return _json_error("Nome ou tipo de participante inválido.", 400)
    if not city or len(city) > 100 or not role or len(role) > 120:
        return _json_error("Município e cargo/função são obrigatórios.", 400)
    if not valid_phone(phone):
        return _json_error("Informe um telefone com DDD válido.", 400)
    if not email or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _json_error("Informe um e-mail válido.", 400)
    if len(instagram) > 60:
        return _json_error("O Instagram deve ter até 60 caracteres.", 400)
    if data.get("consent") != "true":
        return _json_error("É necessário confirmar a ciência do aviso de privacidade.", 400)
    event = Event.query.filter_by(id=event_id).with_for_update().first()
    if not event or not registration_is_open(event):
        return _json_error("Evento indisponível para cadastro.", 400)
    cpf_hash = digest(cpf)
    if EventParticipant.query.filter_by(event_id=event.id, cpf_hash=cpf_hash).first():
        return _json_error("Este CPF já está cadastrado neste evento.", 409)
    confirmed, _ = registration_counts(event)
    waitlisted = event.capacity is not None and confirmed >= event.capacity
    try:
        filename = store_photo(request.files.get("photo"))
    except ValueError as error:
        return _json_error(str(error), 400)

    token = secrets.token_urlsafe(32)
    participant = EventParticipant(
        event=event,
        cpf_hash=cpf_hash,
        cpf_encrypted=encrypt(cpf),
        category=category,
        name=name,
        city=city,
        role=role,
        phone_encrypted=encrypt(phone),
        email_encrypted=encrypt(email),
        instagram_encrypted=encrypt(instagram),
        photo_filename=filename,
        credential_hash=digest(token),
        credential_encrypted=encrypt(token),
        status="waitlisted" if waitlisted else "active",
        consent_at=utcnow(),
        consent_version=current_app.config["PRIVACY_NOTICE_VERSION"],
    )
    db.session.add(participant)
    notification = queue_confirmation(participant, waitlisted)
    try:
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        from ..services import remove_photo

        remove_photo(filename)
        if EventParticipant.query.filter_by(event_id=event.id, cpf_hash=cpf_hash).first():
            return _json_error("Este CPF já está cadastrado neste evento.", 409)
        raise error
    except SQLAlchemyError:
        db.session.rollback()
        from ..services import remove_photo

        remove_photo(filename)
        raise
    delivered = deliver_notification(notification)
    response = {
        "credential": None if waitlisted else token,
        "registration_status": "waitlisted" if waitlisted else "confirmed",
        "notification_sent": delivered,
        "participant": event_participant_summary(participant),
    }
    if not delivered:
        response["notification_warning"] = "Inscrição salva, mas o e-mail está pendente. Tente novamente mais tarde."
    return jsonify(response), 201


@public_bp.post("/event-credentials/lookup")
def start_event_credential_lookup():
    data = _json_object()
    cpf = normalize_cpf(data.get("cpf", ""))
    event_id = str(data.get("event_id", "")).strip()
    if not valid_cpf(cpf) or not event_id:
        return _json_error("Informe um CPF e evento válidos.", 400)
    participant = EventParticipant.query.filter_by(
        event_id=event_id, cpf_hash=digest(cpf), status="active"
    ).first()
    challenge_id = str(uuid.uuid4())
    if participant and participant.email_encrypted:
        now = datetime.now(timezone.utc)
        latest = (
            LookupChallenge.query.filter_by(
                event_participant_id=participant.id, used=False
            )
            .order_by(LookupChallenge.created_at.desc())
            .first()
        )
        if latest and (
            latest.created_at.replace(tzinfo=timezone.utc)
            if latest.created_at.tzinfo is None
            else latest.created_at
        ) > now - timedelta(seconds=60):
            challenge_id = latest.id
        else:
            code = new_lookup_code()
            challenge = LookupChallenge(
                event_participant_id=participant.id,
                code_hash=digest(code),
                expires_at=lookup_expiry(),
            )
            db.session.add(challenge)
            db.session.commit()
            challenge_id = challenge.id
            try:
                send_lookup_code(decrypt(participant.email_encrypted), code)
            except (OSError, RuntimeError) as error:
                current_app.logger.error("Falha ao enviar código de recuperação: %s", error)
                db.session.delete(challenge)
                db.session.commit()
                return _json_error("Consulta temporariamente indisponível. Tente mais tarde.", 503)
    return jsonify(
        {
            "message": "Se houver cadastro com e-mail, enviaremos um código de confirmação.",
            "verification_required": True,
            "challenge_id": challenge_id,
        }
    )


@public_bp.post("/event-credentials/lookup/verify")
def verify_event_credential_lookup():
    data = _json_object()
    challenge = db.session.get(LookupChallenge, data.get("challenge_id", ""))
    code = str(data.get("code", "")).strip()
    if (
        not challenge
        or not challenge.event_participant_id
        or challenge.used
        or (
            challenge.expires_at.replace(tzinfo=timezone.utc)
            if challenge.expires_at.tzinfo is None
            else challenge.expires_at
        )
        <= utcnow()
        or challenge.attempts >= 5
    ):
        return _json_error("Código inválido ou expirado.", 400)
    challenge.attempts += 1
    if not secrets.compare_digest(challenge.code_hash, digest(code)):
        db.session.commit()
        return _json_error("Código inválido ou expirado.", 400)
    challenge.used = True
    participant = db.session.get(EventParticipant, challenge.event_participant_id)
    db.session.commit()
    if not participant or participant.status != "active":
        return _json_error("Credencial indisponível.", 404)
    return jsonify(
        {
            "credential": decrypt(participant.credential_encrypted),
            "participant": event_participant_summary(participant),
        }
    )


@public_bp.post("/participants")
def register_participant():
    data = request.form
    cpf = normalize_cpf(data.get("cpf"))
    name = data.get("name", "").strip()
    category = data.get("category", "").strip()
    city = data.get("city", "").strip()
    role = data.get("role", "").strip()
    phone = data.get("phone", "").strip()
    email = data.get("email", "").strip().lower()
    audience_id = data.get("audience_id", "").strip()
    if not valid_cpf(cpf):
        return _json_error("Informe um CPF válido.", 400)
    if not name or len(name) > 150 or category not in PARTICIPANT_CATEGORIES:
        return _json_error("Nome ou tipo de participante inválido.", 400)
    if not city or len(city) > 100 or not role or len(role) > 120:
        return _json_error("Município e cargo/função são obrigatórios.", 400)
    if not valid_phone(phone):
        return _json_error("Informe um telefone com DDD válido.", 400)
    if not email or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _json_error("Informe um e-mail válido.", 400)
    if data.get("consent") != "true":
        return _json_error("É necessário confirmar a ciência do aviso de privacidade.", 400)
    audience = Audience.query.filter_by(id=audience_id).with_for_update().first()
    if not audience or not registration_is_open(audience):
        return _json_error("Audiência indisponível para cadastro.", 400)
    if Participant.query.filter_by(audience_id=audience.id, cpf_hash=digest(cpf)).first():
        return _json_error("Este CPF já está cadastrado nesta audiência.", 409)
    confirmed, _ = registration_counts(audience)
    waitlisted = audience.capacity is not None and confirmed >= audience.capacity
    try:
        filename = store_photo(request.files.get("photo"))
    except ValueError as error:
        return _json_error(str(error), 400)

    token = secrets.token_urlsafe(32)
    participant = Participant(
        audience=audience,
        cpf_hash=digest(cpf),
        cpf_encrypted=encrypt(cpf),
        category=category,
        name=name,
        city=city,
        role=role,
        organization=data.get("organization", "").strip()[:160] or None,
        phone_encrypted=encrypt(phone),
        email_encrypted=encrypt(email),
        photo_filename=filename,
        credential_hash=digest(token),
        credential_encrypted=encrypt(token),
        status="waitlisted" if waitlisted else "active",
        consent_at=utcnow(),
        consent_version=current_app.config["PRIVACY_NOTICE_VERSION"],
    )
    db.session.add(participant)
    notification = queue_confirmation(participant, waitlisted)
    try:
        db.session.commit()
    except IntegrityError as error:
        db.session.rollback()
        from ..services import remove_photo

        remove_photo(filename)
        if Participant.query.filter_by(audience_id=audience.id, cpf_hash=digest(cpf)).first():
            return _json_error("Este CPF já está cadastrado nesta audiência.", 409)
        raise error
    except SQLAlchemyError:
        db.session.rollback()
        from ..services import remove_photo

        remove_photo(filename)
        raise
    delivered = deliver_notification(notification)
    response = {
        "credential": None if waitlisted else token,
        "registration_status": "waitlisted" if waitlisted else "confirmed",
        "notification_sent": delivered,
        "participant": participant_summary(participant),
    }
    if not delivered:
        response["notification_warning"] = "Inscrição salva, mas o e-mail está pendente. Tente novamente mais tarde."
    return jsonify(response), 201


@public_bp.post("/credentials/qr")
def credential_qr():
    data = _json_object()
    token = str(data.get("credential", "")).strip()
    participant = Participant.query.filter_by(credential_hash=digest(token)).first() if token else None
    event_participant = (
        EventParticipant.query.filter_by(credential_hash=digest(token)).first()
        if token and not participant
        else None
    )
    if (
        (not participant or participant.status != "active")
        and (not event_participant or event_participant.status != "active")
    ):
        return _json_error("Credencial inválida ou cancelada.", 404)
    import qrcode

    image = qrcode.make(token)
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    response = current_app.response_class(output.getvalue(), mimetype="image/png")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@public_bp.post("/credentials/lookup")
def start_credential_lookup():
    cpf = normalize_cpf(_json_object().get("cpf", ""))
    if not valid_cpf(cpf):
        return _json_error("Informe um CPF válido.", 400)
    participant = Participant.query.filter_by(cpf_hash=digest(cpf)).first()
    challenge_id = str(uuid.uuid4())
    if participant and participant.email_encrypted:
        now = datetime.now(timezone.utc)
        latest = (
            LookupChallenge.query.filter_by(participant_id=participant.id, used=False)
            .order_by(LookupChallenge.created_at.desc())
            .first()
        )
        if latest and (
            latest.created_at.replace(tzinfo=timezone.utc)
            if latest.created_at.tzinfo is None
            else latest.created_at
        ) > now - timedelta(seconds=60):
            challenge_id = latest.id
        else:
            code = new_lookup_code()
            challenge = LookupChallenge(
                participant_id=participant.id,
                code_hash=digest(code),
                expires_at=lookup_expiry(),
            )
            db.session.add(challenge)
            db.session.commit()
            challenge_id = challenge.id
            try:
                send_lookup_code(decrypt(participant.email_encrypted), code)
            except (OSError, RuntimeError) as error:
                current_app.logger.error("Falha ao enviar código de recuperação: %s", error)
                db.session.delete(challenge)
                db.session.commit()
                return _json_error("Consulta temporariamente indisponível. Tente mais tarde.", 503)
    return jsonify(
        {
            "message": "Se houver cadastro com e-mail, enviaremos um código de confirmação.",
            "verification_required": True,
            "challenge_id": challenge_id,
        }
    )


@public_bp.post("/credentials/lookup/verify")
def verify_credential_lookup():
    data = _json_object()
    challenge = db.session.get(LookupChallenge, data.get("challenge_id", ""))
    code = str(data.get("code", "")).strip()
    if (
        not challenge
        or challenge.used
        or (
            challenge.expires_at.replace(tzinfo=timezone.utc)
            if challenge.expires_at.tzinfo is None
            else challenge.expires_at
        )
        <= utcnow()
        or challenge.attempts >= 5
    ):
        return _json_error("Código inválido ou expirado.", 400)
    challenge.attempts += 1
    if not secrets.compare_digest(challenge.code_hash, digest(code)):
        db.session.commit()
        return _json_error("Código inválido ou expirado.", 400)
    challenge.used = True
    participant = db.session.get(Participant, challenge.participant_id)
    db.session.commit()
    if not participant or participant.status != "active":
        return _json_error("Credencial indisponível.", 404)
    return jsonify(
        {
            "credential": decrypt(participant.credential_encrypted),
            "participant": participant_summary(participant),
        }
    )
