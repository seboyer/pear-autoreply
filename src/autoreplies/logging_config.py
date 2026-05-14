"""Shared logging setup for the FastAPI app, worker, and scheduler entrypoints.

One place to change the format. Each entrypoint calls `configure_logging(level)`
at startup rather than calling `logging.basicConfig` itself.
"""

import logging

_FORMAT = '{"ts":"%(asctime)s","lvl":"%(levelname)s","name":"%(name)s","msg":"%(message)s"}'


def configure_logging(level: str) -> None:
    """Configure root logging for a process entrypoint."""
    logging.basicConfig(level=level.upper(), format=_FORMAT)
