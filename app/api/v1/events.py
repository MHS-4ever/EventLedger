from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import AuthenticatedSource, authenticate_source
from app.db.session import get_db_session
from app.models import Event, EventStatusHistory
from app.schemas.event import (
    EventCreateRequest,
    EventDetailResponse,
    EventHistoryResponse,
    EventListResponse,
    EventResponse,
    EventStatus,
)
from app.services.events import compute_request_hash

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^\S+$",
    ),
]

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=EventListResponse)
def list_events(
    source: Annotated[AuthenticatedSource, Depends(authenticate_source)],
    session: Annotated[Session, Depends(get_db_session)],
    event_status: Annotated[EventStatus | None, Query(alias="status")] = None,
    event_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventListResponse:
    filters = [Event.source_id == source.source_id]
    if event_status is not None:
        filters.append(Event.status == event_status)
    if event_type is not None:
        filters.append(Event.event_type == event_type)

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
