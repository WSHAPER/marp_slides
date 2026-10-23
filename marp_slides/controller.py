"""FastAPI application: lifecycle, auth middleware and TaskProcessing registration."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, set_handlers

from .settings import PROVIDER_ID, Settings
from .task_definition import PROVIDER, TASK_TYPE
from .task_worker import (
    APP_ENABLED,
    THREAD_STOP_EVENT,
    start_bg_thread,
    stop_bg_thread,
    trigger_handler,
)

LOGGER = logging.getLogger("marp_slides.controller")


def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    try:
        if enabled:
            nc.providers.task_processing.register(PROVIDER, TASK_TYPE)
            APP_ENABLED.set()
            if THREAD_STOP_EVENT.is_set():
                # If the threads were previously stopped, we start them again,
                # otherwise the lifecycle handler has already started them.
                start_bg_thread(Settings.from_env())
        else:
            APP_ENABLED.clear()
            nc.providers.task_processing.unregister(PROVIDER_ID)
            stop_bg_thread()
    except Exception as exc:
        LOGGER.exception("Error in enabled handler")
        return f"Error in enabled handler: {exc}"

    LOGGER.info("App %s", "enabled" if enabled else "disabled")
    return ""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    set_handlers(app, enabled_handler, trigger_handler=trigger_handler)
    settings = Settings.from_env()
    try:
        nc = NextcloudApp()
        nc_app_enabled = nc.enabled_state
    except ValueError as exc:
        # Missing NEXTCLOUD_URL/APP_ID/APP_SECRET env: the heartbeat still
        # serves; the worker will retry client creation in its loop.
        LOGGER.warning("Nextcloud client not configured yet: %s", exc)
        nc_app_enabled = False
    if nc_app_enabled:
        APP_ENABLED.set()
    start_bg_thread(settings)
    LOGGER.info("App enable state at startup: %s", nc_app_enabled)
    yield
    stop_bg_thread()


app = FastAPI(lifespan=lifespan)
app.add_middleware(AppAPIAuthMiddleware)


@app.get("/")
def _(request: Request) -> str:
    """Server check."""
    return f"Hello, {request.scope.get('username', 'anon')}!"
