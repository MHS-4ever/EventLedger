from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.security import AuthenticatedSource, authenticate_source

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.get("/check")
def check_authentication(
    source: Annotated[AuthenticatedSource, Depends(authenticate_source)],
) -> dict[str, str]:
    return {"source_id": source.source_id}

