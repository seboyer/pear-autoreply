"""Pub/Sub push receiver.

Phase 0: validates the request shape and acks. JWT verification + history
fetch + worker enqueue are wired in Phase 1 (see PLAN.md § 1).

Pub/Sub push delivers a JSON envelope:
    {
      "message": {
        "data": "<base64-encoded JSON>",
        "messageId": "...",
        "publishTime": "..."
      },
      "subscription": "..."
    }

The base64-decoded `data` for Gmail push contains:
    {"emailAddress": "agent@pearnyc.com", "historyId": "12345"}
"""

import base64
import json
import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(tags=["pubsub"])


class PubSubMessage(BaseModel):
    data: str = ""
    message_id: str = Field(default="", alias="messageId")
    publish_time: str = Field(default="", alias="publishTime")


class PubSubEnvelope(BaseModel):
    message: PubSubMessage
    subscription: str = ""


@router.post("/pubsub/inbox", status_code=status.HTTP_204_NO_CONTENT)
async def pubsub_inbox(request: Request) -> None:
    """Receive a Pub/Sub push notification.

    TODO (Phase 1):
      1. Verify the OIDC JWT in `Authorization: Bearer ...`
         against `settings.pubsub_audience` and
         `settings.pubsub_service_account_email`.
      2. Decode `message.data` (base64 → JSON) → `{emailAddress, historyId}`.
      3. Look up previous historyId in Redis for that mailbox.
      4. Call `users.history.list` with `historyTypes=messageAdded`,
         `labelId=Pear/Leads`.
      5. Enqueue a worker job per new message-id.
      6. Update Redis historyId pointer.
      7. Return 204 (Pub/Sub treats anything 2xx as ack).
    """
    body = await request.body()

    try:
        envelope = PubSubEnvelope.model_validate_json(body)
    except Exception as exc:  # opaque parsing failures
        logger.warning("pubsub: malformed envelope: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Pub/Sub envelope",
        ) from exc

    if not envelope.message.data:
        logger.info("pubsub: received empty message (heartbeat?)")
        return None

    try:
        decoded = json.loads(base64.b64decode(envelope.message.data))
    except Exception as exc:
        logger.warning("pubsub: cannot decode message.data: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="message.data is not base64-encoded JSON",
        ) from exc

    logger.info(
        "pubsub: received notification messageId=%s emailAddress=%s historyId=%s",
        envelope.message.message_id,
        decoded.get("emailAddress"),
        decoded.get("historyId"),
    )
    # Phase 0 stops here. Phase 1 wires the rest.
    return None
