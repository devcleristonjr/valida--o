import csv
import hmac
import io
import re
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint,
    current_app,
    jsonify,
    make_response,
    request,
    send_file,
    session,
)
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash

from ..extensions import db
from ..models import AccessLog, Admin, Audience, AuditLog, Participant
from ..services import (
    decrypt,
    digest,
    encrypt,
    participant_summary,
    remove_photo,
    validate_code,
)
from ..validation import normalize_cpf, valid_cpf, valid_phone

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


def json_object():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def current_admin():
    admin_id = session.get("admin_id")
    return db.session.get(Admin, admin_id) if admin_id else None


def permission_required(*roles):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            admin = current_admin()
            if not admin or not admin.active:
                session.clear()
                return jsonify({"error": "Autenticação administrativa necessária."}), 401
            if admin.role not in roles:
                return jsonify({"error": "Permissão insuficiente."}), 403
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                supplied = request.headers.get("X-CSRF-Token", "")
                expected = session.get("csrf_token", "")
                if not expected or not supplied or not hmac.compare_digest(supplied, expected):
                    return jsonify({"error": "Token de segurança inválido."}), 403
            return function(*args, **kwargs)

        return wrapped

    return decorate


admin_required = permission_required("admin")
staff_required = permission_required("admin", "reception")


def record_audit(admin, action, participant_id=None, details=""):
    db.session.add(
        AuditLog(
            admin_id=admin.id,
            action=action,
            participant_id=participant_id,
            details=details[:500],
        )
    )


def audience_payload(audience):
    return {
        "id": audience.id,
        "name": audience.name,
        "date": audience.date.isoformat(),
        "time": audience.time.strftime("%H:%M"),
        "active": audience.active,
    }


def _participant_query():
    query = Participant.query.join(Audience)
    audience_id = request.args.get("audience_id")
    if audience_id:
        query = query.filter(Participant.audience_id == audience_id)
    date_filter = request.args.get("date")
    if date_filter:
        try:
            query = query.filter(Audience.date == datetime.strptime(date_filter, "%Y-%m-%d").date())
        except ValueError:
            query = query.filter(Participant.id == "")
    status_filter = request.args.get("status")
    if status_filter == "present":
        query = query.filter(Participant.checked_at.isnot(None))
    elif status_filter == "absent":
        query = query.filter(Participant.checked_at.is_(None), Participant.status == "active")
    elif status_filter in {"active", "cancelled"}:
        query = query.filter(Participant.status == status_filter)
    search = request.args.get("q", "").strip()
    if search:
        term = f"%{search}%"
        filters = [
            Participant.name.ilike(term),
            Participant.city.ilike(term),
            Participant.category.ilike(term),
        ]
        cpf = normalize_cpf(search)
        if cpf:
            filters.append(Participant.cpf_hash == digest(cpf))
        query = query.filter(or_(*filters))
    return query.order_by(Audience.date.desc(), Participant.name)


@admin_bp.post("/login")
def login():
    data = json_object()
    username = data.get("username", "")
    password = data.get("password", "")
    username = username.strip() if isinstance(username, str) else ""
    password = password if isinstance(password, str) else ""
    admin = Admin.query.filter_by(username=username).first()
    if not admin or not admin.active or not check_password_hash(admin.password_hash, password):
        return jsonify({"error": "Usuário ou senha inválidos."}), 401
    session.clear()
    session["admin_id"] = admin.id
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True
    record_audit(admin, "login", details="Login administrativo")
    db.session.commit()
    response = jsonify(
        {"username": admin.username, "role": admin.role, "csrf_token": session["csrf_token"]}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_bp.get("/me")
@staff_required
def me():
    admin = current_admin()
    return jsonify(
        {"username": admin.username, "role": admin.role, "csrf_token": session["csrf_token"]}
    )


@admin_bp.post("/logout")
@staff_required
def logout():
    admin = current_admin()
    record_audit(admin, "logout", details="Logout administrativo")
    db.session.commit()
    session.clear()
    return jsonify({"ok": True})


@admin_bp.get("/audiences")
@staff_required
def list_audiences():
    rows = Audience.query.order_by(Audience.date.desc(), Audience.time.desc()).all()
    return jsonify([audience_payload(row) for row in rows])


@admin_bp.post("/audiences")
@admin_required
def create_audience():
    data = json_object()
    name = data.get("name", "")
    name = name.strip() if isinstance(name, str) else ""
    try:
        audience_date = datetime.strptime(data.get("date", ""), "%Y-%m-%d").date()
        audience_time = datetime.strptime(data.get("time", ""), "%H:%M").time()
    except (TypeError, ValueError):
        return jsonify({"error": "Data e horário inválidos."}), 400
    if not name or len(name) > 160:
        return jsonify({"error": "Informe o assunto da audiência (até 160 caracteres)."}), 400
    audience = Audience(name=name, date=audience_date, time=audience_time)
    db.session.add(audience)
    admin = current_admin()
    record_audit(admin, "create_audience", details=f"{name} · {audience_date.isoformat()}")
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Esta audiência já está cadastrada."}), 409
    return jsonify(audience_payload(audience)), 201


@admin_bp.patch("/audiences/<audience_id>")
@admin_required
def update_audience(audience_id):
    audience = db.session.get(Audience, audience_id)
    if not audience:
        return jsonify({"error": "Audiência não encontrada."}), 404
    data = json_object()
    name = data.get("name")
    if name is not None:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
            return jsonify({"error": "Assunto da audiência inválido."}), 400
        audience.name = name.strip()
    if "date" in data:
        try:
            audience.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return jsonify({"error": "Data inválida."}), 400
    if "time" in data:
        try:
            audience.time = datetime.strptime(data["time"], "%H:%M").time()
        except (TypeError, ValueError):
            return jsonify({"error": "Horário inválido."}), 400
    if "active" in data and isinstance(data["active"], bool):
        audience.active = data["active"]
    record_audit(current_admin(), "update_audience", details=audience.name)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Já existe audiência com este assunto, data e horário."}), 409
    return jsonify(audience_payload(audience))


@admin_bp.get("/participants")
@admin_required
def list_participants():
    participants = _participant_query().limit(5000).all()
    return jsonify([participant_summary(item, include_private=True) for item in participants])


@admin_bp.patch("/participants/<participant_id>")
@admin_required
def update_participant(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    data = json_object()
    string_fields = {
        "name": (150, False),
        "category": (40, False),
        "city": (100, False),
        "role": (120, False),
        "organization": (160, True),
    }
    for field, (limit, optional) in string_fields.items():
        if field in data:
            value = str(data[field]).strip()
            if (not value and not optional) or len(value) > limit:
                return jsonify({"error": f"Campo {field} inválido."}), 400
            setattr(participant, field, value or None if optional else value)
    if "category" in data and participant.category not in {
        "Imprensa", "Convidado", "Prefeito", "Liderança", "Deputado", "Vereador", "Senador"
    }:
        return jsonify({"error": "Categoria inválida."}), 400
    if "cpf" in data:
        cpf = normalize_cpf(data["cpf"])
        if not valid_cpf(cpf):
            return jsonify({"error": "CPF inválido."}), 400
        participant.cpf_hash = digest(cpf)
        participant.cpf_encrypted = encrypt(cpf)
    if "phone" in data:
        if not valid_phone(data["phone"]):
            return jsonify({"error": "Telefone inválido."}), 400
        participant.phone_encrypted = encrypt(data["phone"].strip())
    if "email" in data:
        email = str(data["email"]).strip().lower()
        if email and (
            len(email) > 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        ):
            return jsonify({"error": "E-mail inválido."}), 400
        participant.email_encrypted = encrypt(email)
    admin = current_admin()
    record_audit(admin, "update_participant", participant.id, "Cadastro atualizado")
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "CPF já cadastrado nesta audiência."}), 409
    return jsonify(participant_summary(participant, include_private=True))


@admin_bp.post("/participants/<participant_id>/cancel")
@admin_required
def cancel_participant(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    data = json_object()
    reason = str(data.get("reason", "")).strip()
    if not reason:
        return jsonify({"error": "Informe o motivo do cancelamento."}), 400
    participant.status = "cancelled"
    record_audit(current_admin(), "cancel_credential", participant.id, reason)
    db.session.commit()
    return jsonify({"ok": True})


@admin_bp.get("/participants/<participant_id>/photo")
@admin_required
def participant_photo(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    path = (Path(current_app.config["UPLOAD_FOLDER"]) / participant.photo_filename).resolve()
    if path.parent != Path(current_app.config["UPLOAD_FOLDER"]).resolve() or not path.is_file():
        return jsonify({"error": "Fotografia indisponível."}), 404
    return send_file(path, conditional=True, max_age=0)


@admin_bp.get("/participants/<participant_id>/credential")
@admin_required
def participant_credential(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    if participant.status != "active":
        return jsonify({"error": "Credencial cancelada."}), 409
    return jsonify(
        {
            "credential": decrypt(participant.credential_encrypted),
            "participant": participant_summary(participant),
        }
    )


@admin_bp.post("/check-in")
@staff_required
def check_in():
    data = json_object()
    method = data.get("method", "qr")
    if method not in {"qr", "usb"}:
        return jsonify({"error": "Método de leitura inválido."}), 400
    result, status = validate_code(data.get("code", ""), method, current_admin().id)
    return jsonify(result), status


@admin_bp.post("/participants/<participant_id>/manual-check-in")
@staff_required
def manual_check_in(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    data = json_object()
    reason = str(data.get("reason", "")).strip()
    if len(reason) < 5 or len(reason) > 500:
        return jsonify({"error": "Justificativa obrigatória (5 a 500 caracteres)."}), 400
    result, status = validate_code(
        decrypt(participant.credential_encrypted), "manual", current_admin().id, reason
    )
    return jsonify(result), status


@admin_bp.get("/dashboard")
@staff_required
def dashboard():
    audience_id = request.args.get("audience_id")
    query = Participant.query
    if audience_id:
        query = query.filter_by(audience_id=audience_id)
    total = query.count()
    present = query.filter(Participant.checked_at.isnot(None)).count()
    invalid = AccessLog.query.filter_by(outcome="invalid").count()
    categories = (
        query.with_entities(Participant.category, func.count(Participant.id))
        .group_by(Participant.category)
        .all()
    )
    cities = (
        query.with_entities(Participant.city, func.count(Participant.id))
        .group_by(Participant.city)
        .order_by(func.count(Participant.id).desc())
        .limit(20)
        .all()
    )
    recent = (
        AccessLog.query.filter_by(outcome="valid")
        .order_by(AccessLog.created_at.desc())
        .limit(10)
        .all()
    )
    return jsonify(
        {
            "registered": total,
            "present": present,
            "absent": max(total - present, 0),
            "validated_entries": present,
            "invalid_attempts": invalid,
            "attendance_percent": round(present * 100 / total) if total else 0,
            "by_category": dict(categories),
            "by_city": [{"city": city, "count": count} for city, count in cities],
            "recent_entries": [
                {
                    "name": row.participant.name if row.participant else "Participante",
                    "at": row.created_at.isoformat(),
                    "method": row.method,
                }
                for row in recent
            ],
        }
    )


@admin_bp.get("/history")
@admin_required
def history():
    access = AccessLog.query.order_by(AccessLog.created_at.desc()).limit(500).all()
    audits = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(500).all()
    return jsonify(
        {
            "access": [
                {
                    "outcome": row.outcome,
                    "method": row.method,
                    "detail": row.detail,
                    "name": row.participant.name if row.participant else None,
                    "at": row.created_at.isoformat(),
                }
                for row in access
            ],
            "admin": [
                {
                    "action": row.action,
                    "details": row.details,
                    "at": row.created_at.isoformat(),
                }
                for row in audits
            ],
        }
    )


@admin_bp.get("/export")
@admin_required
def export_report():
    rows = _participant_query().all()
    headers = [
        "Nome", "Categoria", "Município", "Cargo/Função", "Audiência", "Data",
        "Horário", "Horário de entrada", "Situação",
    ]
    values = [
        [
            row.name, row.category, row.city, row.role, row.audience.name,
            row.audience.date.isoformat(), row.audience.time.strftime("%H:%M"),
            row.checked_at.isoformat() if row.checked_at else "",
            "Presente" if row.checked_at else "Ausente",
        ]
        for row in rows
    ]
    output_format = request.args.get("format", "csv").lower()
    if output_format == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        writer.writerow(headers)
        for row in values:
            writer.writerow(
                [
                    ("'" + str(value)) if str(value).startswith(("=", "+", "-", "@")) else value
                    for value in row
                ]
            )
        response = make_response("\ufeff" + output.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = 'attachment; filename="lista-presenca.csv"'
        response.headers["Cache-Control"] = "no-store"
        return response
    if output_format == "xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Lista de presença"
        sheet.append(headers)
        for row in values:
            sheet.append(
                [
                    ("'" + str(value))
                    if str(value).startswith(("=", "+", "-", "@"))
                    else str(value) if value is not None else ""
                    for value in row
                ]
            )
        content = io.BytesIO()
        workbook.save(content)
        content.seek(0)
        return send_file(
            content,
            as_attachment=True,
            download_name="lista-presenca.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    if output_format == "pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
        from xml.sax.saxutils import escape

        content = io.BytesIO()
        document = SimpleDocTemplate(
            content,
            pagesize=landscape(letter),
            leftMargin=30,
            rightMargin=30,
            topMargin=30,
            bottomMargin=30,
        )
        table = Table(
            [[escape(str(item)) for item in headers]]
            + [[escape(str(item)) for item in row] for row in values],
            repeatRows=1,
            colWidths=[110, 58, 75, 75, 125, 55, 48, 120, 58],
        )
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#801c2c")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        document.build([table])
        content.seek(0)
        return send_file(content, as_attachment=True, download_name="lista-presenca.pdf", mimetype="application/pdf")
    return jsonify({"error": "Formato deve ser csv, xlsx ou pdf."}), 400


@admin_bp.delete("/participants/<participant_id>")
@admin_required
def delete_participant(participant_id):
    participant = db.session.get(Participant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    data = json_object()
    reason = str(data.get("reason", "")).strip()
    if len(reason) < 5:
        return jsonify({"error": "Informe a justificativa de exclusão (mínimo 5 caracteres)."}), 400
    admin = current_admin()
    record_audit(admin, "delete_participant", participant.id, reason)
    photo = participant.photo_filename
    AccessLog.query.filter_by(participant_id=participant.id).update(
        {AccessLog.participant_id: None}, synchronize_session=False
    )
    from ..models import LookupChallenge

    LookupChallenge.query.filter_by(participant_id=participant.id).delete(
        synchronize_session=False
    )
    db.session.delete(participant)
    db.session.commit()
    remove_photo(photo)
    return jsonify({"ok": True})
