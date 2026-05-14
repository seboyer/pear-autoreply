"""Daily scheduler entrypoint.

Run via `python -m autoreplies.workers.scheduler`. Sleeps in a loop, doing the
following on a fixed cadence:
- Renew `users.watch` on every agent mailbox (Gmail watches expire after 7 days
  — we renew every 24h to leave headroom).
- Sweep stale Redis state keys (defense in depth — TTL handles most of it).

Phase 1 wires the actual watch-renewal call. Phase 0 just establishes the
container shape.
"""

import logging
import signal
import sys
import time

from ..config import get_settings
from ..logging_config import configure_logging

# Cadence: 24h between full passes.
LOOP_INTERVAL_SECONDS = 24 * 60 * 60


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = logging.getLogger("autoreplies.scheduler")

    stop = {"flag": False}

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("scheduler received signal %d, exiting", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("scheduler starting (env=%s, interval=%ds)",
             settings.app_env, LOOP_INTERVAL_SECONDS)

    while not stop["flag"]:
        try:
            _run_once()
        except Exception:
            log.exception("scheduler tick failed; will retry next interval")

        # Sleep in 1-second chunks so SIGTERM is responsive.
        for _ in range(LOOP_INTERVAL_SECONDS):
            if stop["flag"]:
                break
            time.sleep(1)

    return 0


def _run_once() -> None:
    """One pass of scheduled work. Phase 1 implements watch renewal."""
    # TODO Phase 1: iterate active agent mailboxes, call gmail.users.watch.
    pass


if __name__ == "__main__":
    sys.exit(main())
