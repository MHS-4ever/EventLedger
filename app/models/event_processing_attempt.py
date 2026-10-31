from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EventProcessingAttempt(Base):
    __tablename__ = "event_processing_attempts"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "attempt_number",
            name="uq_event_processing_attempts_event_id_attempt_number",
        ),
        UniqueConstraint(
            "lease_token",
            name="uq_event_processing_attempts_lease_token",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="attempt_number_positive",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ("
            "'completed', "
            "'retryable_failure', "
            "'permanent_failure', "
            "'lease_expired'"
            ")",
            name="outcome_allowed",
        ),
        CheckConstraint(
            "(finished_at IS NULL AND outcome IS NULL) "
            "OR (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="completion_fields_consistent",
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
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
