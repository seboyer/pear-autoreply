"""FastAPI dependencies."""

import secrets

from fastapi import Header, HTTPException, status

from .config import Settings, get_settings
from .services.airtable import AirtableClient
from .services.airtable_schema import get_schema


def get_airtable_client() -> AirtableClient:
    """Construct an AirtableClient wired to the active base schema."""
    settings: Settings = get_settings()
    schema = get_schema(settings.active_airtable_base_id)
    return AirtableClient(
        token=settings.airtable_token,
        schema=schema,
        address_match_threshold=settings.apartment_fuzzy_match_threshold,
    )


def require_admin_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token auth for /admin/* endpoints.

    Constant-time comparison to defeat timing attacks. Token is a single shared
    secret stored in env; rotate quarterly. Pub/Sub endpoint is *not* gated by this
    — its auth is JWT signature verification on the push request.
    """
    settings: Settings = get_settings()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization.split(" ", 1)[1].strip()

    if not secrets.compare_digest(presented, settings.admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
