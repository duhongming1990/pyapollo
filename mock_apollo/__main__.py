"""Run the mock Apollo config service: ``uv run python -m mock_apollo``."""

from __future__ import annotations

import os

import uvicorn

from .app import create_app


def main() -> None:
    host = os.environ.get("MOCK_APOLLO_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_APOLLO_PORT", "18080"))
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level=os.environ.get("MOCK_APOLLO_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
