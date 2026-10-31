from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.core.config import settings
from app.main import app

TEST_API_KEY = "test-api-key-not-a-secret"
TEST_SOURCE_ID = "test-application"

client = TestClient(app)


def test_valid_api_key_authenticates_source() -> None:
    response = client.get(
        "/v1/auth/check",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json() == {"source_id": TEST_SOURCE_ID}
    assert TEST_API_KEY not in response.text


def test_invalid_api_key_is_rejected_without_echoing_secrets() -> None:
    invalid_key = "invalid-test-key"
    response = client.get(
        "/v1/auth/check",
        headers={"X-API-Key": invalid_key},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}
    assert TEST_API_KEY not in response.text
    assert invalid_key not in response.text


def test_missing_api_key_is_rejected() -> None:
    response = client.get("/v1/auth/check")

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}


def test_source_id_comes_from_trusted_configuration() -> None:
    response = client.get(
        "/v1/auth/check",
        params={"source_id": "client-controlled-source"},
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 200
    assert response.json() == {"source_id": TEST_SOURCE_ID}


def test_unconfigured_authentication_fails_securely(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "eventledger_api_key", None)
    monkeypatch.setattr(settings, "eventledger_source_id", None)

    response = client.get(
        "/v1/auth/check",
        headers={"X-API-Key": TEST_API_KEY},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}
