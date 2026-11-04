import logging
from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient
from pytest import LogCaptureFixture, MonkeyPatch
from sqlalchemy.exc import SQLAlchemyError

import app.db.readiness as readiness_service
import app.main as main_module
from app.db.session import get_db_session
from app.main import app


class FakeReadySession:
    def __init__(
        self,
        *,
        revision: str = "expected-head",
        error: Exception | None = None,
    ) -> None:
        self.revision = revision
        self.error = error
        self.execute_count = 0
        self.scalar_count = 0

    def execute(self, statement: object) -> None:
        del statement
        self.execute_count += 1
        if self.error is not None:
            raise self.error

    def scalar(self, statement: object) -> str:
        del statement
        self.scalar_count += 1
        return self.revision


@pytest.fixture
def install_ready_session() -> Generator[
    Callable[[FakeReadySession], None],
    None,
    None,
]:
    def install(session: FakeReadySession) -> None:
        def override() -> Generator[FakeReadySession, None, None]:
            yield session

        app.dependency_overrides[get_db_session] = override

    yield install
    app.dependency_overrides.pop(get_db_session, None)


client = TestClient(app)


def test_ready_when_database_revision_matches(
    monkeypatch: MonkeyPatch,
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    session = FakeReadySession()
    install_ready_session(session)
    monkeypatch.setattr(
        readiness_service,
        "expected_alembic_revision",
        lambda: "expected-head",
    )

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert session.execute_count == 1
    assert session.scalar_count == 1


def test_not_ready_when_database_revision_is_stale(
    monkeypatch: MonkeyPatch,
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    session = FakeReadySession(revision="old-revision")
    install_ready_session(session)
    monkeypatch.setattr(
        readiness_service,
        "expected_alembic_revision",
        lambda: "expected-head",
    )

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_failure_is_sanitized(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    secret = "password=must-not-leak"
    session = FakeReadySession(error=SQLAlchemyError(secret))
    install_ready_session(session)
    monkeypatch.setattr(
        readiness_service,
        "expected_alembic_revision",
        lambda: "expected-head",
    )
    caplog.set_level(logging.WARNING, logger="app.main")

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "readiness_check_failed" in caplog.text
    assert "SQLAlchemyError" in caplog.text
    assert secret not in caplog.text
    assert secret not in response.text


def test_unexpected_readiness_failure_is_sanitized(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    session = FakeReadySession()
    install_ready_session(session)
    secret = "token=must-not-leak"

    def fail_readiness(session: object) -> bool:
        del session
        raise RuntimeError(secret)

    monkeypatch.setattr(main_module, "database_is_ready", fail_readiness)
    caplog.set_level(logging.WARNING, logger="app.main")

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text
    assert secret not in response.text
