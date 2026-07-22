"""
observability/feedback.py
--------------------------
Per-turn user feedback store.

Feedback is lightweight: a rating (1-5) and optional comment, keyed by
(session_id, turn_index).  Records are persisted to Cosmos DB when available,
otherwise written to a local JSONL file for development.

Exposed via POST /feedback in app/routes/admin.py.

Usage::

    from observability.feedback import record_feedback
    from app.models import FeedbackRequest

    feedback_id = await record_feedback(FeedbackRequest(
        session_id="abc-123",
        turn_index=2,
        rating=5,
        comment="Great match!",
    ))
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from app.config import settings
from app.models import FeedbackRequest

_logger = logging.getLogger(__name__)
_LOCAL_FEEDBACK_FILE = Path(__file__).parent.parent / "data" / "feedback.jsonl"
_FEEDBACK_CONTAINER = "feedback"


async def record_feedback(request: FeedbackRequest) -> str:
    """Persist a feedback record. Returns the generated feedback_id."""
    feedback_id = uuid.uuid4().hex[:12]
    doc = {
        "id": feedback_id,
        "session_id": request.session_id,
        "turn_index": request.turn_index,
        "rating": request.rating,
        "comment": request.comment,
        "recorded_at": time.time(),
    }

    if settings.use_cosmos:
        _write_to_cosmos(doc)
    else:
        _write_to_file(doc)

    _logger.info(
        "[Feedback] session=%s turn=%d rating=%d id=%s",
        request.session_id,
        request.turn_index,
        request.rating,
        feedback_id,
    )
    return feedback_id


async def record_interaction_feedback(payload: dict) -> str:
    """Persist dashboard interaction feedback as a rich learning record."""
    feedback_id = uuid.uuid4().hex[:12]
    session_id = str(payload.get("session_id") or "dashboard-interaction")
    doc = {
        "id": feedback_id,
        "session_id": session_id,
        "record_type": "dashboard_interaction",
        "payload": payload,
        "recorded_at": time.time(),
    }

    if settings.use_cosmos:
        _write_to_cosmos(doc)
    else:
        _write_to_file(doc)

    _logger.info(
        "[Feedback] interaction session=%s role=%s candidate=%s id=%s",
        session_id,
        payload.get("role_id", ""),
        payload.get("candidate_id") or payload.get("candidate_name") or "",
        feedback_id,
    )
    return feedback_id


def _write_to_cosmos(doc: dict) -> None:
    try:
        from azure.cosmos import CosmosClient, PartitionKey
        from azure.cosmos.exceptions import CosmosResourceExistsError

        client = CosmosClient(
            url=settings.cosmos_endpoint,
            credential=settings.cosmos_key,
            connection_verify=False,
        )
        db = client.create_database_if_not_exists(settings.cosmos_database)
        try:
            container = db.create_container(
                id=_FEEDBACK_CONTAINER,
                partition_key=PartitionKey(path="/session_id"),
            )
        except CosmosResourceExistsError:
            container = db.get_container_client(_FEEDBACK_CONTAINER)
        container.upsert_item(doc)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("[Feedback] Cosmos write failed, falling back to file: %s", exc)
        _write_to_file(doc)


def _write_to_file(doc: dict) -> None:
    try:
        _LOCAL_FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _LOCAL_FEEDBACK_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(doc) + "\n")
    except Exception as exc:  # noqa: BLE001
        _logger.error("[Feedback] File write failed: %s", exc)
