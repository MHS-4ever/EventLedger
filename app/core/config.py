from pydantic import Field, SecretStr
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


settings = Settings()

