from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "idempotency_key",
            name="uq_events_source_id_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="status_allowed",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_nonnegative",
        ),
        CheckConstraint(
            "(status = 'processing' "
            "AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) "
            "OR (status <> 'processing' "
            "AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="lease_state_consistent",
        ),
        Index("ix_events_status_created_at", "status", "created_at"),
        Index(
            "ix_events_pending_claim",
            "next_attempt_at",
            "created_at",
            "id",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_events_expired_lease",
            "lease_expires_at",
            "id",
            postgresql_where=text("status = 'processing'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    lease_token: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

