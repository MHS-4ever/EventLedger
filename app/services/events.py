import hashlib
import json

from pydantic import JsonValue


def compute_request_hash(
    event_type: str,
    payload: dict[str, JsonValue],
) -> str:
    canonical_request = json.dumps(
        {"event_type": event_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()

