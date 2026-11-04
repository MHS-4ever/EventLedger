import logging
from collections.abc import Generator
from uuid import uuid4

from fastapi.testclient import TestClient
from pytest import LogCaptureFixture, MonkeyPatch

import app.api.v1.events as events_api
from app.db.session import get_db_session
from app.main import app


def test_manual_retry_openapi_contract() -> None:
    operation = app.openapi()["paths"]["/v1/events/{event_id}/retry"]["post"]
    response_codes = set(operation["responses"])
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert {"200", "202", "401", "404", "409", "422", "500"} <= response_codes
    assert "scoped to this individual event retry resource" in operation[
        "description"
    ]
    assert parameters["Idempotency-Key"]["required"] is True
    assert operation["security"] == [{"APIKeyHeader": []}]
    assert "requestBody" not in operation
    assert "Location" in operation["responses"]["202"]["headers"]
    assert "X-Idempotent-Replay" in operation["responses"]["200"]["headers"]


def test_invalid_retry_uuid_returns_validation_error() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/events/not-a-uuid/retry",
            headers={
                "X-API-Key": "test-api-key-not-a-secret",
                "Idempotency-Key": "valid-retry-key",
            },
        )

    assert response.status_code == 422


def test_unexpected_retry_error_is_generic_and_logged_safely(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    secret = "password=never-log-this"

    class FakeSession:
        def __init__(self) -> None:
            self.rollback_count = 0

        def rollback(self) -> None:
            self.rollback_count += 1

    session = FakeSession()

    def override() -> Generator[FakeSession, None, None]:
        yield session

    def fail_authorization(*args, **kwargs):
        del args, kwargs
        raise RuntimeError(secret)

    app.dependency_overrides[get_db_session] = override
    monkeypatch.setattr(events_api, "authorize_manual_retry", fail_authorization)
    caplog.set_level(logging.ERROR, logger="app.api.v1.events")
    event_id = uuid4()
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/v1/events/{event_id}/retry",
                headers={
                    "X-API-Key": "test-api-key-not-a-secret",
                    "Idempotency-Key": "unexpected-error-test",
                },
            )
    finally:
        app.dependency_overrides.pop(get_db_session, None)

    assert response.status_code == 500
    assert response.json() == {"detail": "Unable to authorize manual retry"}
    assert session.rollback_count == 1
    assert "manual_retry_unexpected_error" in caplog.text
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text
    assert secret not in response.text
