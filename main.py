#!/usr/bin/env python3
"""marp_slides ExApp entrypoint: run the FastAPI app under uvicorn."""

from __future__ import annotations

import logging
import os

from nc_py_api.ex_app import run_app

from marp_slides.controller import app
from marp_slides.settings import DEFAULT_APP_PORT, Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    settings = Settings.from_env()
    # run_app() requires APP_PORT and binds APP_HOST (default 127.0.0.1, which
    # is not reachable from the Docker network).
    os.environ.setdefault("APP_PORT", str(settings.app_port or DEFAULT_APP_PORT))
    os.environ.setdefault("APP_HOST", "0.0.0.0")
    run_app(
        uvicorn_app=app,
        http="h11",
        interface="asgi3",
        log_level="info",
        timeout_keep_alive=120,
    )


if __name__ == "__main__":
    main()
