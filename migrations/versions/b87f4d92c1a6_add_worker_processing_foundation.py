"""add worker processing foundation

Revision ID: b87f4d92c1a6
Revises: ea20c2a0ca0d
Create Date: 2026-10-31

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b87f4d92c1a6"
down_revision: Union[str, Sequence[str], None] = "ea20c2a0ca0d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add durable claiming, attempt audit, and demo fulfillment state."""
    op.add_column(
        "events",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.execute(
        sa.text(
            "UPDATE events "
            "SET next_attempt_at = created_at "
            "WHERE next_attempt_at IS NULL"
        )
    )
    op.alter_column(
        "events",
        "next_attempt_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    op.add_column(
        "events",
        sa.Column(
            "lease_token",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    connection = op.get_bind()
    has_processing_rows = connection.execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM events WHERE status = 'processing'"
            ")"
        )
    ).scalar_one()
    if has_processing_rows:
        raise RuntimeError(
            "Cannot add lease consistency enforcement while events with status "
            "'processing' exist. Resolve those rows explicitly, then rerun the "
            "migration; this migration will not reset them automatically."
        )

    op.create_check_constraint(
        op.f("ck_events_lease_state_consistent"),
        "events",
        "(status = 'processing' "
        "AND lease_token IS NOT NULL "
        "AND lease_expires_at IS NOT NULL) "
        "OR (status <> 'processing' "
        "AND lease_token IS NULL "
        "AND lease_expires_at IS NULL)",
    )
    op.create_index(
        "ix_events_pending_claim",
        "events",
        ["next_attempt_at", "created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_events_expired_lease",
        "events",
        ["lease_expires_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'processing'"),
    )

    op.create_table(
        "event_processing_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0",
            name=op.f("ck_event_processing_attempts_attempt_number_positive"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ("
            "'completed', "
            "'retryable_failure', "
            "'permanent_failure', "
            "'lease_expired'"
            ")",
            name=op.f("ck_event_processing_attempts_outcome_allowed"),
        ),
        sa.CheckConstraint(
            "(finished_at IS NULL AND outcome IS NULL) "
            "OR (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name=op.f(
                "ck_event_processing_attempts_completion_fields_consistent"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_processing_attempts_event_id_events"),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_event_processing_attempts"),
        ),
        sa.UniqueConstraint(
            "event_id",
            "attempt_number",
            name="uq_event_processing_attempts_event_id_attempt_number",
        ),
        sa.UniqueConstraint(
            "lease_token",
            name="uq_event_processing_attempts_lease_token",
        ),
    )

    op.create_table(
        "order_fulfillments",
        sa.Column("source_id", sa.String(length=100), nullable=False),
        sa.Column("order_id", sa.String(length=255), nullable=False),
        sa.Column("first_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "fulfilled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(order_id)) > 0 "
            "AND order_id = btrim(order_id) "
            "AND order_id !~ '^[[:space:]]|[[:space:]]$'",
            name=op.f("ck_order_fulfillments_order_id_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["first_event_id"],
            ["events.id"],
            name=op.f("fk_order_fulfillments_first_event_id_events"),
        ),
        sa.PrimaryKeyConstraint(
            "source_id",
            "order_id",
            name=op.f("pk_order_fulfillments"),
        ),
        sa.UniqueConstraint(
            "first_event_id",
            name="uq_order_fulfillments_first_event_id",
        ),
    )


def downgrade() -> None:
    """Remove only the worker processing foundation."""
    op.drop_table("order_fulfillments")
    op.drop_table("event_processing_attempts")
    op.drop_index("ix_events_expired_lease", table_name="events")
    op.drop_index("ix_events_pending_claim", table_name="events")
    op.drop_constraint(
        op.f("ck_events_lease_state_consistent"),
        "events",
        type_="check",
    )
    op.drop_column("events", "lease_expires_at")
    op.drop_column("events", "lease_token")
    op.drop_column("events", "next_attempt_at")
