import pytest
from pydantic import ValidationError

from app.core.config import Settings


DATABASE_SETTINGS = {
    "POSTGRES_DB": "eventledger_test",
    "POSTGRES_USER": "eventledger_test",
    "POSTGRES_PASSWORD": "test-password-not-a-secret",
}


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **DATABASE_SETTINGS, **overrides)


def test_development_mode_keeps_authentication_optional() -> None:
    settings = make_settings(
        EVENTLEDGER_API_KEY=None,
        EVENTLEDGER_SOURCE_ID=None,
    )

    assert not settings.shipsafe_production_mode


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {
                "EVENTLEDGER_API_KEY": None,
                "EVENTLEDGER_SOURCE_ID": "production-source",
            },
            "EVENTLEDGER_API_KEY",
        ),
        (
            {
                "EVENTLEDGER_API_KEY": "production-api-key",
                "EVENTLEDGER_SOURCE_ID": None,
            },
            "EVENTLEDGER_SOURCE_ID",
        ),
        (
            {
                "EVENTLEDGER_API_KEY": "production-api-key",
                "EVENTLEDGER_SOURCE_ID": " production-source ",
            },
            "EVENTLEDGER_SOURCE_ID",
        ),
    ],
)
def test_shipsafe_production_mode_requires_valid_identity(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(SHIPSAFE_PRODUCTION_MODE=True, **overrides)


def test_shipsafe_production_mode_accepts_configured_identity() -> None:
    settings = make_settings(
        SHIPSAFE_PRODUCTION_MODE=True,
        EVENTLEDGER_API_KEY="production-api-key",
        EVENTLEDGER_SOURCE_ID="production-source",
    )

    assert settings.shipsafe_production_mode
