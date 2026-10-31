from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    lease_duration_seconds: int
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int

    def retry_delay_seconds(self, attempt_number: int) -> int:
        if attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        delay = self.retry_base_seconds * (2 ** (attempt_number - 1))
        return min(delay, self.retry_max_seconds)


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    event_id: UUID
    source_id: str
    event_type: str
    payload: dict[str, Any]
    attempt_number: int
    lease_token: UUID
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    event_id: UUID
    attempt_number: int
    status: Literal["pending", "failed"]


@dataclass(frozen=True, slots=True)
class FailureResult:
    event_id: UUID
    attempt_number: int
    status: Literal["pending", "failed"]


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    event_id: UUID
    attempt_number: int
    status: Literal["completed", "pending", "failed", "ownership_lost"]


@dataclass(frozen=True, slots=True)
class WorkerCycleResult:
    recovered: RecoveryResult | None
    exhausted_event_id: UUID | None
    processing: ProcessingResult | None
