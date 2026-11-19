import csv
import hmac
import io
import re
import secrets
import uuid
from datetime import datetime, timedelta
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
    url_for,
)
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import (
    AccessLog,
    Admin,
    Audience,
    AuditLog,
    EmailNotification,
    Event,
    EventAct,
    EventAnalysis,
    EventAttachment,
    EventDelivery,
    EventEnvironment,
    EventGuest,
    EventGuestEnvironment,
    EventParticipant,
    EventWorkflow,
    LookupChallenge,
    MunicipalVisit,
    Participant,
    RegistrationInvite,
    utcnow,
)
from ..rate_limit import rate_limit_retry_after
from ..services import (
    activity_capacity_payload,
    activity_status,
    deliver_pending_notifications,
    decrypt,
    digest,
    encrypt,
    event_participant_summary,
    participant_summary,
    promote_waitlist,
    queue_activity_cancellation,
    queue_due_reminders,
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
invite_manager_required = permission_required("admin", "organizer")
staff_required = permission_required("admin", "reception")
session_required = permission_required("admin", "organizer", "reception")


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
        "location": audience.location,
        "municipality": audience.municipality,
        "responsible": audience.responsible,
        "expectation": audience.expectation,
        "complementary": audience.complementary,
        "active": audience.active,
        **activity_capacity_payload(audience),
    }


def event_payload(event):
    return {
        "id": event.id,
        "name": event.name,
        "date": event.date.isoformat(),
        "time": event.start_time.strftime("%H:%M"),
        "end_time": event.end_time.strftime("%H:%M") if event.end_time else None,
        "location": event.location,
        "modality": event.modality,
        "subject": event.subject,
        "municipality": event.municipality,
        "responsible": event.responsible,
        "teams": event.teams or [],
        "secretariats": event.secretariats or [],
        "expectation": event.expectation,
        "complementary": event.complementary,
        "general_data": event.general_data or {},
        "active": event.active,
        **activity_capacity_payload(event),
    }


WORKFLOW_STAGES = (
    "demand",
    "analysis",
    "agenda",
    "technical_note",
    "ceremonial",
    "secom",
    "report",
)


def workflow_defaults():
    return {
        "demand": {
            "requester": "",
            "origin": "",
            "description": "",
            "received_at": "",
        },
        "analysis": {
            "relevance": "",
            "decision": "Em análise",
            "opinion": "",
            "adjustments": "",
        },
        "agenda": {
            "team": "",
            "political": "",
            "logistics": "",
            "transport": "",
            "security": "",
            "lodging": "",
            "checklist": [],
        },
        "technical_note": {
            "population": "",
            "idh": "",
            "gdp": "",
            "economy": "",
            "investments": "",
            "local_demands": "",
            "trip_report": "",
        },
        "ceremonial": {
            "route": [],
            "plaques": [],
            "acts": [],
            "visits": "",
            "return_note": "",
        },
        "secom": {
            "brief": "",
            "release": "",
            "deliverables": [],
        },
        "report": {
            "authorities": [],
            "geography": "",
            "assessment": "",
        },
    }


def ensure_workflow(event):
    workflow = EventWorkflow.query.filter_by(event_id=event.id).first()
    if workflow:
        return workflow
    workflow = EventWorkflow(
        event_id=event.id,
        **workflow_defaults(),
        stage_status={stage: {"status": "pending", "owner": "", "due_at": ""} for stage in WORKFLOW_STAGES},
        stage_history=[],
    )
    db.session.add(workflow)
    db.session.flush()
    return workflow


def workflow_payload(workflow):
    return {
        "current_stage": workflow.current_stage,
        "closed": workflow.closed,
        "demand": workflow.demand or {},
        "analysis": workflow.analysis or {},
        "agenda": workflow.agenda or {},
        "technical_note": workflow.technical_note or {},
        "ceremonial": workflow.ceremonial or {},
        "secom": workflow.secom or {},
        "report": workflow.report or {},
        "stage_status": workflow.stage_status or {},
        "stage_history": workflow.stage_history or [],
        "updated_at": workflow.updated_at.isoformat() if workflow.updated_at else None,
    }


ACTIVITY_STATES = {"preparing", "open", "closed", "cancelled", "archived"}


def update_activity_settings(activity, data):
    if "capacity" in data:
        value = data["capacity"]
        if value in (None, ""):
            activity.capacity = None
        elif isinstance(value, bool):
            return "A capacidade deve ser um número inteiro positivo."
        else:
            try:
                capacity = int(value)
            except (TypeError, ValueError):
                return "A capacidade deve ser um número inteiro positivo."
            if str(capacity) != str(value).strip() or capacity < 1:
                return "A capacidade deve ser um número inteiro positivo."
            activity.capacity = capacity
    if "status" in data:
        status = data["status"]
        if not isinstance(status, str) or status not in ACTIVITY_STATES:
            return "Situação da atividade inválida."
        activity.lifecycle_status = status
        activity.active = status == "open"
    elif "active" in data:
        if not isinstance(data["active"], bool):
            return "Situação das inscrições inválida."
        activity.lifecycle_status = "open" if data["active"] else "closed"
        activity.active = data["active"]
    return None


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
    elif status_filter == "waitlisted":
        query = query.filter(Participant.status == "waitlisted")
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


@admin_bp.post("/convites")
@invite_manager_required
def create_registration_invite():
    retry_after = rate_limit_retry_after("registration-invite-create", 8, 3600)
    if retry_after:
        response = jsonify({"error": "Muitas solicitações. Tente novamente mais tarde."})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response

    data = json_object()
    cpf = normalize_cpf(data.get("cpf"))
    if not valid_cpf(cpf):
        return jsonify({"error": "Informe um CPF válido."}), 400
    replace_active = data.get("replace_active", False)
    if not isinstance(replace_active, bool):
        return jsonify({"error": "Solicitação inválida."}), 400

    now = utcnow()
    existing_query = RegistrationInvite.query.filter_by(
        cpf_hash=digest(cpf),
        used_at=None,
        invalidated_at=None,
    )
    active = (
        existing_query.filter(RegistrationInvite.expires_at > now)
        .with_for_update()
        .first()
    )
    if active and not replace_active:
        return jsonify(
            {
                "error": (
                    "Já existe um convite ativo para este CPF. "
                    "Confirme a emissão de um novo link para invalidar o anterior."
                ),
                "active_invite": True,
            }
        ), 409

    for invite in existing_query.with_for_update().all():
        invite.invalidated_at = now

    token = secrets.token_urlsafe(32)
    invite = RegistrationInvite(
        cpf_hash=digest(cpf),
        cpf_encrypted=encrypt(cpf),
        token_hash=digest(token),
        created_at=now,
        expires_at=now + timedelta(days=current_app.config["REGISTRATION_INVITE_DAYS"]),
        created_by=current_admin().id,
    )
    db.session.add(invite)
    record_audit(current_admin(), "registration_invite_created", details="Convite de cadastro gerado")
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        active_invite = (
            RegistrationInvite.query.filter_by(
                cpf_hash=digest(cpf),
                used_at=None,
                invalidated_at=None,
            )
            .first()
        )
        if active_invite:
            return jsonify(
                {
                    "error": (
                        "Já existe um convite ativo para este CPF. "
                        "Confirme a emissão de um novo link para invalidar o anterior."
                    ),
                    "active_invite": True,
                }
            ), 409
        raise

    return jsonify(
        {
            "url": url_for("invite_registration_page", token=token),
            "expires_at": invite.expires_at.isoformat(),
        }
    ), 201


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
@session_required
def me():
    admin = current_admin()
    return jsonify(
        {"username": admin.username, "role": admin.role, "csrf_token": session["csrf_token"]}
    )


@admin_bp.post("/logout")
@session_required
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
    audience = Audience(
        name=name,
        date=audience_date,
        time=audience_time,
        location=str(data.get("location") or "")[:200],
        municipality=str(data.get("municipality") or "")[:100],
        responsible=str(data.get("responsible") or "")[:160],
        expectation=str(data.get("expectation") or "")[:5000],
        complementary=str(data.get("complementary") or "")[:5000],
    )
    error = update_activity_settings(audience, data)
    if error:
        return jsonify({"error": error}), 400
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
    for field in ("location", "municipality", "responsible", "expectation", "complementary"):
        if field in data:
            setattr(audience, field, str(data[field] or "")[:5000])
    previous_status = audience.lifecycle_status
    error = update_activity_settings(audience, data)
    if error:
        return jsonify({"error": error}), 400
    promoted = promote_waitlist(audience) if audience.active else []
    cancellation_notice_count = (
        queue_activity_cancellation(audience)
        if previous_status != "cancelled" and audience.lifecycle_status == "cancelled"
        else 0
    )
    record_audit(current_admin(), "update_audience", details=audience.name)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Já existe audiência com este assunto, data e horário."}), 409
    delivery = deliver_pending_notifications() if promoted or cancellation_notice_count else {"failed": 0}
    return jsonify(
        {
            **audience_payload(audience),
            "promoted": len(promoted),
            "notification_failures": delivery["failed"],
            "cancellation_notices": cancellation_notice_count,
        }
    )


@admin_bp.get("/events")
@admin_required
def list_events():
    rows = Event.query.order_by(Event.date.desc(), Event.start_time.desc()).all()
    return jsonify([event_payload(row) for row in rows])


@admin_bp.post("/events")
@admin_required
def create_event():
    data = json_object()
    name = data.get("name", "")
    name = name.strip() if isinstance(name, str) else ""
    location = data.get("location", "")
    location = location.strip() if isinstance(location, str) else ""
    try:
        event_date = datetime.strptime(data.get("date", ""), "%Y-%m-%d").date()
        start_time = datetime.strptime(data.get("time", ""), "%H:%M").time()
        end_time = (
            datetime.strptime(data["end_time"], "%H:%M").time()
            if data.get("end_time")
            else None
        )
    except (TypeError, ValueError):
        return jsonify({"error": "Data e horário do evento inválidos."}), 400
    if not name or len(name) > 160:
        return jsonify({"error": "Informe o nome do evento (até 160 caracteres)."}), 400
    if not location or len(location) > 200:
        return jsonify({"error": "Informe o local do evento (até 200 caracteres)."}), 400
    if end_time and end_time <= start_time:
        return jsonify({"error": "O horário de encerramento deve ser posterior ao início."}), 400
    event = Event(
        name=name,
        date=event_date,
        start_time=start_time,
        end_time=end_time,
        location=location,
        modality=str(data.get("modality") or "event")[:30],
        subject=str(data.get("subject") or name)[:160],
        municipality=str(data.get("municipality") or "")[:100],
        responsible=str(data.get("responsible") or "")[:160],
        teams=[str(v)[:80] for v in data.get("teams", [])] if isinstance(data.get("teams"), list) else [],
        secretariats=[str(v)[:160] for v in data.get("secretariats", [])] if isinstance(data.get("secretariats"), list) else [],
        expectation=str(data.get("expectation") or "")[:5000],
        complementary=str(data.get("complementary") or "")[:5000],
        general_data=data.get("general_data") if isinstance(data.get("general_data"), dict) else {},
    )
    error = update_activity_settings(event, data)
    if error:
        return jsonify({"error": error}), 400
    db.session.add(event)
    ensure_workflow(event)
    record_audit(current_admin(), "create_event", details=f"{name} · {event_date.isoformat()}")
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Este evento já está cadastrado."}), 409
    return jsonify(event_payload(event)), 201


@admin_bp.patch("/events/<event_id>")
@admin_required
def update_event(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        return jsonify({"error": "Evento não encontrado."}), 404
    data = json_object()
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
            return jsonify({"error": "Nome do evento inválido."}), 400
        event.name = name.strip()
    if "date" in data:
        try:
            event.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return jsonify({"error": "Data inválida."}), 400
    if "time" in data:
        try:
            event.start_time = datetime.strptime(data["time"], "%H:%M").time()
        except (TypeError, ValueError):
            return jsonify({"error": "Horário de início inválido."}), 400
    if "end_time" in data:
        try:
            event.end_time = (
                datetime.strptime(data["end_time"], "%H:%M").time()
                if data["end_time"]
                else None
            )
        except (TypeError, ValueError):
            return jsonify({"error": "Horário de encerramento inválido."}), 400
    if "location" in data:
        location = data["location"]
        if not isinstance(location, str) or not location.strip() or len(location.strip()) > 200:
            return jsonify({"error": "Local do evento inválido."}), 400
        event.location = location.strip()
    for field in ("subject", "municipality", "responsible", "expectation", "complementary"):
        if field in data:
            setattr(event, field, str(data[field] or "")[:5000])
    if "modality" in data:
        modality = str(data["modality"] or "")
        if modality not in {"visit", "event", "audience"}:
            return jsonify({"error": "Modalidade inválida."}), 400
        event.modality = modality
    if "teams" in data:
        if not isinstance(data["teams"], list):
            return jsonify({"error": "Equipes responsáveis inválidas."}), 400
        event.teams = [str(v)[:80] for v in data["teams"]]
    if "secretariats" in data:
        if not isinstance(data["secretariats"], list):
            return jsonify({"error": "Secretarias inválidas."}), 400
        event.secretariats = [str(v)[:160] for v in data["secretariats"]]
    if "general_data" in data:
        if not isinstance(data["general_data"], dict):
            return jsonify({"error": "Dados gerais inválidos."}), 400
        event.general_data = data["general_data"]
    if event.end_time and event.end_time <= event.start_time:
        return jsonify({"error": "O horário de encerramento deve ser posterior ao início."}), 400
    previous_status = event.lifecycle_status
    error = update_activity_settings(event, data)
    if error:
        return jsonify({"error": error}), 400
    promoted = promote_waitlist(event) if event.active else []
    cancellation_notice_count = (
        queue_activity_cancellation(event)
        if previous_status != "cancelled" and event.lifecycle_status == "cancelled"
        else 0
    )
    record_audit(current_admin(), "update_event", details=event.name)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Já existe evento com este nome, data e horário."}), 409
    delivery = deliver_pending_notifications() if promoted or cancellation_notice_count else {"failed": 0}
    return jsonify(
        {
            **event_payload(event),
            "promoted": len(promoted),
            "notification_failures": delivery["failed"],
            "cancellation_notices": cancellation_notice_count,
        }
    )


def consolidated_event_report(event, workflow):
    demand = workflow.demand or {}
    analysis = workflow.analysis or {}
    agenda = workflow.agenda or {}
    note = workflow.technical_note or {}
    ceremonial = workflow.ceremonial or {}
    secom = workflow.secom or {}
    report = workflow.report or {}
    checklist = agenda.get("checklist", [])
    lines = [
        f"RELATÓRIO DO EVENTO: {event.name}",
        f"{event.date.isoformat()} · {event.location}",
        "",
        "1. DEMANDA E ANÁLISE",
        f"Solicitante: {demand.get('requester') or '—'} ({demand.get('origin') or '—'})",
        f"Descrição: {demand.get('description') or '—'}",
        f"Decisão: {analysis.get('decision') or '—'} | Pertinência: {analysis.get('relevance') or '—'}",
        f"Parecer: {analysis.get('opinion') or '—'}",
        "",
        "2. AGENDA",
        f"Equipe: {agenda.get('team') or '—'}",
        f"Organização política: {agenda.get('political') or '—'}",
        f"Logística: {agenda.get('logistics') or '—'}",
        f"Checklist: {sum(1 for item in checklist if item.get('done'))}/{len(checklist)}",
        "",
        "3. NOTA TÉCNICA",
        f"População: {note.get('population') or '—'} | IDH: {note.get('idh') or '—'} | PIB: {note.get('gdp') or '—'}",
        f"Economia: {note.get('economy') or '—'}",
        f"Investimentos: {note.get('investments') or '—'}",
        f"Demandas locais: {note.get('local_demands') or '—'}",
        f"Relatório de viagem: {note.get('trip_report') or '—'}",
        "",
        "4. CERIMONIAL",
        f"Visitas: {ceremonial.get('visits') or '—'}",
        f"Retorno: {ceremonial.get('return_note') or '—'}",
        "",
        "5. SECOM",
        f"Pauta: {secom.get('brief') or '—'}",
        f"Release: {secom.get('release') or '—'}",
        "",
        "6. AUTORIDADES E GEOGRAFIA",
    ]
    for authority in report.get("authorities", []):
        lines.append(
            f"- {authority.get('name') or '—'}, {authority.get('role') or '—'} "
            f"({authority.get('party') or '—'}), {authority.get('municipality') or '—'}"
        )
    lines.extend([
        f"Geografia política: {report.get('geography') or '—'}",
        f"Avaliação: {report.get('assessment') or '—'}",
    ])
    return "\n".join(lines)


@admin_bp.get("/events/<event_id>/acts")
@admin_required
def list_event_acts(event_id):
    if not db.session.get(Event, event_id):
        return jsonify({"error": "Evento não encontrado."}), 404
    rows = EventAct.query.filter_by(event_id=event_id).order_by(EventAct.created_at).all()
    return jsonify([{
        "id": row.id, "municipality": row.municipality, "secretariat": row.secretariat,
        "act": row.act, "action": row.action, "species": row.species,
        "classification": row.classification, "modality": row.modality,
        "budget_ceiling": float(row.budget_ceiling) if row.budget_ceiling is not None else None,
        "validation_status": row.validation_status, "justification": row.justification, "data": row.data,
    } for row in rows])


@admin_bp.post("/events/<event_id>/acts")
@admin_required
def create_event_act(event_id):
    if not db.session.get(Event, event_id):
        return jsonify({"error": "Evento não encontrado."}), 404
    data = json_object()
    act = EventAct(
        event_id=event_id,
        municipality=str(data.get("municipality") or "")[:100],
        secretariat=str(data.get("secretariat") or "")[:160],
        act=str(data.get("act") or "")[:160],
        action=str(data.get("action") or "")[:240],
        species=str(data.get("species") or "")[:80],
        classification=str(data.get("classification") or "")[:80],
        modality=str(data.get("modality") or "")[:80],
        validation_status=str(data.get("validation_status") or "pending")[:20],
        justification=str(data.get("justification") or "")[:5000],
        data={k: v for k, v in data.items() if k not in {"municipality", "secretariat", "act", "action", "species", "classification", "modality", "validation_status", "justification", "budget_ceiling"}},
    )
    if data.get("budget_ceiling") not in (None, ""):
        try:
            act.budget_ceiling = float(data["budget_ceiling"])
        except (TypeError, ValueError):
            return jsonify({"error": "Teto orçamentário inválido."}), 400
    db.session.add(act)
    record_audit(current_admin(), "create_event_act", details=f"Evento {event_id}")
    db.session.commit()
    return jsonify({"id": act.id}), 201


@admin_bp.delete("/events/<event_id>/acts/<act_id>")
@admin_required
def delete_event_act(event_id, act_id):
    act = EventAct.query.filter_by(event_id=event_id, id=act_id).first()
    if not act:
        return jsonify({"error": "Ato não encontrado."}), 404
    db.session.delete(act)
    record_audit(current_admin(), "delete_event_act", details=f"Ato {act_id}")
    db.session.commit()
    return jsonify({"deleted": True})


@admin_bp.get("/events/<event_id>/guests")
@admin_required
def list_event_guests(event_id):
    if not db.session.get(Event, event_id):
        return jsonify({"error": "Evento não encontrado."}), 404
    rows = EventGuest.query.filter_by(event_id=event_id).order_by(EventGuest.name).all()
    return jsonify([{
        "id": row.id, "origin": row.origin, "name": row.name, "ballot_name": row.ballot_name,
        "role": row.role, "municipality": row.municipality, "party": row.party,
        "political_support": row.political_support, "allowed_environment": row.allowed_environment,
        "allowed_area": row.allowed_area, "representative": row.representative,
        "invitation_status": row.invitation_status, "acceptance_status": row.acceptance_status,
        "phone": decrypt(row.phone_encrypted) if row.phone_encrypted else "",
        "email": decrypt(row.email_encrypted) if row.email_encrypted else "",
    } for row in rows])


@admin_bp.post("/events/<event_id>/guests")
@admin_required
def create_event_guest(event_id):
    if not db.session.get(Event, event_id):
        return jsonify({"error": "Evento não encontrado."}), 404
    data = json_object()
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 150:
        return jsonify({"error": "Informe o nome do convidado."}), 400
    guest = EventGuest(
        event_id=event_id, origin=str(data.get("origin") or "")[:100], name=name,
        ballot_name=str(data.get("ballot_name") or "")[:150],
        phone_encrypted=encrypt(str(data.get("phone") or "")) if data.get("phone") else None,
        email_encrypted=encrypt(str(data.get("email") or "")) if data.get("email") else None,
        role=str(data.get("role") or "")[:120], municipality=str(data.get("municipality") or "")[:100],
        party=str(data.get("party") or "")[:100], political_support=str(data.get("political_support") or "")[:160],
        allowed_environment=str(data.get("allowed_environment") or "")[:160],
        allowed_area=str(data.get("allowed_area") or "")[:160],
        representative=str(data.get("representative") or "")[:150],
        invitation_status=str(data.get("invitation_status") or "not_sent")[:20],
        acceptance_status=str(data.get("acceptance_status") or "pending")[:20],
    )
    db.session.add(guest)
    record_audit(current_admin(), "create_event_guest", details=name)
    db.session.commit()
    return jsonify({"id": guest.id}), 201


@admin_bp.patch("/events/<event_id>/guests/<guest_id>")
@admin_required
def update_event_guest(event_id, guest_id):
    guest = EventGuest.query.filter_by(event_id=event_id, id=guest_id).first()
    if not guest:
        return jsonify({"error": "Convidado não encontrado."}), 404
    data = json_object()
    for field in ("origin", "name", "ballot_name", "role", "municipality", "party", "political_support", "allowed_environment", "allowed_area", "representative", "invitation_status", "acceptance_status"):
        if field in data:
            setattr(guest, field, str(data[field] or "")[:160])
    if "phone" in data:
        guest.phone_encrypted = encrypt(str(data["phone"] or "")) if data["phone"] else None
    if "email" in data:
        guest.email_encrypted = encrypt(str(data["email"] or "")) if data["email"] else None
    if "confirmation" in data:
        guest.confirmation_history = (guest.confirmation_history or []) + [{
            "status": str(data["confirmation"]),
            "by": current_admin().username,
            "at": utcnow().isoformat(),
        }]
    record_audit(current_admin(), "update_event_guest", details=guest.name)
    db.session.commit()
    return jsonify({"id": guest.id, "acceptance_status": guest.acceptance_status})


@admin_bp.delete("/events/<event_id>/guests/<guest_id>")
@admin_required
def delete_event_guest(event_id, guest_id):
    guest = EventGuest.query.filter_by(event_id=event_id, id=guest_id).first()
    if not guest:
        return jsonify({"error": "Convidado não encontrado."}), 404
    db.session.delete(guest)
    record_audit(current_admin(), "delete_event_guest", details=guest.name)
    db.session.commit()
    return jsonify({"deleted": True})


@admin_bp.get("/events/<event_id>/environments")
@admin_required
def list_event_environments(event_id):
    rows = EventEnvironment.query.filter_by(event_id=event_id).order_by(EventEnvironment.name).all()
    return jsonify([{
        "id": row.id, "type": row.type, "name": row.name, "capacity": row.capacity,
        "color": row.color, "representative_allowed": row.representative_allowed,
        "occupied": EventGuestEnvironment.query.filter_by(environment_id=row.id).count(),
    } for row in rows])


@admin_bp.post("/events/<event_id>/environments")
@admin_required
def create_event_environment(event_id):
    if not db.session.get(Event, event_id):
        return jsonify({"error": "Evento não encontrado."}), 404
    data = json_object()
    name = str(data.get("name") or "").strip()
    try:
        capacity = int(data.get("capacity"))
    except (TypeError, ValueError):
        return jsonify({"error": "Capacidade inválida."}), 400
    if not name or capacity < 1:
        return jsonify({"error": "Nome e capacidade são obrigatórios."}), 400
    environment = EventEnvironment(event_id=event_id, type=str(data.get("type") or "")[:50],
        name=name[:160], capacity=capacity, color=str(data.get("color") or "#dc2626")[:20],
        representative_allowed=data.get("representative_allowed") is True)
    db.session.add(environment)
    record_audit(current_admin(), "create_event_environment", details=name)
    db.session.commit()
    return jsonify({"id": environment.id}), 201


@admin_bp.post("/events/<event_id>/guest-environments")
@admin_required
def link_event_guest_environment(event_id):
    data = json_object()
    guest = EventGuest.query.filter_by(event_id=event_id, id=data.get("guest_id")).first()
    environment = EventEnvironment.query.filter_by(event_id=event_id, id=data.get("environment_id")).first()
    if not guest or not environment:
        return jsonify({"error": "Convidado ou ambiente inválido."}), 400
    occupied = EventGuestEnvironment.query.filter_by(environment_id=environment.id).count()
    if occupied >= environment.capacity:
        return jsonify({"error": "A capacidade do ambiente foi atingida."}), 409
    link = EventGuestEnvironment(event_id=event_id, guest_id=guest.id, environment_id=environment.id,
        area=str(data.get("area") or "")[:160], with_representative=data.get("with_representative") is True)
    db.session.add(link)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Convidado já vinculado a este ambiente."}), 409
    record_audit(current_admin(), "link_event_guest_environment", details=f"{guest.name} · {environment.name}")
    db.session.commit()
    return jsonify({"id": link.id}), 201


@admin_bp.get("/visits")
@admin_required
def list_visits():
    rows = MunicipalVisit.query.order_by(MunicipalVisit.date, MunicipalVisit.time).all()
    return jsonify([{
        "id": row.id, "municipality": row.municipality, "activity": row.activity,
        "date": row.date.isoformat(), "time": row.time.strftime("%H:%M"),
        "location": row.location, "responsible": row.responsible, "observations": row.observations,
    } for row in rows])


@admin_bp.post("/visits")
@admin_required
def create_visit():
    data = json_object()
    try:
        visit_date = datetime.strptime(str(data.get("date") or ""), "%Y-%m-%d").date()
        visit_time = datetime.strptime(str(data.get("time") or ""), "%H:%M").time()
    except (TypeError, ValueError):
        return jsonify({"error": "Data e horário da visita inválidos."}), 400
    municipality = str(data.get("municipality") or "").strip()
    activity = str(data.get("activity") or "").strip()
    if not municipality or not activity:
        return jsonify({"error": "Município e atividade são obrigatórios."}), 400
    visit = MunicipalVisit(municipality=municipality[:100], activity=activity[:200], date=visit_date,
        time=visit_time, location=str(data.get("location") or "")[:200],
        responsible=str(data.get("responsible") or "")[:160], observations=str(data.get("observations") or "")[:5000])
    db.session.add(visit)
    record_audit(current_admin(), "create_municipal_visit", details=f"{municipality} · {activity}")
    db.session.commit()
    return jsonify({"id": visit.id}), 201


@admin_bp.get("/events/<event_id>/analyses")
@admin_required
def list_event_analyses(event_id):
    rows = EventAnalysis.query.filter_by(event_id=event_id).order_by(EventAnalysis.created_at.desc()).all()
    return jsonify([{
        "id": row.id, "stage": row.stage, "record_id": row.record_id, "reason": row.reason,
        "origin": row.origin, "responsible": row.responsible, "status": row.status,
        "provision": row.provision, "history": row.history or [],
        "created_at": row.created_at.isoformat(), "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    } for row in rows])


@admin_bp.post("/events/<event_id>/analyses")
@admin_required
def create_event_analysis(event_id):
    if not db.session.get(Event, event_id):
        return jsonify({"error": "Evento não encontrado."}), 404
    data = json_object()
    reason = str(data.get("reason") or "").strip()
    if not reason:
        return jsonify({"error": "Informe o motivo da análise."}), 400
    analysis = EventAnalysis(event_id=event_id, stage=str(data.get("stage") or "")[:40],
        record_id=str(data.get("record_id") or "")[:36] or None, reason=reason[:5000],
        origin=str(data.get("origin") or "")[:160], responsible=str(data.get("responsible") or "")[:160],
        history=[{"action": "created", "by": current_admin().username, "at": utcnow().isoformat()}])
    db.session.add(analysis)
    record_audit(current_admin(), "create_event_analysis", details=reason)
    db.session.commit()
    return jsonify({"id": analysis.id}), 201


@admin_bp.patch("/events/<event_id>/analyses/<analysis_id>")
@admin_required
def resolve_event_analysis(event_id, analysis_id):
    analysis = EventAnalysis.query.filter_by(event_id=event_id, id=analysis_id).first()
    if not analysis:
        return jsonify({"error": "Análise não encontrada."}), 404
    data = json_object()
    if "status" in data:
        if data["status"] not in {"pending", "in_progress", "resolved"}:
            return jsonify({"error": "Situação inválida."}), 400
        analysis.status = data["status"]
    if "provision" in data:
        analysis.provision = str(data["provision"] or "")[:5000]
    if analysis.status == "resolved":
        analysis.resolved_at = utcnow()
    analysis.history = (analysis.history or []) + [{"action": "updated", "status": analysis.status,
        "provision": analysis.provision, "by": current_admin().username, "at": utcnow().isoformat()}]
    db.session.commit()
    return jsonify({"id": analysis.id, "status": analysis.status, "resolved_at": analysis.resolved_at.isoformat() if analysis.resolved_at else None})


@admin_bp.get("/events/<event_id>/attachments")
@admin_required
def list_event_attachments(event_id):
    rows = EventAttachment.query.filter_by(event_id=event_id).order_by(EventAttachment.created_at.desc()).all()
    return jsonify([{"id": row.id, "stage": row.stage, "document_type": row.document_type,
        "original_name": row.original_name, "mime_type": row.mime_type, "size_bytes": row.size_bytes,
        "created_at": row.created_at.isoformat()} for row in rows])


@admin_bp.post("/events/<event_id>/attachments")
@admin_required
def upload_event_attachment(event_id):
    if not db.session.get(Event, event_id):
        return jsonify({"error": "Evento não encontrado."}), 404
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "Selecione um arquivo."}), 400
    allowed = {
        "application/pdf", "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv", "image/jpeg", "image/png", "image/webp", "audio/mpeg", "audio/wav", "audio/ogg",
    }
    if file.mimetype not in allowed:
        return jsonify({"error": "Tipo de arquivo não permitido."}), 400
    original_name = secure_filename(file.filename)
    filename = f"{uuid.uuid4().hex}_{original_name}"
    folder = Path(current_app.config["UPLOAD_FOLDER"]) / "event-attachments"
    folder.mkdir(parents=True, exist_ok=True)
    file.save(folder / filename)
    size = (folder / filename).stat().st_size
    attachment = EventAttachment(event_id=event_id, stage=str(request.form.get("stage") or "")[:40],
        document_type=str(request.form.get("document_type") or "")[:80],
        original_name=original_name[:255], filename=filename, mime_type=file.mimetype,
        size_bytes=size, uploaded_by=current_admin().id)
    db.session.add(attachment)
    record_audit(current_admin(), "upload_event_attachment", details=original_name)
    db.session.commit()
    return jsonify({"id": attachment.id, "original_name": original_name}), 201


@admin_bp.delete("/events/<event_id>/attachments/<attachment_id>")
@admin_required
def delete_event_attachment(event_id, attachment_id):
    attachment = EventAttachment.query.filter_by(event_id=event_id, id=attachment_id).first()
    if not attachment:
        return jsonify({"error": "Anexo não encontrado."}), 404
    path = Path(current_app.config["UPLOAD_FOLDER"]) / "event-attachments" / attachment.filename
    if path.is_file():
        path.unlink()
    db.session.delete(attachment)
    record_audit(current_admin(), "delete_event_attachment", details=attachment.original_name)
    db.session.commit()
    return jsonify({"deleted": True})


@admin_bp.get("/events/<event_id>/deliveries")
@admin_required
def list_event_deliveries(event_id):
    rows = EventDelivery.query.filter_by(event_id=event_id).order_by(EventDelivery.created_at.desc()).all()
    return jsonify([{"id": row.id, "item": row.item, "association": row.association,
        "cnpj": row.cnpj, "observation": row.observation, "photograph": row.photograph,
        "provision": row.provision} for row in rows])


@admin_bp.post("/events/<event_id>/deliveries")
@admin_required
def create_event_delivery(event_id):
    if not db.session.get(Event, event_id):
        return jsonify({"error": "Evento não encontrado."}), 404
    data = json_object()
    item = str(data.get("item") or "").strip()
    if not item:
        return jsonify({"error": "Informe o item da entrega."}), 400
    delivery = EventDelivery(event_id=event_id, item=item[:200],
        association=str(data.get("association") or "")[:160], cnpj=str(data.get("cnpj") or "")[:18],
        observation=str(data.get("observation") or "")[:5000], provision=str(data.get("provision") or "")[:5000])
    db.session.add(delivery)
    record_audit(current_admin(), "create_event_delivery", details=item)
    db.session.commit()
    return jsonify({"id": delivery.id}), 201


@admin_bp.get("/events/<event_id>/workflow")
@admin_required
def get_event_workflow(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        return jsonify({"error": "Evento não encontrado."}), 404
    workflow = ensure_workflow(event)
    db.session.commit()
    return jsonify({
        **workflow_payload(workflow),
        "consolidated_report": consolidated_event_report(event, workflow),
    })


@admin_bp.patch("/events/<event_id>/workflow")
@admin_required
def update_event_workflow(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        return jsonify({"error": "Evento não encontrado."}), 404
    workflow = ensure_workflow(event)
    data = json_object()
    if workflow.closed:
        return jsonify({"error": "O fluxo deste evento já foi encerrado."}), 409

    for key in WORKFLOW_STAGES:
        if key in data:
            value = data[key]
            if not isinstance(value, dict):
                return jsonify({"error": f"Dados inválidos para a etapa {key}."}), 400
            setattr(workflow, key, value)

    if "stage_status" in data:
        value = data["stage_status"]
        if not isinstance(value, dict):
            return jsonify({"error": "Situação das etapas inválida."}), 400
        sanitized_status = {}
        for stage in WORKFLOW_STAGES:
            item = value.get(stage, {})
            if not isinstance(item, dict):
                item = {}
            sanitized_status[stage] = {
                "status": item.get("status") if item.get("status") in {"pending", "in_progress", "completed", "blocked"} else "pending",
                "owner": str(item.get("owner") or "")[:160],
                "due_at": str(item.get("due_at") or "")[:20],
            }
        workflow.stage_status = sanitized_status

    if data.get("advance") is True:
        if workflow.current_stage >= len(WORKFLOW_STAGES) - 1:
            return jsonify({"error": "Já está na última etapa do fluxo."}), 400
        completed_stage = WORKFLOW_STAGES[workflow.current_stage]
        status = workflow.stage_status or {}
        status[completed_stage] = {
            **status.get(completed_stage, {}),
            "status": "completed",
        }
        workflow.stage_status = status
        workflow.current_stage += 1
        workflow.stage_history = (workflow.stage_history or []) + [{
            "stage": completed_stage,
            "action": "completed",
            "by": current_admin().username,
            "at": utcnow().isoformat(),
        }]

    if data.get("close") is True:
        unresolved = EventAnalysis.query.filter_by(event_id=event.id).filter(
            EventAnalysis.status != "resolved"
        ).count()
        if unresolved:
            return jsonify({
                "error": f"Há {unresolved} análise(s) necessária(s) pendente(s). Resolva-as antes de encerrar.",
            }), 409
        workflow.closed = True
        final_stage = WORKFLOW_STAGES[-1]
        status = workflow.stage_status or {}
        status[final_stage] = {
            **status.get(final_stage, {}),
            "status": "completed",
        }
        workflow.stage_status = status
        workflow.stage_history = (workflow.stage_history or []) + [{
            "stage": final_stage,
            "action": "closed",
            "by": current_admin().username,
            "at": utcnow().isoformat(),
        }]

    workflow.updated_at = utcnow()
    record_audit(
        current_admin(),
        "update_event_workflow",
        details=f"{event.name}",
    )
    db.session.commit()
    return jsonify({
        **workflow_payload(workflow),
        "consolidated_report": consolidated_event_report(event, workflow),
    })


@admin_bp.get("/event-participants")
@admin_required
def list_event_participants():
    event_id = request.args.get("event_id", "")
    if not event_id or not db.session.get(Event, event_id):
        return jsonify({"error": "Selecione um evento válido."}), 400
    query = EventParticipant.query.filter_by(event_id=event_id)
    status_filter = request.args.get("status")
    if status_filter == "present":
        query = query.filter(EventParticipant.checked_at.isnot(None))
    elif status_filter == "absent":
        query = query.filter(
            EventParticipant.checked_at.is_(None), EventParticipant.status == "active"
        )
    elif status_filter in {"active", "cancelled", "waitlisted"}:
        query = query.filter(EventParticipant.status == status_filter)
    search = request.args.get("q", "").strip()
    if search:
        term = f"%{search}%"
        filters = [
            EventParticipant.name.ilike(term),
            EventParticipant.city.ilike(term),
            EventParticipant.category.ilike(term),
        ]
        cpf = normalize_cpf(search)
        if cpf:
            filters.append(EventParticipant.cpf_hash == digest(cpf))
        query = query.filter(or_(*filters))
    participants = query.order_by(EventParticipant.name).limit(5000).all()
    return jsonify(
        [event_participant_summary(item, include_private=True) for item in participants]
    )


@admin_bp.get("/events/<event_id>/export")
@admin_required
def export_event_report(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        return jsonify({"error": "Evento não encontrado."}), 404
    rows = EventParticipant.query.filter_by(event_id=event.id).order_by(
        EventParticipant.name
    ).all()
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        ["Nome", "CPF", "Categoria", "Município", "Cargo/Função", "Telefone", "E-mail",
         "Instagram", "Evento", "Data", "Horário", "Entrada", "Situação"]
    )
    for row in rows:
        values = [
            row.name,
            decrypt(row.cpf_encrypted),
            row.category,
            row.city,
            row.role,
            decrypt(row.phone_encrypted),
            decrypt(row.email_encrypted),
            decrypt(row.instagram_encrypted),
            event.name,
            event.date.isoformat(),
            event.start_time.strftime("%H:%M"),
            row.checked_at.isoformat() if row.checked_at else "",
            (
                "Lista de espera" if row.status == "waitlisted"
                else "Cancelada" if row.status == "cancelled"
                else "Presente" if row.checked_at else "Ausente"
            ),
        ]
        writer.writerow(
            [("'" + str(value)) if str(value).startswith(("=", "+", "-", "@")) else value for value in values]
        )
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename="lista-evento.csv"'
    response.headers["Cache-Control"] = "no-store"
    return response


@admin_bp.get("/events/<event_id>/dashboard")
@admin_required
def event_dashboard(event_id):
    event = db.session.get(Event, event_id)
    if not event:
        return jsonify({"error": "Evento não encontrado."}), 404
    query = EventParticipant.query.filter_by(event_id=event_id)
    confirmed = query.filter_by(status="active")
    registered = confirmed.count()
    present = confirmed.filter(EventParticipant.checked_at.isnot(None)).count()
    absent = confirmed.filter(EventParticipant.checked_at.is_(None)).count()
    cancelled = query.filter_by(status="cancelled").count()
    waitlisted = query.filter_by(status="waitlisted").count()
    return jsonify(
        {
            "registered": registered,
            "present": present,
            "absent": absent,
            "cancelled": cancelled,
            "waitlisted": waitlisted,
            "capacity": event.capacity,
            "available_spots": (
                None if event.capacity is None else max(event.capacity - registered, 0)
            ),
            "status": activity_status(event),
            "attendance_percent": round(present * 100 / registered) if registered else 0,
        }
    )


@admin_bp.get("/event-participants/<participant_id>/credential")
@admin_required
def event_participant_credential(participant_id):
    participant = db.session.get(EventParticipant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    if participant.status != "active":
        return jsonify({"error": "Credencial cancelada."}), 409
    return jsonify(
        {
            "credential": decrypt(participant.credential_encrypted),
            "participant": event_participant_summary(participant),
        }
    )


@admin_bp.post("/event-participants/<participant_id>/cancel")
@admin_required
def cancel_event_participant(participant_id):
    participant = db.session.get(EventParticipant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    reason = str(json_object().get("reason", "")).strip()
    if not reason:
        return jsonify({"error": "Informe o motivo do cancelamento."}), 400
    was_confirmed = participant.status == "active"
    event = participant.event
    participant.status = "cancelled"
    promoted = promote_waitlist(event) if was_confirmed else []
    record_audit(current_admin(), "cancel_event_credential", participant.id, reason)
    db.session.commit()
    delivery = deliver_pending_notifications() if promoted else {"failed": 0}
    return jsonify({"ok": True, "promoted": len(promoted), "notification_failures": delivery["failed"]})


@admin_bp.delete("/event-participants/<participant_id>")
@admin_required
def delete_event_participant(participant_id):
    participant = db.session.get(EventParticipant, participant_id)
    if not participant:
        return jsonify({"error": "Participante não encontrado."}), 404
    reason = str(json_object().get("reason", "")).strip()
    if len(reason) < 5:
        return jsonify({"error": "Informe a justificativa de exclusão (mínimo 5 caracteres)."}), 400
    was_confirmed = participant.status == "active"
    event = participant.event
    record_audit(current_admin(), "delete_event_participant", participant.id, reason)
    photo = participant.photo_filename
    AccessLog.query.filter_by(event_participant_id=participant.id).update(
        {AccessLog.event_participant_id: None}, synchronize_session=False
    )
    LookupChallenge.query.filter_by(event_participant_id=participant.id).delete(
        synchronize_session=False
    )
    EmailNotification.query.filter_by(event_participant_id=participant.id).delete(
        synchronize_session=False
    )
    db.session.delete(participant)
    promoted = promote_waitlist(event) if was_confirmed else []
    db.session.commit()
    remove_photo(photo)
    delivery = deliver_pending_notifications() if promoted else {"failed": 0}
    return jsonify({"ok": True, "promoted": len(promoted), "notification_failures": delivery["failed"]})


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
    was_confirmed = participant.status == "active"
    audience = participant.audience
    participant.status = "cancelled"
    promoted = promote_waitlist(audience) if was_confirmed else []
    record_audit(current_admin(), "cancel_credential", participant.id, reason)
    db.session.commit()
    delivery = deliver_pending_notifications() if promoted else {"failed": 0}
    return jsonify({"ok": True, "promoted": len(promoted), "notification_failures": delivery["failed"]})


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
    all_registrations = query
    total_waitlisted = all_registrations.filter_by(status="waitlisted").count()
    total_cancelled = all_registrations.filter_by(status="cancelled").count()
    query = query.filter_by(status="active")
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
        AccessLog.query.filter(
            AccessLog.outcome == "valid", AccessLog.participant_id.isnot(None)
        )
        .order_by(AccessLog.created_at.desc())
        .limit(10)
        .all()
    )
    return jsonify(
        {
            "registered": total,
            "present": present,
            "absent": max(total - present, 0),
            "waitlisted": total_waitlisted,
            "cancelled": total_cancelled,
            "validated_entries": present,
            "invalid_attempts": invalid,
            "attendance_percent": round(present * 100 / total) if total else 0,
            "by_category": dict(categories),
            "by_city": [{"city": city, "count": count} for city, count in cities],
            "recent_entries": [
                {
                    "name": (
                        row.participant.name
                        if row.participant
                        else row.event_participant.name if row.event_participant else "Participante"
                    ),
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
                    "name": (
                        row.participant.name
                        if row.participant
                        else row.event_participant.name if row.event_participant else None
                    ),
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


@admin_bp.get("/notifications")
@admin_required
def notification_status():
    pending = EmailNotification.query.filter_by(sent_at=None).count()
    failed = EmailNotification.query.filter(
        EmailNotification.sent_at.is_(None), EmailNotification.last_error.isnot(None)
    ).count()
    return jsonify({"pending": pending, "failed": failed})


@admin_bp.post("/notifications/process")
@admin_required
def process_notifications():
    queued = queue_due_reminders()
    result = deliver_pending_notifications()
    return jsonify({"queued": queued, **result})


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
            (
                "Lista de espera" if row.status == "waitlisted"
                else "Cancelada" if row.status == "cancelled"
                else "Presente" if row.checked_at else "Ausente"
            ),
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
    was_confirmed = participant.status == "active"
    audience = participant.audience
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
    EmailNotification.query.filter_by(participant_id=participant.id).delete(
        synchronize_session=False
    )
    db.session.delete(participant)
    promoted = promote_waitlist(audience) if was_confirmed else []
    db.session.commit()
    remove_photo(photo)
    delivery = deliver_pending_notifications() if promoted else {"failed": 0}
    return jsonify({"ok": True, "promoted": len(promoted), "notification_failures": delivery["failed"]})
