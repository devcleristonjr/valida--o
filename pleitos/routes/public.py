import re
import secrets
import io
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import SQLAlchemyError

from ..extensions import db
from ..models import Audience, LookupChallenge, Participant, utcnow
from ..services import (
    decrypt,
    digest,
    encrypt,
    lookup_expiry,
    new_lookup_code,
    participant_summary,
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


@public_bp.get("/audiences")
def audiences():
    rows = Audience.query.filter_by(active=True).order_by(Audience.date, Audience.time).all()
    return jsonify(
        [
            {
                "id": item.id,
                "name": item.name,
                "date": item.date.isoformat(),
                "time": item.time.strftime("%H:%M"),
            }
            for item in rows
        ]
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
    if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        return _json_error("Informe um e-mail válido.", 400)
    if data.get("consent") != "true":
        return _json_error("É necessário confirmar a ciência do aviso de privacidade.", 400)
    audience = db.session.get(Audience, audience_id)
    if not audience or not audience.active:
        return _json_error("Audiência indisponível para cadastro.", 400)
    if Participant.query.filter_by(audience_id=audience.id, cpf_hash=digest(cpf)).first():
        return _json_error("Este CPF já está cadastrado nesta audiência.", 409)
    try:
        filename = store_photo(request.files.get("photo"))
    except ValueError as error:
        return _json_error(str(error), 400)

    token = secrets.token_urlsafe(32)
    participant = Participant(
        audience_id=audience.id,
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
        consent_at=utcnow(),
        consent_version=current_app.config["PRIVACY_NOTICE_VERSION"],
    )
    db.session.add(participant)
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
    return (
        jsonify(
            {
                "credential": token,
                "participant": participant_summary(participant),
            }
        ),
        201,
    )


@public_bp.post("/credentials/qr")
def credential_qr():
    data = _json_object()
    token = str(data.get("credential", "")).strip()
    participant = Participant.query.filter_by(credential_hash=digest(token)).first() if token else None
    if not participant or participant.status != "active":
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
