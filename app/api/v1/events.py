import logging
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import AuthenticatedSource, authenticate_source
from app.db.session import get_db_session
from app.models import Event, EventProcessingAttempt, EventStatusHistory
from app.schemas.event import (
    EventCreateRequest,
    EventDetailResponse,
    EventHistoryResponse,
    EventListResponse,
    EventManualRetryResponse,
    EventProcessingAttemptItem,
    EventProcessingAttemptResponse,
    EventResponse,
    EventStatus,
    EventType,
)
from app.services.events import (
    ManualRetryConflict,
    ManualRetryEventNotFound,
    authorize_manual_retry,
    compute_request_hash,
    public_attempt_error_summary,
)

logger = logging.getLogger(__name__)

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^\S+$",
    ),
]

ManualRetryIdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[^\s\x00-\x1f\x7f-\x9f]+$",
    ),
]

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=EventListResponse)
def list_events(
    source: Annotated[AuthenticatedSource, Depends(authenticate_source)],
    session: Annotated[Session, Depends(get_db_session)],
    event_status: Annotated[EventStatus | None, Query(alias="status")] = None,
    event_type: Annotated[EventType | None, Query()] = None,
    created_from: Annotated[AwareDatetime | None, Query()] = None,
    created_to: Annotated[AwareDatetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventListResponse:
    if (
        created_from is not None
        and created_to is not None
        and created_from >= created_to
    ):
        raise HTTPException(
            status_code=422,
            detail="created_from must be earlier than created_to",
        )

    filters = [Event.source_id == source.source_id]
    if event_status is not None:
        filters.append(Event.status == event_status)
    if event_type is not None:
        filters.append(Event.event_type == event_type)
    if created_from is not None:
        filters.append(Event.created_at >= created_from)
    if created_to is not None:
        filters.append(Event.created_at < created_to)

    count_statement = select(func.count()).select_from(Event).where(*filters)
    items_statement = (
        select(Event)
        .where(*filters)
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(limit)
        .offset(offset)
    )

    try:
        total = session.scalar(count_statement) or 0
        events = session.scalars(items_statement).all()
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to list events",
        ) from None

    return EventListResponse(
        items=events,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
def create_event(
    request: EventCreateRequest,
    response: Response,
    idempotency_key: IdempotencyKey,
    source: Annotated[AuthenticatedSource, Depends(authenticate_source)],
    session: Annotated[Session, Depends(get_db_session)],
) -> EventResponse:
    request_hash = compute_request_hash(request.event_type, request.payload)
    insert_statement = (
        insert(Event)
        .values(
            id=uuid4(),
            source_id=source.source_id,
            idempotency_key=idempotency_key,
            event_type=request.event_type,
            payload=request.payload,
            request_hash=request_hash,
            status="pending",
            attempt_count=0,
        )
        .on_conflict_do_nothing(
            index_elements=[Event.source_id, Event.idempotency_key]
        )
        .returning(Event)
    )

    try:
        inserted_event = session.scalars(insert_statement).one_or_none()

        if inserted_event is not None:
            session.add(
                EventStatusHistory(
                    event_id=inserted_event.id,
                    status="pending",
                )
            )
            session.commit()
            return EventResponse.model_validate(inserted_event)

        existing_event = session.scalar(
            select(Event).where(
                Event.source_id == source.source_id,
                Event.idempotency_key == idempotency_key,
            )
        )

        if existing_event is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unable to create event",
            )

        is_idempotent_replay = existing_event.request_hash == request_hash
        existing_response = EventResponse.model_validate(existing_event)
        session.rollback()

        if is_idempotent_replay:
            response.status_code = status.HTTP_200_OK
            response.headers["X-Idempotent-Replay"] = "true"
            return existing_response

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_conflict",
                "message": (
                    "Idempotency key was already used for a different request"
                ),
            },
        )
    except SQLAlchemyError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to create event",
        ) from None


@router.get("/{event_id}/history", response_model=EventHistoryResponse)
def get_event_history(
    event_id: UUID,
    source: Annotated[AuthenticatedSource, Depends(authenticate_source)],
    session: Annotated[Session, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventHistoryResponse:
    try:
        authorized_event_id = session.scalar(
            select(Event.id).where(
                Event.id == event_id,
                Event.source_id == source.source_id,
            )
        )

        if authorized_event_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found",
            )

        history_filter = EventStatusHistory.event_id == authorized_event_id
        total = (
            session.scalar(
                select(func.count())
                .select_from(EventStatusHistory)
                .where(history_filter)
            )
            or 0
        )
        history = session.scalars(
            select(EventStatusHistory)
            .where(history_filter)
            .order_by(
                EventStatusHistory.created_at.asc(),
                EventStatusHistory.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve event history",
        ) from None

    return EventHistoryResponse(
        event_id=authorized_event_id,
        items=history,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{event_id}/attempts",
    response_model=EventProcessingAttemptResponse,
)
def get_event_processing_attempts(
    event_id: UUID,
    source: Annotated[AuthenticatedSource, Depends(authenticate_source)],
    session: Annotated[Session, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventProcessingAttemptResponse:
    try:
        authorized_event_id = session.scalar(
            select(Event.id).where(
                Event.id == event_id,
                Event.source_id == source.source_id,
            )
        )

        if authorized_event_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Event not found",
            )

        attempt_filter = EventProcessingAttempt.event_id == authorized_event_id
        total = (
            session.scalar(
                select(func.count())
                .select_from(EventProcessingAttempt)
                .where(attempt_filter)
            )
            or 0
        )
        attempts = session.scalars(
            select(EventProcessingAttempt)
            .where(attempt_filter)
            .order_by(EventProcessingAttempt.attempt_number.desc())
            .limit(limit)
            .offset(offset)
        ).all()
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve processing attempts",
        ) from None

    return EventProcessingAttemptResponse(
        event_id=authorized_event_id,
        items=[
            EventProcessingAttemptItem(
                id=attempt.id,
                attempt_number=attempt.attempt_number,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                outcome=attempt.outcome,
                error_summary=public_attempt_error_summary(attempt.outcome),
            )
            for attempt in attempts
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{event_id}/retry",
    response_model=EventManualRetryResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Authorize one manual processing retry",
    description=(
        "Idempotency keys are scoped to this individual event retry resource. "
        "A valid replay returns the original stored acceptance even if the "
        "worker has since changed the event state."
    ),
    responses={
        200: {
            "model": EventManualRetryResponse,
            "description": "Same-key idempotent replay",
            "headers": {
                "Location": {
                    "description": "Canonical event resource",
                    "schema": {"type": "string"},
                },
                "X-Idempotent-Replay": {
                    "description": "Always true for a replay",
                    "schema": {"type": "string", "enum": ["true"]},
                },
            },
        },
        202: {
            "description": "Manual retry accepted",
            "headers": {
                "Location": {
                    "description": "Canonical event resource",
                    "schema": {"type": "string"},
                }
            },
        },
        401: {"description": "Invalid or missing API key"},
        404: {"description": "Event not found"},
        409: {"description": "Event cannot receive this retry grant"},
        422: {"description": "Invalid event ID or idempotency key"},
        500: {"description": "Unable to authorize manual retry"},
    },
)
def retry_event(
    event_id: UUID,
    response: Response,
    idempotency_key: ManualRetryIdempotencyKey,
    source: Annotated[AuthenticatedSource, Depends(authenticate_source)],
    session: Annotated[Session, Depends(get_db_session)],
) -> EventManualRetryResponse:
    try:
        authorization = authorize_manual_retry(
            session,
            event_id=event_id,
            source_id=source.source_id,
            idempotency_key=idempotency_key,
        )
        session.commit()
    except ManualRetryEventNotFound:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        ) from None
    except ManualRetryConflict as conflict:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": conflict.code, "message": conflict.message},
        ) from None
    except SQLAlchemyError as error:
        session.rollback()
        logger.error(
            "manual_retry_database_error event_id=%s error_type=%s",
            event_id,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to authorize manual retry",
        ) from None
    except Exception as error:
        session.rollback()
        logger.error(
            "manual_retry_unexpected_error event_id=%s error_type=%s",
            event_id,
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to authorize manual retry",
        ) from None

    response.headers["Location"] = f"/v1/events/{event_id}"
    if authorization.replayed:
        response.status_code = status.HTTP_200_OK
        response.headers["X-Idempotent-Replay"] = "true"

    return EventManualRetryResponse(
        event_id=authorization.event_id,
        retry_request_id=authorization.retry_request_id,
        status="accepted",
        target_attempt_number=authorization.target_attempt_number,
        accepted_at=authorization.accepted_at,
    )


@router.get("/{event_id}", response_model=EventDetailResponse)
def get_event(
    event_id: UUID,
    source: Annotated[AuthenticatedSource, Depends(authenticate_source)],
    session: Annotated[Session, Depends(get_db_session)],
) -> EventDetailResponse:
    try:
        event = session.scalar(
            select(Event).where(
                Event.id == event_id,
                Event.source_id == source.source_id,
            )
        )
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve event",
        ) from None

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    return EventDetailResponse.model_validate(event)
