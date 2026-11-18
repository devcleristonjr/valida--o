"""Add institutional event workflow

Revision ID: 20261118_event_workflow
Revises: 20261116_registration_invites
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa


revision = "20261118_event_workflow"
down_revision = "20261116_registration_invites"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "event_workflows",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("current_stage", sa.Integer(), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("demand", sa.JSON(), nullable=False),
        sa.Column("analysis", sa.JSON(), nullable=False),
        sa.Column("agenda", sa.JSON(), nullable=False),
        sa.Column("technical_note", sa.JSON(), nullable=False),
        sa.Column("ceremonial", sa.JSON(), nullable=False),
        sa.Column("secom", sa.JSON(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("stage_status", sa.JSON(), nullable=False),
        sa.Column("stage_history", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )



def downgrade():
    op.drop_table("event_workflows")
