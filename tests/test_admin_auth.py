"""Bearer-token auth contract for /admin/* endpoints."""

from fastapi.testclient import TestClient


def test_admin_endpoint_rejects_missing_auth(client: TestClient) -> None:
    response = client.post("/admin/replay/abc123")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_admin_endpoint_rejects_wrong_token(client: TestClient) -> None:
    response = client.post(
        "/admin/replay/abc123",
        headers={"Authorization": "Bearer not-the-right-token"},
    )
    assert response.status_code == 401


def test_admin_endpoint_rejects_malformed_auth(client: TestClient) -> None:
    response = client.post(
        "/admin/replay/abc123",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401


def test_admin_endpoint_with_correct_token_passes_auth(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    """Right token → request passes auth (lands on the 501 stub, which is the Phase 0 expectation)."""
    response = client.post("/admin/replay/abc123", headers=admin_headers)
    assert response.status_code == 501


def test_admin_healthz_detail_works_with_auth(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.get("/admin/healthz/detail", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stub"
    assert "redis" in body["checks"]
