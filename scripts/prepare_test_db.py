import sys

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

EXPECTED_DATABASE = "eventledger_test"
EXPECTED_USER = "eventledger_test"
EXPECTED_PORT = 5433
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class TestDatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env.test",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(
        default="127.0.0.1",
        validation_alias="TEST_POSTGRES_HOST",
    )
    port: int = Field(default=5433, validation_alias="TEST_POSTGRES_PORT")
    database: str = Field(validation_alias="TEST_POSTGRES_DB")
    user: str = Field(validation_alias="TEST_POSTGRES_USER")
    password: SecretStr = Field(validation_alias="TEST_POSTGRES_PASSWORD")


def load_test_database_settings() -> TestDatabaseSettings:
    return TestDatabaseSettings()


def validate_test_database_settings(settings: TestDatabaseSettings) -> None:
    if settings.database != EXPECTED_DATABASE:
        raise RuntimeError("Refusing to use a database other than eventledger_test.")
    if settings.user != EXPECTED_USER:
        raise RuntimeError("Refusing to use a user other than eventledger_test.")
    if settings.host.lower() not in LOCAL_HOSTS:
        raise RuntimeError("Refusing to use a non-local PostgreSQL host.")
    if settings.port != EXPECTED_PORT:
        raise RuntimeError("Refusing to use a PostgreSQL port other than 5433.")


def build_test_database_url(settings: TestDatabaseSettings) -> URL:
    validate_test_database_settings(settings)
    return URL.create(
        drivername="postgresql+psycopg",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )


def create_test_database_engine(settings: TestDatabaseSettings) -> Engine:
    return create_engine(
        build_test_database_url(settings),
        pool_pre_ping=True,
    )


def verify_test_database_connection(connection: Connection) -> None:
    database_name, user_name = connection.execute(
        text("SELECT current_database(), current_user")
    ).one()

    if database_name != EXPECTED_DATABASE or user_name != EXPECTED_USER:
        raise RuntimeError(
            "Connected database identity is not the isolated EventLedger test database."
        )


def apply_test_database_migrations(engine: Engine) -> None:
    alembic_config = Config("alembic.ini")

    with engine.begin() as connection:
        verify_test_database_connection(connection)
        alembic_config.attributes["connection"] = connection
        command.upgrade(alembic_config, "head")


def main() -> int:
    try:
        settings = load_test_database_settings()
        validate_test_database_settings(settings)
    except ValidationError:
        print(
            "Invalid test database configuration. Check the required "
            "TEST_POSTGRES_* values in .env.test.",
            file=sys.stderr,
        )
        return 1
    except RuntimeError as error:
        print(f"Safety check failed: {error}", file=sys.stderr)
        return 1

    engine = create_test_database_engine(settings)
    try:
        apply_test_database_migrations(engine)
    except RuntimeError as error:
        print(f"Safety check failed: {error}", file=sys.stderr)
        return 1
    except (CommandError, SQLAlchemyError):
        print(
            "Test database preparation failed. Verify the isolated container "
            "is healthy and the test credentials are correct.",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()

    print(
        "Alembic migrations applied successfully to "
        f"eventledger_test on {settings.host}:{settings.port}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
