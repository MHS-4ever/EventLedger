"""add manual retry foundation

Revision ID: d4e5f6a7b8c9
Revises: b87f4d92c1a6
Create Date: 2026-11-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "b87f4d92c1a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add exact per-event attempt overrides and manual retry audit state."""
    op.add_column(
        "events",
        sa.Column("attempt_limit_override", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_events_attempt_limit_override_valid"),
        "events",
        "attempt_limit_override IS NULL OR ("
        "attempt_limit_override > 0 "
        "AND attempt_limit_override >= attempt_count)",
    )

    op.create_table(
        "event_manual_retry_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("target_attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 255 "
            "AND idempotency_key !~ '[[:space:]]'",
            name=op.f(
                "ck_event_manual_retry_requests_idempotency_key_valid"
            ),
        ),
        sa.CheckConstraint(
            "target_attempt_number > 0",
            name=op.f(
                "ck_event_manual_retry_requests_target_attempt_number_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f(
                "fk_event_manual_retry_requests_event_id_events"
            ),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_event_manual_retry_requests"),
        ),
        sa.UniqueConstraint(
            "event_id",
            name="uq_event_manual_retry_requests_event_id",
        ),
    )


def downgrade() -> None:
    """Remove manual retry audit state and exact attempt overrides."""
    op.drop_table("event_manual_retry_requests")
    op.drop_constraint(
        op.f("ck_events_attempt_limit_override_valid"),
        "events",
        type_="check",
    )
    op.drop_column("events", "attempt_limit_override")
