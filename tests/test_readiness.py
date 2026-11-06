import logging
from collections.abc import Callable, Generator

import pytest
from fastapi.testclient import TestClient
from pytest import LogCaptureFixture, MonkeyPatch
from sqlalchemy.exc import SQLAlchemyError

import app.main as main_module
from app.db.compatibility import LEGACY_BOOTSTRAP_REVISION
from app.db.session import get_db_session
from app.main import app


class FakeReadySession:
    def __init__(
        self,
        *,
        revision: str = LEGACY_BOOTSTRAP_REVISION,
        contract_exists: bool = False,
        contract_version: int | None = None,
        error: Exception | None = None,
    ) -> None:
        self.revision = revision
        self.contract_exists = contract_exists
        self.contract_version = contract_version
        self.error = error
        self.execute_count = 0
        self.scalar_count = 0

    def execute(self, statement: object) -> None:
        del statement
        self.execute_count += 1
        if self.error is not None:
            raise self.error

    def scalar(self, statement: object) -> str | int | bool | None:
        self.scalar_count += 1
        sql = str(statement)
        if "to_regclass" in sql:
            return self.contract_exists
        if "eventledger_schema_contract" in sql:
            return self.contract_version
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


def test_ready_on_legacy_bootstrap_revision(
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    session = FakeReadySession()
    install_ready_session(session)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert session.execute_count == 1
    assert session.scalar_count == 2


def test_ready_on_supported_contract_without_reading_alembic_revision(
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    session = FakeReadySession(
        revision="future-revision-not-consulted",
        contract_exists=True,
        contract_version=1,
    )
    install_ready_session(session)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert session.scalar_count == 2


def test_not_ready_when_database_revision_is_stale(
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    session = FakeReadySession(revision="old-revision")
    install_ready_session(session)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_not_ready_when_contract_is_unsupported(
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    session = FakeReadySession(contract_exists=True, contract_version=2)
    install_ready_session(session)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_failure_is_sanitized(
    caplog: LogCaptureFixture,
    install_ready_session: Callable[[FakeReadySession], None],
) -> None:
    secret = "password=must-not-leak"
    session = FakeReadySession(error=SQLAlchemyError(secret))
    install_ready_session(session)
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
