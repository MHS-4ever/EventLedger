import time
from dataclasses import dataclass
from typing import Any

from app.services.processing_errors import PermanentFailure, RetryableFailure
from app.services.processing_types import ClaimedEvent


@dataclass(frozen=True, slots=True)
class HandlerOptions:
    failure_simulation_enabled: bool = False
    simulation_max_slow_seconds: float = 30


def _apply_failure_simulation(
    claim: ClaimedEvent,
    options: HandlerOptions,
) -> None:
    if not options.failure_simulation_enabled:
        return

    mode = claim.payload.get("_demo_failure")
    if mode is None or mode == "success":
        return
    if mode == "transient_once":
        if claim.attempt_number == 1:
            raise RetryableFailure("Simulated transient processing failure")
        return
    if mode == "transient_always":
        raise RetryableFailure("Simulated transient processing failure")
    if mode == "permanent":
        raise PermanentFailure("Simulated permanent processing failure")
    if mode in {"slow", "slow_once"}:
        if mode == "slow_once" and claim.attempt_number > 1:
            return
        duration = claim.payload.get("_demo_slow_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration <= 0
            or duration > options.simulation_max_slow_seconds
        ):
            raise PermanentFailure("Invalid simulated slow-processing duration")
        time.sleep(float(duration))
        return

    raise PermanentFailure("Invalid failure simulation mode")


def validate_order_created(
    claim: ClaimedEvent,
    options: HandlerOptions,
) -> str:
    if claim.event_type != "order.created":
        raise PermanentFailure("Unsupported event type")

    _apply_failure_simulation(claim, options)

    order_id: Any = claim.payload.get("order_id")
    if not isinstance(order_id, str):
        raise PermanentFailure("order_id must be a string")
    if not order_id:
        raise PermanentFailure("order_id must not be empty")
    if len(order_id) > 255:
        raise PermanentFailure("order_id must not exceed 255 characters")
    if order_id != order_id.strip():
        raise PermanentFailure("order_id must not contain surrounding whitespace")
    return order_id
