from dataclasses import dataclass
from hmac import compare_digest
from typing import Annotated

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import SecretStr

from app.core.config import settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True, slots=True)
class AuthenticatedSource:
    source_id: str


def _configured_identities() -> tuple[tuple[SecretStr, str], ...]:
    api_key = settings.eventledger_api_key
    source_id = settings.eventledger_source_id

    if (
        api_key is None
        or not api_key.get_secret_value()
        or source_id is None
        or not source_id.strip()
    ):
        return ()

    return ((api_key, source_id),)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
    )


def authenticate_source(
    supplied_api_key: Annotated[str | None, Security(api_key_header)],
) -> AuthenticatedSource:
    if supplied_api_key is None:
        raise _unauthorized()

    for configured_api_key, source_id in _configured_identities():
        if compare_digest(
            supplied_api_key,
            configured_api_key.get_secret_value(),
        ):
            return AuthenticatedSource(source_id=source_id)

    raise _unauthorized()

