from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EventManualRetryRequest(Base):
    __tablename__ = "event_manual_retry_requests"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            name="uq_event_manual_retry_requests_event_id",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 255 "
            "AND idempotency_key !~ '[[:space:]]'",
            name="idempotency_key_valid",
        ),
        CheckConstraint(
            "target_attempt_number > 0",
            name="target_attempt_number_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("events.id"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    target_attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
