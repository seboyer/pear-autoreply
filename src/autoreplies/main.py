"""FastAPI app entrypoint.

Wires the three route groups (health, pubsub, admin) and exposes the ASGI app
as `autoreplies.main:app` for uvicorn.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .config import get_settings
from .logging_config import configure_logging
from .routes import admin, health, pubsub


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks. Real client init goes here in later phases."""
    settings = get_settings()
    configure_logging(settings.log_level)
    logging.getLogger(__name__).info("autoreplies starting (env=%s)", settings.app_env)
    yield
    logging.getLogger(__name__).info("autoreplies shutting down")


app = FastAPI(
    title="Pear Autoreplies",
    description="Rental-platform lead autoreply pipeline.",
    version="0.1.0",
    lifespan=lifespan,
    # No interactive docs in production.
    docs_url="/docs" if get_settings().app_env != "production" else None,
    redoc_url=None,
)

app.include_router(health.router)
app.include_router(pubsub.router)
app.include_router(admin.router)
