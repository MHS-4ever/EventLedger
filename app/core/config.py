from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_db: str = Field(validation_alias="POSTGRES_DB")
    postgres_user: str = Field(validation_alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(validation_alias="POSTGRES_PASSWORD")
    postgres_host: str = Field(
        default="127.0.0.1",
        validation_alias="POSTGRES_HOST",
    )
    postgres_port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    eventledger_api_key: SecretStr | None = Field(
        default=None,
        validation_alias="EVENTLEDGER_API_KEY",
    )
    eventledger_source_id: str | None = Field(
        default=None,
        validation_alias="EVENTLEDGER_SOURCE_ID",
    )
    shipsafe_production_mode: bool = Field(
        default=False,
        validation_alias="SHIPSAFE_PRODUCTION_MODE",
    )
    worker_poll_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        le=60,
        validation_alias="WORKER_POLL_INTERVAL_SECONDS",
    )
    worker_lease_duration_seconds: int = Field(
        default=30,
        ge=1,
        le=86_400,
        validation_alias="WORKER_LEASE_DURATION_SECONDS",
    )
    worker_max_attempts: int = Field(
        default=3,
        ge=1,
        le=100,
        validation_alias="WORKER_MAX_ATTEMPTS",
    )
    worker_retry_base_seconds: int = Field(
        default=2,
        ge=1,
        le=86_400,
        validation_alias="WORKER_RETRY_BASE_SECONDS",
    )
    worker_retry_max_seconds: int = Field(
        default=60,
        ge=1,
        le=86_400,
        validation_alias="WORKER_RETRY_MAX_SECONDS",
    )
    worker_identifier: str | None = Field(
        default=None,
        max_length=100,
        validation_alias="WORKER_IDENTIFIER",
    )
    worker_failure_simulation_enabled: bool = Field(
        default=False,
        validation_alias="WORKER_FAILURE_SIMULATION_ENABLED",
    )
    worker_simulation_max_slow_seconds: float = Field(
        default=30,
        gt=0,
        le=300,
        validation_alias="WORKER_SIMULATION_MAX_SLOW_SECONDS",
    )

    @field_validator("worker_identifier")
    @classmethod
    def validate_worker_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or value != value.strip():
            raise ValueError("worker identifier must be nonempty without outer whitespace")
        if not value.isprintable():
            raise ValueError("worker identifier must not contain control characters")
        return value

    @model_validator(mode="after")
    def validate_worker_retry_bounds(self) -> "Settings":
        if self.worker_retry_max_seconds < self.worker_retry_base_seconds:
            raise ValueError(
                "WORKER_RETRY_MAX_SECONDS must be greater than or equal to "
                "WORKER_RETRY_BASE_SECONDS"
            )
        if self.shipsafe_production_mode:
            api_key = self.eventledger_api_key
            source_id = self.eventledger_source_id
            if api_key is None or not api_key.get_secret_value():
                raise ValueError(
                    "SHIPSAFE_PRODUCTION_MODE requires EVENTLEDGER_API_KEY"
                )
            if (
                source_id is None
                or not source_id.strip()
                or source_id != source_id.strip()
                or not source_id.isprintable()
            ):
                raise ValueError(
                    "SHIPSAFE_PRODUCTION_MODE requires a valid "
                    "EVENTLEDGER_SOURCE_ID"
                )
        return self


settings = Settings()

