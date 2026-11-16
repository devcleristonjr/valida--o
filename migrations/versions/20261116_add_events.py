"""Add independent event registration

Revision ID: 20261116_add_events
Revises: ed0aab402399
Create Date: 2026-11-16 13:00:00

"""
from alembic import op
import sqlalchemy as sa


revision = "20261116_add_events"
down_revision = "ed0aab402399"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", "date", "start_time", name="uq_event_schedule"),
    )
    op.create_table(
        "event_participants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("cpf_hash", sa.String(length=64), nullable=False),
        sa.Column("cpf_encrypted", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=120), nullable=False),
        sa.Column("phone_encrypted", sa.Text(), nullable=False),
        sa.Column("email_encrypted", sa.Text(), nullable=True),
        sa.Column("instagram_encrypted", sa.Text(), nullable=True),
        sa.Column("photo_filename", sa.String(length=80), nullable=False),
        sa.Column("credential_hash", sa.String(length=64), nullable=False),
        sa.Column("credential_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_version", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_method", sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "cpf_hash", name="uq_event_participant_cpf"),
        sa.UniqueConstraint("credential_hash"),
    )
    op.create_index(
        "ix_event_participant_event_status",
        "event_participants",
        ["event_id", "status"],
        unique=False,
    )
    with op.batch_alter_table("access_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("event_participant_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_access_logs_event_participant_id_event_participants",
            "event_participants",
            ["event_participant_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("lookup_challenges", schema=None) as batch_op:
        batch_op.add_column(sa.Column("event_participant_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_lookup_challenges_event_participant_id_event_participants",
            "event_participants",
            ["event_participant_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade():
    with op.batch_alter_table("lookup_challenges", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_lookup_challenges_event_participant_id_event_participants",
            type_="foreignkey",
        )
        batch_op.drop_column("event_participant_id")
    with op.batch_alter_table("access_logs", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_access_logs_event_participant_id_event_participants",
            type_="foreignkey",
        )
        batch_op.drop_column("event_participant_id")
    op.drop_index("ix_event_participant_event_status", table_name="event_participants")
    op.drop_table("event_participants")
    op.drop_table("events")
