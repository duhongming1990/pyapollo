"""Shared helpers for the test suite."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import requests
import uvicorn


class UvicornTestServer:
    """Run a FastAPI app in a background thread on a free local port."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.port = free_port()
        self.config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            access_log=False,
            lifespan="on",
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(
            target=self.server.run,
            name=f"uvicorn-test-{self.port}",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "UvicornTestServer":
        self.thread.start()
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if not self.thread.is_alive():
                raise RuntimeError("uvicorn test server exited during startup")
            try:
                response = requests.get(f"{self.base_url}/health", timeout=0.25)
            except requests.RequestException:
                time.sleep(0.05)
                continue
            if response.status_code == 200:
                return self
            time.sleep(0.05)
        raise RuntimeError(f"uvicorn test server did not start on {self.base_url}")

    def stop(self) -> None:
        if not self.thread.is_alive():
            return
        self.server.should_exit = True
        self.thread.join(timeout=10.0)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(timeout=5.0)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(
    predicate,
    *,
    timeout: float = 10.0,
    interval: float = 0.05,
    message: str = "condition",
):
    """Poll ``predicate`` until it returns a truthy value."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}")


def publish_config(
    base_url: str,
    namespace: str,
    content: str,
    *,
    fmt: str | None = None,
    app_id: str = "demo-app",
    cluster: str = "default",
) -> dict[str, Any]:
    """Publish a new namespace release through the mock admin API."""

    payload: dict[str, Any] = {"content": content}
    if fmt is not None:
        payload["format"] = fmt
    response = requests.put(
        f"{base_url}/admin/configs/{app_id}/{cluster}/{namespace}",
        json=payload,
        timeout=5.0,
    )
    response.raise_for_status()
    return response.json()
