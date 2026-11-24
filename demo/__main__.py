"""Run the demo: ``uv run python -m demo``."""

from __future__ import annotations

import os

import uvicorn

from .main import app


def main() -> None:
    host = os.environ.get("DEMO_HOST", "127.0.0.1")
    port = int(os.environ.get("DEMO_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
