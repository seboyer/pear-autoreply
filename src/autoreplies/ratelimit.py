"""App-level rate-limit middleware for Pear Autoreplies.

Replaces the Caddy ``rate_limit`` directive that is dropped in the Render
migration (Caddy cannot run on Render without the custom caddy-ratelimit
image).  Currently guards one path prefix:

- ``/admin/*`` — also bearer-gated; a second layer of defence against
  brute-forcing the admin token.

The middleware is generic (a ``{prefix: limit}`` map), so new prefixes can be
added in ``main.py`` without touching this module.

Implementation notes
--------------------
- Per-process in-memory store.  The effective rate limit scales linearly with
  ``web.numInstances`` (each instance keeps its own window).  This is fine for
  a blunt anti-abuse cap; if you need exact per-IP limits, keep ``web`` at
  ``numInstances: 1`` or move to a Redis-backed store.
- Fixed-window algorithm: simple, zero external dependencies, predictably
  bounded memory.
- Thread-safe via ``threading.Lock`` (uvicorn uses a thread-pool by default
  for sync routes; async handlers also share the process address space).
"""

import threading
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

RequestResponseEndpoint = Callable[[Request], Awaitable[Response]]


def client_ip(request: Request) -> str:
    """Return the client's IP address from the request.

    Render's load balancer sets the ``X-Forwarded-For`` header; we take the
    first (left-most) entry, which is the original client IP in a single-hop
    proxy topology.

    .. warning::
        ``X-Forwarded-For`` is trivially spoofable by a client that sends its
        own header before Render's LB appends the real hop.  This function is
        **defense-in-depth only** — do not rely on it for security-critical
        identity decisions.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


class _FixedWindowLimiter:
    """Thread-safe fixed-window per-(identity, bucket) rate limiter.

    Args:
        window_seconds: Length of each window in seconds.
        max_keys: Maximum number of (identity, bucket) pairs to track before
            performing an in-place GC pass (evicts stale windows).
    """

    def __init__(self, window_seconds: int = 60, max_keys: int = 100_000) -> None:
        self._window = window_seconds
        self._max_keys = max_keys
        # Maps (identity, bucket) -> (window_index, count)
        self._store: dict[tuple[str, str], tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(
        self,
        identity: str,
        bucket: str,
        limit: int,
        *,
        now: float | None = None,
    ) -> bool:
        """Record a hit and return ``True`` if within the limit, ``False`` if exceeded.

        Args:
            identity: Per-client discriminator (typically an IP address).
            bucket: Rate-limit bucket identifier (typically a path prefix).
            limit: Maximum number of allowed hits per window.
            now: Override current time (seconds since epoch).  Used in tests.

        Returns:
            ``True`` if the request is allowed; ``False`` if it should be
            rejected (limit exceeded for this window).
        """
        ts = now if now is not None else time.time()
        window_index = int(ts // self._window)
        key = (identity, bucket)

        with self._lock:
            # Cheap GC: if the store is over the key cap, drop all stale entries.
            if len(self._store) >= self._max_keys:
                self._store = {k: v for k, v in self._store.items() if v[0] == window_index}

            entry = self._store.get(key)
            if entry is None or entry[0] != window_index:
                # New window (or first ever hit): reset counter.
                self._store[key] = (window_index, 1)
                return True

            stored_index, count = entry
            if count >= limit:
                return False

            self._store[key] = (stored_index, count + 1)
            return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that enforces per-IP fixed-window rate limits.

    Args:
        app: The wrapped ASGI application.
        rules: Mapping of path prefix → requests-per-window.  Longer (more
            specific) prefixes take priority.
        window_seconds: Window duration in seconds (default: 60).
    """

    def __init__(
        self,
        app: ASGIApp,
        rules: dict[str, int],
        window_seconds: int = 60,
    ) -> None:
        super().__init__(app)
        # Sort longest prefix first so more-specific rules win.
        self._rules: list[tuple[str, int]] = sorted(
            rules.items(), key=lambda item: len(item[0]), reverse=True
        )
        self._window_seconds = window_seconds
        self._limiter = _FixedWindowLimiter(window_seconds=window_seconds)

    def _match(self, path: str) -> tuple[str, int] | None:
        """Return the first matching ``(prefix, limit)`` pair or ``None``."""
        for prefix, limit in self._rules:
            if path == prefix or path.startswith(prefix + "/"):
                return prefix, limit
        return None

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        match = self._match(request.url.path)
        if match is None:
            return await call_next(request)

        prefix, limit = match
        ip = client_ip(request)
        if not self._limiter.hit(ip, prefix, limit):
            return JSONResponse(
                {"detail": "Rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(self._window_seconds)},
            )

        return await call_next(request)
