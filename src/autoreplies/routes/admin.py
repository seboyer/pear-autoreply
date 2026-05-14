"""Admin endpoints. All gated by bearer-token auth (see deps.require_admin_token).

These routes are surfaced behind Caddy with a 30 req/min/IP rate limit on /admin/*.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import require_admin_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_token)])


@router.post("/replay/{message_id}")
def replay(message_id: str) -> dict[str, str]:
    """Replay processing for a given Gmail message-id.

    Phase 0: stub. Phase 4 wires re-enqueue against the existing Redis state
    (skipping any phase that already succeeded).
    """
    logger.info("admin: replay requested for message_id=%s", message_id)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Replay not implemented yet (Phase 4)",
    )


@router.post("/reload-template")
def reload_template() -> dict[str, str]:
    """Re-read FALLBACK_TEMPLATE.md from disk without restarting.

    Phase 0: stub. Phase 2 wires the in-process template cache.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Template reload not implemented yet (Phase 2)",
    )


@router.post("/reload-signatures")
def reload_signatures() -> dict[str, str]:
    """Invalidate all cached agent Gmail signatures (force refetch on next send).

    Phase 0: stub. Phase 2 wires the Redis signature cache.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Signature reload not implemented yet (Phase 2)",
    )


@router.get("/healthz/detail")
def healthz_detail() -> dict[str, str | dict[str, str]]:
    """Deep healthcheck: verify connectivity to each downstream service.

    Phase 0: returns a flat 'unimplemented' map so monitoring can already wire it up.
    Phase 4 fills in real Redis/Airtable/Supabase/Slack/Anthropic pings.
    """
    return {
        "status": "stub",
        "checks": {
            "redis": "not_implemented",
            "airtable": "not_implemented",
            "supabase": "not_implemented",
            "slack": "not_implemented",
            "anthropic": "not_implemented",
            "gmail": "not_implemented",
        },
    }
