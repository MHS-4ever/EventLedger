from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OrderFulfillment(Base):
    __tablename__ = "order_fulfillments"
    __table_args__ = (
        UniqueConstraint(
            "first_event_id",
            name="uq_order_fulfillments_first_event_id",
        ),
        CheckConstraint(
            "length(btrim(order_id)) > 0 "
            "AND order_id = btrim(order_id) "
            "AND order_id !~ '^[[:space:]]|[[:space:]]$'",
            name="order_id_valid",
        ),
    )

    source_id: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
        nullable=False,
    )
    order_id: Mapped[str] = mapped_column(
        String(255),
        primary_key=True,
        nullable=False,
    )
    first_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("events.id"),
        nullable=False,
    )
    fulfilled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
