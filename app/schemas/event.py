"""Schemas for the event ingestion API.

POST /v1/events requires an ``Idempotency-Key`` HTTP header containing 1-255
non-whitespace characters. The authenticated ``source_id`` comes
from the API-key dependency, never from the request body. Duplicate handling
is intentionally outside the scope of these schemas.
"""

from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

EventStatus: TypeAlias = Literal[
    "pending",
    "processing",
    "completed",
    "failed",
]

ProcessingAttemptOutcome: TypeAlias = Literal[
    "completed",
    "retryable_failure",
    "permanent_failure",
    "lease_expired",
]

ProcessingAttemptErrorSummary: TypeAlias = Literal[
    "Retryable processing failure",
    "Permanent processing failure",
    "Processing lease expired",
]

EventType = Annotated[
    str,
    Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)*$",
        examples=["order.created", "payment.failed"],
    ),
]


class EventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: EventType
    payload: dict[str, JsonValue]


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: str
    event_type: str
    status: EventStatus
    created_at: AwareDatetime


class EventDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: str
    event_type: str
    payload: dict[str, JsonValue]
    status: EventStatus
    attempt_count: int
    created_at: AwareDatetime
    updated_at: AwareDatetime


class EventListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_id: str
    event_type: str
    status: EventStatus
    attempt_count: int
    created_at: AwareDatetime
    updated_at: AwareDatetime


class EventListResponse(BaseModel):
    items: list[EventListItem]
    total: int
    limit: int
    offset: int


class EventHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: EventStatus
    created_at: AwareDatetime


class EventHistoryResponse(BaseModel):
    event_id: UUID
    items: list[EventHistoryItem]
    total: int
    limit: int
    offset: int


class EventProcessingAttemptItem(BaseModel):
    id: UUID
    attempt_number: int
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    outcome: ProcessingAttemptOutcome | None
    error_summary: ProcessingAttemptErrorSummary | None


class EventProcessingAttemptResponse(BaseModel):
    event_id: UUID
    items: list[EventProcessingAttemptItem]
    total: int
    limit: int
    offset: int


class EventManualRetryResponse(BaseModel):
    event_id: UUID
    retry_request_id: UUID
    status: Literal["accepted"]
    target_attempt_number: Annotated[int, Field(gt=0)]
    accepted_at: AwareDatetime

