"""Add activity lifecycle, capacity, and email notifications

Revision ID: 20261116_activity_ops
Revises: 20261116_add_events
Create Date: 2026-11-16 14:15:00

"""
from alembic import op
import sqlalchemy as sa


revision = "20261116_activity_ops"
down_revision = "20261116_add_events"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("audiences") as batch_op:
        batch_op.add_column(
            sa.Column("lifecycle_status", sa.String(length=16), nullable=False, server_default="open")
        )
        batch_op.add_column(sa.Column("capacity", sa.Integer(), nullable=True))
    with op.batch_alter_table("events") as batch_op:
        batch_op.add_column(
            sa.Column("lifecycle_status", sa.String(length=16), nullable=False, server_default="open")
        )
        batch_op.add_column(sa.Column("capacity", sa.Integer(), nullable=True))
    op.create_table(
        "email_notifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("participant_id", sa.String(length=36), nullable=True),
        sa.Column("event_participant_id", sa.String(length=36), nullable=True),
        sa.Column("notification_type", sa.String(length=24), nullable=False),
        sa.Column("recipient_encrypted", sa.Text(), nullable=False),
        sa.Column("subject_encrypted", sa.Text(), nullable=False),
        sa.Column("body_encrypted", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["event_participant_id"], ["event_participants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "participant_id",
            "notification_type",
            name="uq_email_notification_participant_type",
        ),
        sa.UniqueConstraint(
            "event_participant_id",
            "notification_type",
            name="uq_email_notification_event_participant_type",
        ),
    )
    op.create_index(
        "ix_email_notification_pending", "email_notifications", ["sent_at", "created_at"]
    )


def downgrade():
    op.drop_index("ix_email_notification_pending", table_name="email_notifications")
    op.drop_table("email_notifications")
    with op.batch_alter_table("events") as batch_op:
        batch_op.drop_column("capacity")
        batch_op.drop_column("lifecycle_status")
    with op.batch_alter_table("audiences") as batch_op:
        batch_op.drop_column("capacity")
        batch_op.drop_column("lifecycle_status")
