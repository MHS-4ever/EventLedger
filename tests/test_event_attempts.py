from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db_session
from app.main import app
from app.models import EventProcessingAttempt
from app.services.events import public_attempt_error_summary

TEST_API_KEY = "test-api-key-not-a-secret"
TEST_SOURCE_ID = "test-application"


def stored_attempt(
    event_id: UUID,
    *,
    attempt_number: int,
    outcome: str | None = None,
    error: str | None = None,
) -> EventProcessingAttempt:
    started_at = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    return EventProcessingAttempt(
        id=uuid4(),
        event_id=event_id,
        attempt_number=attempt_number,
        lease_token=uuid4(),
        started_at=started_at,
        finished_at=(
            started_at + timedelta(seconds=1) if outcome is not None else None
        ),
        outcome=outcome,
        error=error,
    )


class FakeAttemptResult:
    def __init__(self, attempts: list[EventProcessingAttempt]) -> None:
        self.attempts = attempts

    def all(self) -> list[EventProcessingAttempt]:
        return self.attempts


class AttemptFakeSession:
    def __init__(
        self,
        event_id: UUID,
        *,
        authorized: bool = True,
        attempts: list[EventProcessingAttempt] | None = None,
        total: int = 0,
    ) -> None:
        self.event_id = event_id
        self.authorized = authorized
        self.attempts = attempts or []
        self.total = total
        self.scalar_statements: list[object] = []
        self.items_statements: list[object] = []

    def scalar(self, statement: object):
        self.scalar_statements.append(statement)
        if len(self.scalar_statements) == 1:
            parameters = compiled_parameters(statement)
            if (
                self.authorized
                and self.event_id in parameters
                and TEST_SOURCE_ID in parameters
            ):
                return self.event_id
            return None
        return self.total

    def scalars(self, statement: object) -> FakeAttemptResult:
        self.items_statements.append(statement)
        return FakeAttemptResult(self.attempts)


@pytest.fixture
def install_attempt_session() -> Generator[
    Callable[[AttemptFakeSession], None],
    None,
    None,
]:
    def install(session: AttemptFakeSession) -> None:
        def override() -> Generator[AttemptFakeSession, None, None]:
            yield session

        app.dependency_overrides[get_db_session] = override

    yield install
    app.dependency_overrides.pop(get_db_session, None)


client = TestClient(app)


def compiled_parameters(statement: object) -> set[object]:
    return set(statement.compile().params.values())


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("completed", None),
        ("retryable_failure", "Retryable processing failure"),
        ("permanent_failure", "Permanent processing failure"),
        ("lease_expired", "Processing lease expired"),
        (None, None),
    ],
)
def test_attempt_error_summaries_are_fixed(
    outcome: str | None,
    expected: str | None,
) -> None:
    assert public_attempt_error_summary(outcome) == expected


def test_empty_attempt_history(
    install_attempt_session: Callable[[AttemptFakeSession], None],
) -> None:
    event_id = uuid4()
    session = AttemptFakeSession(event_id)
    install_attempt_session(session)

    response = client.get(
        f"/v1/events/{event_id}/attempts",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json() == {
        "event_id": str(event_id),
        "items": [],
        "total": 0,
        "limit": 20,
        "offset": 0,
    }


def test_active_and_finished_attempts_use_safe_public_summaries(
    install_attempt_session: Callable[[AttemptFakeSession], None],
) -> None:
    event_id = uuid4()
    secret = "password=do-not-publish"
    attempts = [
        stored_attempt(event_id, attempt_number=3),
        stored_attempt(
            event_id,
            attempt_number=2,
            outcome="permanent_failure",
            error=secret,
        ),
        stored_attempt(event_id, attempt_number=1, outcome="completed"),
    ]
    session = AttemptFakeSession(event_id, attempts=attempts, total=3)
    install_attempt_session(session)

    response = client.get(
        f"/v1/events/{event_id}/attempts",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["finished_at"] is None
    assert body["items"][0]["outcome"] is None
    assert body["items"][0]["error_summary"] is None
    assert body["items"][1]["error_summary"] == "Permanent processing failure"
    assert body["items"][2]["error_summary"] is None
    assert secret not in response.text
    assert "lease_token" not in response.text
    assert "error" not in body["items"][1]


def test_attempts_are_ordered_descending_and_paginated(
    install_attempt_session: Callable[[AttemptFakeSession], None],
) -> None:
    event_id = uuid4()
    attempt = stored_attempt(
        event_id,
        attempt_number=2,
        outcome="retryable_failure",
    )
    session = AttemptFakeSession(event_id, attempts=[attempt], total=4)
    install_attempt_session(session)

    response = client.get(
        f"/v1/events/{event_id}/attempts",
        params={"limit": 1, "offset": 2},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 4
    assert response.json()["limit"] == 1
    assert response.json()["offset"] == 2
    parameters = compiled_parameters(session.items_statements[0])
    assert {1, 2} <= parameters
    statement_sql = " ".join(str(session.items_statements[0]).split())
    assert "ORDER BY event_processing_attempts.attempt_number DESC" in statement_sql


@pytest.mark.parametrize("api_key", [None, "invalid-test-key"])
def test_attempt_authentication_is_required(
    api_key: str | None,
    install_attempt_session: Callable[[AttemptFakeSession], None],
) -> None:
    event_id = uuid4()
    session = AttemptFakeSession(event_id)
    install_attempt_session(session)
    headers = {"X-API-Key": api_key} if api_key is not None else {}

    response = client.get(f"/v1/events/{event_id}/attempts", headers=headers)

    assert response.status_code == 401
    assert session.scalar_statements == []
    assert session.items_statements == []


@pytest.mark.parametrize("client_source", [None, "another-source"])
def test_missing_and_foreign_attempt_event_return_not_found(
    client_source: str | None,
    install_attempt_session: Callable[[AttemptFakeSession], None],
) -> None:
    event_id = uuid4()
    session = AttemptFakeSession(event_id, authorized=False)
    install_attempt_session(session)

    response = client.get(
        f"/v1/events/{event_id}/attempts",
        params={"source_id": client_source} if client_source else {},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found"}
    assert len(session.scalar_statements) == 1
    assert session.items_statements == []
    parameters = compiled_parameters(session.scalar_statements[0])
    assert TEST_SOURCE_ID in parameters
    if client_source is not None:
        assert client_source not in parameters


@pytest.mark.parametrize(
    "parameters",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"limit": "invalid"},
        {"offset": "invalid"},
    ],
)
def test_invalid_attempt_pagination_is_rejected(
    parameters: dict[str, object],
    install_attempt_session: Callable[[AttemptFakeSession], None],
) -> None:
    event_id = uuid4()
    session = AttemptFakeSession(event_id)
    install_attempt_session(session)

    response = client.get(
        f"/v1/events/{event_id}/attempts",
        params=parameters,
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 422
    assert session.scalar_statements == []
    assert session.items_statements == []
