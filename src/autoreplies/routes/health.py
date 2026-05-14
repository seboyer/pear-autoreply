"""Healthcheck endpoint.

`/healthz` is consumed by Docker's HEALTHCHECK and any external monitors. It
intentionally does not depend on Redis / Airtable / Anthropic — those have their
own checks under `/admin/healthz/detail` (auth-gated). Keeping `/healthz` cheap
prevents flapping when a downstream provider has a transient blip.
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe. Returns 200 if the process is up."""
    return {"status": "ok"}
