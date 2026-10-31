from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.event import EventCreateRequest, EventResponse


def test_valid_event_request() -> None:
    request = EventCreateRequest(
        event_type="order.created",
        payload={"order_id": "order-123"},
    )

    assert request.event_type == "order.created"
    assert request.payload == {"order_id": "order-123"}


def test_nested_json_payload() -> None:
    payload = {
        "order": {
            "items": [
                {"sku": "item-1", "quantity": 2},
                {"sku": "item-2", "quantity": 1},
            ],
            "paid": True,
            "discount": None,
        },
        "total": 42.5,
    }

    request = EventCreateRequest(
        event_type="order.created",
        payload=payload,
    )

    assert request.payload == payload


@pytest.mark.parametrize("event_type", ["", "   "])
def test_empty_event_type_is_rejected(event_type: str) -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest(event_type=event_type, payload={})


def test_missing_event_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest.model_validate({"payload": {}})


def test_missing_payload_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest.model_validate({"event_type": "order.created"})


@pytest.mark.parametrize("payload", [[], "not-an-object", None])
def test_invalid_top_level_payload_is_rejected(payload: object) -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest(event_type="order.created", payload=payload)


@pytest.mark.parametrize(
    "event_type",
    ["Order.Created", "order created", "order..created", ".created", "order."],
)
def test_invalid_event_name_is_rejected(event_type: str) -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest(event_type=event_type, payload={})


def test_unexpected_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest(
            event_type="order.created",
            payload={},
            id=str(uuid4()),
        )


def test_client_supplied_source_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest(
            event_type="order.created",
            payload={},
            source_id="untrusted-source",
        )


def test_client_supplied_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventCreateRequest(
            event_type="order.created",
            payload={},
            status="completed",
        )


def test_valid_response_schema() -> None:
    event_id = uuid4()
    created_at = datetime.now(timezone.utc)

    response = EventResponse(
        id=event_id,
        source_id="test-application",
        event_type="order.created",
        status="pending",
        created_at=created_at,
    )

    assert response.id == event_id
    assert response.source_id == "test-application"
    assert response.event_type == "order.created"
    assert response.status == "pending"
    assert response.created_at == created_at


def test_invalid_response_status_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EventResponse(
            id=uuid4(),
            source_id="test-application",
            event_type="order.created",
            status="unknown",
            created_at=datetime.now(timezone.utc),
        )


def test_response_requires_timezone_aware_created_at() -> None:
    with pytest.raises(ValidationError):
        EventResponse(
            id=uuid4(),
            source_id="test-application",
            event_type="order.created",
            status="pending",
            created_at=datetime.now(),
        )
