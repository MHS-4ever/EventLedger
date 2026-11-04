import logging
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.v1.router import router as v1_router
from app.db.readiness import database_is_ready
from app.db.session import get_db_session

logger = logging.getLogger(__name__)

app = FastAPI(title="EventLedger")
app.include_router(v1_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/ready",
    response_model=None,
    responses={503: {"description": "Database or schema is not ready"}},
)
def readiness(
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    try:
        ready = database_is_ready(session)
    except Exception as error:
        logger.warning(
            "readiness_check_failed error_type=%s",
            type(error).__name__,
        )
        ready = False

    if not ready:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready"},
        )
    return JSONResponse(status_code=200, content={"status": "ready"})

