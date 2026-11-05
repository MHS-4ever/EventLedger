import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

import app.worker as worker_module
from app.core.config import Settings
from app.models import Event
from app.services.order_handler import HandlerOptions, validate_order_created
from app.services.processing import (
    effective_attempt_limit,
    process_claim,
    sanitize_error,
)
from app.services.processing_errors import (
    LeaseOwnershipLost,
    PermanentFailure,
    ProcessingStateError,
    RetryableFailure,
)
from app.services.processing_types import (
    ClaimedEvent,
    FailureResult,
    ProcessingPolicy,
    WorkerCycleResult,
)
from app.worker import build_parser, run_worker


BASE_SETTINGS = {
    "POSTGRES_DB": "eventledger_test",
    "POSTGRES_USER": "eventledger_test",
    "POSTGRES_PASSWORD": "test-password-not-a-secret",
}


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **BASE_SETTINGS, **overrides)


def make_claim(
    *,
    event_type: str = "order.created",
    payload: dict | None = None,
    attempt_number: int = 1,
) -> ClaimedEvent:
    lease_token = uuid4()
    return ClaimedEvent(
        event_id=uuid4(),
        source_id="test-source",
        event_type=event_type,
        payload=payload if payload is not None else {"order_id": "ORD-1001"},
        attempt_number=attempt_number,
        lease_token=lease_token,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )


def test_effective_attempt_limit_uses_override_exactly() -> None:
    policy = ProcessingPolicy(30, 3, 2, 8)
    ordinary_event = Event(attempt_count=2)
    overridden_event = Event(attempt_count=3, attempt_limit_override=4)

    assert effective_attempt_limit(ordinary_event, policy) == 3
    assert effective_attempt_limit(overridden_event, policy) == 4
    assert effective_attempt_limit(
        overridden_event,
        ProcessingPolicy(30, 100, 2, 8),
    ) == 4


@pytest.mark.parametrize(
    ("attempt_count", "override"),
    [(0, 0), (3, 2)],
)
def test_effective_attempt_limit_rejects_malformed_override(
    attempt_count: int,
    override: int,
) -> None:
    event = Event(
        attempt_count=attempt_count,
        attempt_limit_override=override,
    )

    with pytest.raises(ProcessingStateError, match="override is invalid"):
        effective_attempt_limit(event, ProcessingPolicy(30, 3, 2, 8))


def test_worker_import_does_not_import_fastapi_application() -> None:
    environment = os.environ.copy()
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import app.worker; "
                "assert 'app.main' not in sys.modules"
            ),
        ],
        cwd=os.getcwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_worker_settings_defaults_are_valid() -> None:
    worker_settings = make_settings()

    assert worker_settings.worker_poll_interval_seconds == 1
    assert worker_settings.worker_lease_duration_seconds == 30
    assert worker_settings.worker_max_attempts == 3
    assert worker_settings.worker_retry_base_seconds == 2
    assert worker_settings.worker_retry_max_seconds == 60
    assert not worker_settings.worker_failure_simulation_enabled


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"WORKER_POLL_INTERVAL_SECONDS": 0}, "greater than 0"),
        ({"WORKER_LEASE_DURATION_SECONDS": 0}, "greater than or equal to 1"),
        ({"WORKER_MAX_ATTEMPTS": 0}, "greater than or equal to 1"),
        (
            {
                "WORKER_RETRY_BASE_SECONDS": 10,
                "WORKER_RETRY_MAX_SECONDS": 5,
            },
            "greater than or equal",
        ),
        ({"WORKER_IDENTIFIER": " worker-a"}, "outer whitespace"),
    ],
)
def test_worker_settings_reject_invalid_values(
    overrides: dict[str, object],
    expected_fragment: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        make_settings(**overrides)

    assert expected_fragment in str(error.value)


@pytest.mark.parametrize(
    "identifier",
    [
        "worker\nsecond-line",
        "worker\rreturn",
        "worker\ttab",
        "worker\x00null",
        "worker\u2028line-separator",
    ],
)
def test_worker_identifier_rejects_control_characters(identifier: str) -> None:
    with pytest.raises(ValidationError, match="control characters"):
        make_settings(WORKER_IDENTIFIER=identifier)


def test_worker_identifier_accepts_normal_custom_value() -> None:
    worker_settings = make_settings(WORKER_IDENTIFIER="worker-a_01")

    assert worker_settings.worker_identifier == "worker-a_01"


def test_retry_backoff_is_exponential_and_capped() -> None:
    policy = ProcessingPolicy(
        lease_duration_seconds=30,
        max_attempts=5,
        retry_base_seconds=2,
        retry_max_seconds=5,
    )

    assert policy.retry_delay_seconds(1) == 2
    assert policy.retry_delay_seconds(2) == 4
    assert policy.retry_delay_seconds(3) == 5
    assert policy.retry_delay_seconds(10) == 5


def test_retry_backoff_rejects_nonpositive_attempt() -> None:
    policy = ProcessingPolicy(30, 3, 2, 60)

    with pytest.raises(ValueError, match="positive"):
        policy.retry_delay_seconds(0)


@pytest.mark.parametrize(
    "invalid_order_id",
    [None, True, 1, [], {}, "", " " * 2, " leading", "trailing ", "\u00a0id"],
)
def test_order_handler_rejects_invalid_order_identifiers(
    invalid_order_id: object,
) -> None:
    claim = make_claim(payload={"order_id": invalid_order_id})

    with pytest.raises(PermanentFailure):
        validate_order_created(claim, HandlerOptions())


def test_order_handler_rejects_unsupported_event_type() -> None:
    with pytest.raises(PermanentFailure, match="Unsupported"):
        validate_order_created(make_claim(event_type="payment.received"), HandlerOptions())


def test_order_handler_accepts_maximum_length_order_identifier() -> None:
    order_id = "A" * 255

    assert (
        validate_order_created(
            make_claim(payload={"order_id": order_id}),
            HandlerOptions(),
        )
        == order_id
    )


def test_failure_simulation_is_disabled_by_default() -> None:
    claim = make_claim(
        payload={"order_id": "ORD-1001", "_demo_failure": "transient_always"}
    )

    assert validate_order_created(claim, HandlerOptions()) == "ORD-1001"


def test_transient_once_simulation_uses_durable_attempt_number() -> None:
    options = HandlerOptions(failure_simulation_enabled=True)
    first = make_claim(
        payload={"order_id": "ORD-1001", "_demo_failure": "transient_once"},
        attempt_number=1,
    )
    second = make_claim(
        payload={"order_id": "ORD-1001", "_demo_failure": "transient_once"},
        attempt_number=2,
    )

    with pytest.raises(RetryableFailure):
        validate_order_created(first, options)
    assert validate_order_created(second, options) == "ORD-1001"


def test_slow_simulation_is_bounded(monkeypatch: MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("app.services.order_handler.time.sleep", slept.append)
    options = HandlerOptions(
        failure_simulation_enabled=True,
        simulation_max_slow_seconds=5,
    )

    valid = make_claim(
        payload={
            "order_id": "ORD-1001",
            "_demo_failure": "slow",
            "_demo_slow_seconds": 3,
        }
    )
    assert validate_order_created(valid, options) == "ORD-1001"
    assert slept == [3.0]

    invalid = make_claim(
        payload={
            "order_id": "ORD-1001",
            "_demo_failure": "slow",
            "_demo_slow_seconds": 6,
        }
    )
    with pytest.raises(PermanentFailure, match="duration"):
        validate_order_created(invalid, options)


def test_slow_once_simulation_only_delays_first_attempt(
    monkeypatch: MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr("app.services.order_handler.time.sleep", slept.append)
    options = HandlerOptions(failure_simulation_enabled=True)
    payload = {
        "order_id": "ORD-1001",
        "_demo_failure": "slow_once",
        "_demo_slow_seconds": 3,
    }

    assert validate_order_created(make_claim(payload=payload), options) == "ORD-1001"
    assert (
        validate_order_created(
            make_claim(payload=payload, attempt_number=2),
            options,
        )
        == "ORD-1001"
    )
    assert slept == [3.0]


def test_error_sanitization_is_bounded_and_single_line() -> None:
    sanitized = sanitize_error("  controlled\n failure  " + "x" * 600)

    assert "\n" not in sanitized
    assert sanitized.startswith("controlled failure")
    assert len(sanitized) == 500


def test_once_cli_has_precise_semantics() -> None:
    arguments = build_parser().parse_args(["--once"])

    assert arguments.once


def test_worker_warns_when_failure_simulation_is_enabled(
    monkeypatch: MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker_settings = make_settings(
        WORKER_IDENTIFIER="simulation-test",
        WORKER_FAILURE_SIMULATION_ENABLED=True,
    )
    monkeypatch.setattr(worker_module, "settings", worker_settings)
    monkeypatch.setattr(
        worker_module,
        "run_worker_cycle",
        lambda *args, **kwargs: WorkerCycleResult(None, None, None),
    )
    monkeypatch.setattr(worker_module, "_schema_preflight", lambda *args: True)
    monkeypatch.setattr(worker_module.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker_module.engine, "dispose", lambda: None)
    caplog.set_level(logging.WARNING, logger="app.worker")

    assert run_worker(once=True) == 0

    warning = next(
        record
        for record in caplog.records
        if "worker_failure_simulation_enabled" in record.getMessage()
    )
    assert "simulation-test" in warning.getMessage()
    assert "must not be enabled in production" in warning.getMessage()


def test_worker_rejects_incompatible_schema_before_polling(
    monkeypatch: MonkeyPatch,
) -> None:
    disposed: list[bool] = []
    installed_signals: list[object] = []
    monkeypatch.setattr(worker_module, "_schema_preflight", lambda *args: False)
    monkeypatch.setattr(
        worker_module,
        "run_worker_cycle",
        lambda *args, **kwargs: pytest.fail("worker must not poll"),
    )
    monkeypatch.setattr(
        worker_module.signal,
        "signal",
        lambda signum, handler: installed_signals.append(signum),
    )
    monkeypatch.setattr(
        worker_module.engine,
        "dispose",
        lambda: disposed.append(True),
    )

    assert run_worker(once=True) == worker_module.WORKER_SCHEMA_PREFLIGHT_EXIT_CODE
    assert disposed == [True]
    assert worker_module.signal.SIGINT in installed_signals
    if hasattr(worker_module.signal, "SIGTERM"):
        assert worker_module.signal.SIGTERM in installed_signals


def test_worker_schema_preflight_sanitizes_database_failure(
    monkeypatch: MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "password=must-not-leak"

    class SessionContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            del args

    def fail_check(session: object) -> object:
        del session
        raise RuntimeError(secret)

    monkeypatch.setattr(worker_module, "SessionLocal", SessionContext)
    monkeypatch.setattr(worker_module, "check_schema_compatibility", fail_check)
    caplog.set_level(logging.ERROR, logger="app.worker")

    assert not worker_module._schema_preflight("test-worker")
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text


def test_process_claim_classifies_retryable_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    claim = make_claim()
    captured: dict[str, object] = {}

    def transient_handler(claim, options):
        del claim, options
        raise RetryableFailure("Temporary dependency failure")

    def finalize(session_factory, finalized_claim, policy, **values):
        del session_factory, policy
        captured.update(values)
        return FailureResult(
            finalized_claim.event_id,
            finalized_claim.attempt_number,
            "pending",
        )

    monkeypatch.setattr(
        "app.services.processing.finalize_processing_failure",
        finalize,
    )

    result = process_claim(
        object(),
        claim,
        ProcessingPolicy(30, 3, 2, 60),
        HandlerOptions(),
        handler=transient_handler,
    )

    assert result.status == "pending"
    assert captured == {
        "retryable": True,
        "message": "Temporary dependency failure",
    }


def test_process_claim_hides_unexpected_exception_message(
    monkeypatch: MonkeyPatch,
) -> None:
    claim = make_claim()
    captured: dict[str, object] = {}

    def unsafe_handler(claim, options):
        del claim, options
        raise RuntimeError("password=do-not-persist")

    def finalize(session_factory, finalized_claim, policy, **values):
        del session_factory, policy
        captured.update(values)
        return FailureResult(
            finalized_claim.event_id,
            finalized_claim.attempt_number,
            "failed",
        )

    monkeypatch.setattr(
        "app.services.processing.finalize_processing_failure",
        finalize,
    )

    result = process_claim(
        object(),
        claim,
        ProcessingPolicy(30, 3, 2, 60),
        HandlerOptions(),
        handler=unsafe_handler,
    )

    assert result.status == "failed"
    assert captured == {
        "retryable": False,
        "message": "Unexpected processing error",
    }


def test_process_claim_reports_ownership_loss_without_business_success(
    monkeypatch: MonkeyPatch,
) -> None:
    claim = make_claim()

    def lose_ownership(session_factory, finalized_claim, order_id):
        del session_factory, finalized_claim, order_id
        raise LeaseOwnershipLost("stale")

    monkeypatch.setattr(
        "app.services.processing.finalize_order_success",
        lose_ownership,
    )

    result = process_claim(
        object(),
        claim,
        ProcessingPolicy(30, 3, 2, 60),
        HandlerOptions(),
    )

    assert result.status == "ownership_lost"
