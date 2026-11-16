import uuid
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from .extensions import db


def utcnow():
    return datetime.now(timezone.utc)


class Audience(db.Model):
    __tablename__ = "audiences"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(160), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.Time, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    lifecycle_status = db.Column(db.String(16), nullable=False, default="open")
    capacity = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        db.UniqueConstraint("name", "date", "time", name="uq_audience_schedule"),
    )


class Event(db.Model):
    __tablename__ = "events"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(160), nullable=False)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time)
    location = db.Column(db.String(200), nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    lifecycle_status = db.Column(db.String(16), nullable=False, default="open")
    capacity = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    __table_args__ = (
        db.UniqueConstraint("name", "date", "start_time", name="uq_event_schedule"),
    )


class Participant(db.Model):
    __tablename__ = "participants"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    audience_id = db.Column(
        db.String(36), db.ForeignKey("audiences.id", ondelete="RESTRICT"), nullable=False
    )
    cpf_hash = db.Column(db.String(64), nullable=False)
    cpf_encrypted = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(40), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(120), nullable=False)
    organization = db.Column(db.String(160))
    phone_encrypted = db.Column(db.Text, nullable=False)
    email_encrypted = db.Column(db.Text)
    photo_filename = db.Column(db.String(80), nullable=False)
    credential_hash = db.Column(db.String(64), nullable=False, unique=True)
    credential_encrypted = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="active")
    consent_at = db.Column(db.DateTime(timezone=True), nullable=False)
    consent_version = db.Column(db.String(30), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    checked_at = db.Column(db.DateTime(timezone=True))
    entry_method = db.Column(db.String(20))
    audience = db.relationship("Audience")
    __table_args__ = (
        db.UniqueConstraint("audience_id", "cpf_hash", name="uq_participant_cpf_audience"),
        db.Index("ix_participant_audience_status", "audience_id", "status"),
    )


class EventParticipant(db.Model):
    __tablename__ = "event_participants"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_id = db.Column(
        db.String(36), db.ForeignKey("events.id", ondelete="RESTRICT"), nullable=False
    )
    cpf_hash = db.Column(db.String(64), nullable=False)
    cpf_encrypted = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(40), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(120), nullable=False)
    phone_encrypted = db.Column(db.Text, nullable=False)
    email_encrypted = db.Column(db.Text)
    instagram_encrypted = db.Column(db.Text)
    photo_filename = db.Column(db.String(80), nullable=False)
    credential_hash = db.Column(db.String(64), nullable=False, unique=True)
    credential_encrypted = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="active")
    consent_at = db.Column(db.DateTime(timezone=True), nullable=False)
    consent_version = db.Column(db.String(30), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    checked_at = db.Column(db.DateTime(timezone=True))
    entry_method = db.Column(db.String(20))
    event = db.relationship("Event")
    __table_args__ = (
        db.UniqueConstraint("event_id", "cpf_hash", name="uq_event_participant_cpf"),
        db.Index("ix_event_participant_event_status", "event_id", "status"),
    )


class AccessLog(db.Model):
    __tablename__ = "access_logs"
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(
        db.String(36), db.ForeignKey("participants.id", ondelete="SET NULL")
    )
    event_participant_id = db.Column(
        db.String(36), db.ForeignKey("event_participants.id", ondelete="SET NULL")
    )
    attempted_code_hash = db.Column(db.String(64))
    outcome = db.Column(db.String(20), nullable=False)
    method = db.Column(db.String(20), nullable=False)
    detail = db.Column(db.String(240), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    participant = db.relationship("Participant")
    event_participant = db.relationship("EventParticipant")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.String(36), nullable=False)
    action = db.Column(db.String(60), nullable=False)
    participant_id = db.Column(db.String(36))
    details = db.Column(db.String(500), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class Admin(db.Model):
    __tablename__ = "admins"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="admin")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    @classmethod
    def create(cls, username, password, role="admin"):
        return cls(
            username=username,
            password_hash=generate_password_hash(password, method="scrypt"),
            role=role,
        )


class RegistrationInvite(db.Model):
    __tablename__ = "registration_invites"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cpf_hash = db.Column(db.String(64), nullable=False)
    cpf_encrypted = db.Column(db.Text, nullable=False)
    token_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))
    invalidated_at = db.Column(db.DateTime(timezone=True))
    created_by = db.Column(
        db.String(36), db.ForeignKey("admins.id", ondelete="RESTRICT"), nullable=False
    )
    __table_args__ = (
        db.UniqueConstraint("token_hash", name="uq_registration_invites_token_hash"),
        db.Index("ix_registration_invites_cpf_hash", "cpf_hash"),
        db.Index(
            "uq_registration_invites_active_cpf",
            "cpf_hash",
            unique=True,
            sqlite_where=db.text("used_at IS NULL AND invalidated_at IS NULL"),
            postgresql_where=db.text("used_at IS NULL AND invalidated_at IS NULL"),
        ),
    )


class LookupChallenge(db.Model):
    __tablename__ = "lookup_challenges"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    participant_id = db.Column(db.String(36), db.ForeignKey("participants.id", ondelete="CASCADE"))
    event_participant_id = db.Column(
        db.String(36), db.ForeignKey("event_participants.id", ondelete="CASCADE")
    )
    code_hash = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    used = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class EmailNotification(db.Model):
    __tablename__ = "email_notifications"
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    participant_id = db.Column(
        db.String(36), db.ForeignKey("participants.id", ondelete="CASCADE")
    )
    event_participant_id = db.Column(
        db.String(36), db.ForeignKey("event_participants.id", ondelete="CASCADE")
    )
    notification_type = db.Column(db.String(24), nullable=False)
    recipient_encrypted = db.Column(db.Text, nullable=False)
    subject_encrypted = db.Column(db.Text, nullable=False)
    body_encrypted = db.Column(db.Text, nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(240))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    sent_at = db.Column(db.DateTime(timezone=True))
    __table_args__ = (
        db.UniqueConstraint(
            "participant_id", "notification_type", name="uq_email_notification_participant_type"
        ),
        db.UniqueConstraint(
            "event_participant_id",
            "notification_type",
            name="uq_email_notification_event_participant_type",
        ),
        db.Index("ix_email_notification_pending", "sent_at", "created_at"),
    )
