"""Liveness probe contract.

`/healthz` must remain cheap, dependency-free, and 200/OK as long as the
process is up. Don't add downstream-service checks here — that's
`/admin/healthz/detail`.
"""

from fastapi.testclient import TestClient


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_no_auth_required(client: TestClient) -> None:
    """Healthz must work without bearer token (otherwise Docker/uptime can't probe it)."""
    response = client.get("/healthz")
    assert response.status_code != 401
