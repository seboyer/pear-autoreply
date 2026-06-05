"""Tests for the app-level rate-limit middleware (src/autoreplies/ratelimit.py).

Covers:
- _FixedWindowLimiter unit tests (windowing, identity/bucket isolation).
- client_ip unit tests (XFF parsing, host fallback).
- RateLimitMiddleware integration tests (limits enforced, Retry-After header,
  per-IP isolation, non-matched paths pass through).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from autoreplies.ratelimit import RateLimitMiddleware, _FixedWindowLimiter, client_ip

# ---------------------------------------------------------------------------
# _FixedWindowLimiter unit tests
# ---------------------------------------------------------------------------


def test_limiter_allows_exactly_limit_hits_then_blocks() -> None:
    """Within a single window, exactly ``limit`` hits are allowed; the next is blocked."""
    limiter = _FixedWindowLimiter(window_seconds=60)
    for i in range(3):
        result = limiter.hit("ip1", "/admin", 3, now=0.0)
        assert result is True, f"hit {i + 1} should be allowed"

    blocked = limiter.hit("ip1", "/admin", 3, now=0.0)
    assert blocked is False, "4th hit in same window should be blocked"


def test_limiter_resets_in_next_window() -> None:
    """Counter resets when crossing a window boundary."""
    limiter = _FixedWindowLimiter(window_seconds=60)

    # Exhaust the first window.
    for _ in range(2):
        limiter.hit("ip1", "/admin", 2, now=0.0)
    assert limiter.hit("ip1", "/admin", 2, now=0.0) is False

    # Cross into the next window.
    assert limiter.hit("ip1", "/admin", 2, now=60.0) is True
    assert limiter.hit("ip1", "/admin", 2, now=60.0) is True
    assert limiter.hit("ip1", "/admin", 2, now=60.0) is False


def test_limiter_tracks_identities_independently() -> None:
    """Two different identities (IPs) are tracked in separate counters."""
    limiter = _FixedWindowLimiter(window_seconds=60)
    limit = 2

    # Exhaust ip1.
    for _ in range(limit):
        limiter.hit("ip1", "/admin", limit, now=0.0)
    assert limiter.hit("ip1", "/admin", limit, now=0.0) is False

    # ip2 is unaffected.
    assert limiter.hit("ip2", "/admin", limit, now=0.0) is True


def test_limiter_tracks_buckets_independently() -> None:
    """Two different buckets (path prefixes) are tracked in separate counters."""
    limiter = _FixedWindowLimiter(window_seconds=60)
    limit = 1

    assert limiter.hit("ip1", "/admin", limit, now=0.0) is True
    assert limiter.hit("ip1", "/admin", limit, now=0.0) is False

    # Different bucket — should still be allowed.
    assert limiter.hit("ip1", "/pubsub/inbox", limit, now=0.0) is True


# ---------------------------------------------------------------------------
# client_ip unit tests
# ---------------------------------------------------------------------------


def _make_request(headers: dict[str, str], client_host: str | None = "10.0.0.1") -> Request:
    """Build a minimal Starlette Request from a dict of headers."""
    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    if client_host is not None:
        scope["client"] = (client_host, 12345)
    return Request(scope)


def test_client_ip_parses_first_xff_entry() -> None:
    """X-Forwarded-For with multiple IPs: return the first (leftmost) one."""
    request = _make_request({"x-forwarded-for": "1.2.3.4, 5.6.7.8"})
    assert client_ip(request) == "1.2.3.4"


def test_client_ip_strips_xff_whitespace() -> None:
    """Whitespace around the first XFF entry is stripped."""
    request = _make_request({"x-forwarded-for": "  1.2.3.4  , 5.6.7.8"})
    assert client_ip(request) == "1.2.3.4"


def test_client_ip_falls_back_to_client_host() -> None:
    """When no XFF header is present, fall back to request.client.host."""
    request = _make_request({}, client_host="9.8.7.6")
    assert client_ip(request) == "9.8.7.6"


def test_client_ip_returns_unknown_when_no_client_or_xff() -> None:
    """When neither XFF nor client is available, return 'unknown'."""
    request = _make_request({}, client_host=None)
    assert client_ip(request) == "unknown"


# ---------------------------------------------------------------------------
# RateLimitMiddleware integration tests
# ---------------------------------------------------------------------------


def _make_app(limit: int = 2) -> FastAPI:
    """Build a minimal FastAPI app with the middleware applied."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, rules={"/admin": limit})

    @app.get("/admin/x")
    async def admin_x() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/other")
    async def other() -> dict[str, str]:
        return {"ok": "true"}

    return app


def test_middleware_allows_up_to_limit_then_429() -> None:
    """3 sequential hits with limit=2: first two 200, third 429."""
    app = _make_app(limit=2)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Forwarded-For": "1.1.1.1"}

    r1 = client.get("/admin/x", headers=headers)
    assert r1.status_code == 200

    r2 = client.get("/admin/x", headers=headers)
    assert r2.status_code == 200

    r3 = client.get("/admin/x", headers=headers)
    assert r3.status_code == 429


def test_middleware_429_has_retry_after_header() -> None:
    """A 429 response must include a Retry-After header."""
    app = _make_app(limit=1)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Forwarded-For": "2.2.2.2"}

    client.get("/admin/x", headers=headers)  # exhaust
    r = client.get("/admin/x", headers=headers)
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_middleware_non_matched_path_never_limited() -> None:
    """/other is not in the rules and should never be rate-limited."""
    app = _make_app(limit=1)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"X-Forwarded-For": "3.3.3.3"}

    for _ in range(10):
        r = client.get("/other", headers=headers)
        assert r.status_code == 200


def test_middleware_per_ip_isolation() -> None:
    """Distinct IPs are tracked independently — one exhausted IP doesn't block another."""
    app = _make_app(limit=1)
    client = TestClient(app, raise_server_exceptions=False)

    # Exhaust ip A.
    r_a1 = client.get("/admin/x", headers={"X-Forwarded-For": "4.4.4.4"})
    assert r_a1.status_code == 200
    r_a2 = client.get("/admin/x", headers={"X-Forwarded-For": "4.4.4.4"})
    assert r_a2.status_code == 429

    # ip B should still be allowed.
    r_b = client.get("/admin/x", headers={"X-Forwarded-For": "5.5.5.5"})
    assert r_b.status_code == 200
