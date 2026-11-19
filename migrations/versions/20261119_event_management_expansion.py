"""Expand institutional event management

Revision ID: 20261119_event_management
Revises: 20261118_event_workflow
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa


revision = "20261119_event_management"
down_revision = "20261119invite"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("modality", sa.String(length=30), nullable=False, server_default="event"))
        batch.add_column(sa.Column("subject", sa.String(length=160), nullable=False, server_default=""))
        batch.add_column(sa.Column("municipality", sa.String(length=100), nullable=False, server_default=""))
        batch.add_column(sa.Column("responsible", sa.String(length=160), nullable=False, server_default=""))
        batch.add_column(sa.Column("teams", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("secretariats", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("expectation", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("complementary", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("general_data", sa.JSON(), nullable=False, server_default="{}"))
    with op.batch_alter_table("audiences") as batch:
        batch.add_column(sa.Column("location", sa.String(length=200), nullable=False, server_default=""))
        batch.add_column(sa.Column("municipality", sa.String(length=100), nullable=False, server_default=""))
        batch.add_column(sa.Column("responsible", sa.String(length=160), nullable=False, server_default=""))
        batch.add_column(sa.Column("expectation", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("complementary", sa.Text(), nullable=False, server_default=""))
    with op.batch_alter_table("event_participants") as batch:
        batch.add_column(sa.Column("press_profile", sa.JSON(), nullable=False, server_default="{}"))

    op.create_table(
        "event_acts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("municipality", sa.String(length=100), nullable=False),
        sa.Column("secretariat", sa.String(length=160), nullable=False),
        sa.Column("act", sa.String(length=160), nullable=False),
        sa.Column("action", sa.String(length=240), nullable=False),
        sa.Column("species", sa.String(length=80), nullable=False),
        sa.Column("classification", sa.String(length=80), nullable=False),
        sa.Column("modality", sa.String(length=80), nullable=False),
        sa.Column("budget_ceiling", sa.Numeric(14, 2)),
        sa.Column("validation_status", sa.String(length=20), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "event_guests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("origin", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("ballot_name", sa.String(length=150), nullable=False),
        sa.Column("phone_encrypted", sa.Text()),
        sa.Column("email_encrypted", sa.Text()),
        sa.Column("role", sa.String(length=120), nullable=False),
        sa.Column("municipality", sa.String(length=100), nullable=False),
        sa.Column("party", sa.String(length=100), nullable=False),
        sa.Column("political_support", sa.String(length=160), nullable=False),
        sa.Column("allowed_environment", sa.String(length=160), nullable=False),
        sa.Column("allowed_area", sa.String(length=160), nullable=False),
        sa.Column("representative", sa.String(length=150), nullable=False),
        sa.Column("invitation_status", sa.String(length=20), nullable=False),
        sa.Column("acceptance_status", sa.String(length=20), nullable=False),
        sa.Column("confirmation_history", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "event_environments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("color", sa.String(length=20), nullable=False),
        sa.Column("representative_allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "event_guest_environments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("guest_id", sa.String(length=36), sa.ForeignKey("event_guests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("environment_id", sa.String(length=36), sa.ForeignKey("event_environments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("area", sa.String(length=160), nullable=False),
        sa.Column("with_representative", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("guest_id", "environment_id", name="uq_guest_environment"),
    )
    op.create_table(
        "event_attachments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("document_type", sa.String(length=80), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "event_analyses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("record_id", sa.String(length=36)),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=160), nullable=False),
        sa.Column("responsible", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provision", sa.Text(), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "municipal_visits",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("municipality", sa.String(length=100), nullable=False),
        sa.Column("activity", sa.String(length=200), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("time", sa.Time(), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=False),
        sa.Column("responsible", sa.String(length=160), nullable=False),
        sa.Column("observations", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "event_deliveries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_id", sa.String(length=36), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item", sa.String(length=200), nullable=False),
        sa.Column("association", sa.String(length=160), nullable=False),
        sa.Column("cnpj", sa.String(length=18), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("photograph", sa.String(length=255)),
        sa.Column("provision", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("event_deliveries")
    op.drop_table("municipal_visits")
    op.drop_table("event_analyses")
    op.drop_table("event_attachments")
    op.drop_table("event_guest_environments")
    op.drop_table("event_environments")
    op.drop_table("event_guests")
    op.drop_table("event_acts")
    with op.batch_alter_table("event_participants") as batch:
        batch.drop_column("press_profile")
    with op.batch_alter_table("audiences") as batch:
        batch.drop_columns("location", "municipality", "responsible", "expectation", "complementary")
    with op.batch_alter_table("events") as batch:
        batch.drop_columns("general_data", "complementary", "expectation", "secretariats", "teams", "responsible", "municipality", "subject", "modality")
