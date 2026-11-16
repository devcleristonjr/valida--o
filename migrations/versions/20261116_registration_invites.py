"""Add secure participant registration invites

Revision ID: 20261116_registration_invites
Revises: 20261116_activity_ops
Create Date: 2026-11-16 13:45:00

"""
from alembic import op
import sqlalchemy as sa


revision = "20261116_registration_invites"
down_revision = "20261116_activity_ops"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "registration_invites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("cpf_hash", sa.String(length=64), nullable=False),
        sa.Column("cpf_encrypted", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"], ["admins.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_registration_invites_token_hash"),
    )
    op.create_index(
        "ix_registration_invites_cpf_hash",
        "registration_invites",
        ["cpf_hash"],
    )
    op.create_index(
        "uq_registration_invites_active_cpf",
        "registration_invites",
        ["cpf_hash"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
        sqlite_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
    )


def downgrade():
    op.drop_index("uq_registration_invites_active_cpf", table_name="registration_invites")
    op.drop_index("ix_registration_invites_cpf_hash", table_name="registration_invites")
    op.drop_table("registration_invites")
