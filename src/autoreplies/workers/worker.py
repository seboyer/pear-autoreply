"""RQ worker entrypoint.

Run via `python -m autoreplies.workers.worker`. Drains the default queue.
Phase 4 wires the queue name + retry policy from settings.
"""

import logging
import sys

from redis import Redis
from rq import Queue, Worker

from ..config import get_settings
from ..logging_config import configure_logging


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = logging.getLogger("autoreplies.worker")

    redis = Redis.from_url(settings.redis_url)
    queue = Queue("default", connection=redis)
    worker = Worker([queue], connection=redis)

    log.info("worker starting (env=%s, queue=%s)", settings.app_env, queue.name)
    worker.work(with_scheduler=False, logging_level=settings.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
