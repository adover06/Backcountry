"""switch to Firebase UIDs + add invite_codes; nuke prior data

Revision ID: 0002_firebase_invites
Revises: 0001_initial
Create Date: 2026-05-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_firebase_invites"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nuke and recreate: existing data is throwaway (user opted in).
    op.execute("DROP TABLE IF EXISTS share_tokens CASCADE")
    op.execute("DROP TABLE IF EXISTS saved_trails CASCADE")
    op.execute("DROP TABLE IF EXISTS trip_plans CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")

    op.create_table(
        "users",
        sa.Column("id", sa.String(128), primary_key=True),  # Firebase UID
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(120)),
        sa.Column(
            "preferences",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "invite_codes",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("note", sa.Text),
        sa.Column(
            "created_by",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "redeemed_by",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            unique=True,
        ),
        sa.Column("redeemed_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "trip_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("start_date", sa.Date),
        sa.Column("end_date", sa.Date),
        sa.Column("route", postgresql.JSONB),
        sa.Column("selected_trail", postgresql.JSONB),
        sa.Column("checks", postgresql.JSONB),
        sa.Column("report", postgresql.JSONB),
        sa.Column("gpx_path", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_trip_plans_user_id", "trip_plans", ["user_id"])

    op.create_table(
        "saved_trails",
        sa.Column(
            "user_id",
            sa.String(128),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("trail_id", sa.String(128), primary_key=True),
        sa.Column(
            "saved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "share_tokens",
        sa.Column("token", sa.String(64), primary_key=True),
        sa.Column(
            "trip_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trip_plans.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    # Destructive forward — no real downgrade. Drop everything.
    op.execute("DROP TABLE IF EXISTS share_tokens CASCADE")
    op.execute("DROP TABLE IF EXISTS saved_trails CASCADE")
    op.execute("DROP TABLE IF EXISTS trip_plans CASCADE")
    op.execute("DROP TABLE IF EXISTS invite_codes CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
