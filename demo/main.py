"""FastAPI demo for :mod:`apollo_client`.

Run the mock Apollo server first::

    uv run python -m mock_apollo

Then start the demo::

    uv run uvicorn demo.main:app --port 8000

Open http://127.0.0.1:8000 to see live config and hot updates.  The API is also
available directly (``/config``, ``/config/{namespace}/{key}``, ``/events`` ...).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from apollo_client import ApolloClient, ApolloError, ChangeEvent
from apollo_client.utils import jsonable

logger = logging.getLogger("apollo_client.demo")

_INDEX_PATH = Path(__file__).with_name("static") / "index.html"
_MISSING = object()


def create_app(client: ApolloClient | None = None) -> FastAPI:
    """Create the demo app, optionally with an injected client (tests)."""

    client = client or ApolloClient.from_env(
        app_id="demo-app",
        config_server_url=os.environ.get(
            "APOLLO_CONFIG_SERVER_URL",
            os.environ.get("APOLLO_CONFIG_SERVER", "http://127.0.0.1:18080"),
        ),
        namespaces=["application", "demo.yaml"],
        timeout=5.0,
        # Apollo's server-side long poll is 60s by default.  A shorter client
        # timeout would abort every poll just before the server answers 304.
        long_poll_timeout=60.0,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(client.start)
        logger.info(
            "Apollo demo ready: app=%s cluster=%s namespaces=%s",
            client.app_id,
            client.cluster,
            client.namespaces,
        )
        try:
            yield
        finally:
            await run_in_threadpool(partial(client.stop, timeout=2.0))

    app = FastAPI(
        title="Apollo Python Client Demo",
        version="0.2.0",
        description=(
            "Demo API for the YAML/properties Apollo client with hot updates, "
            "namespace discovery, Meta Server support and native asyncio. "
            "Use /events (SSE) to observe changes as they are applied."
        ),
        lifespan=lifespan,
    )
    app.state.apollo = client

    # -- pages ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        try:
            html = _INDEX_PATH.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - packaging issue
            raise HTTPException(
                status_code=500, detail=f"dashboard not available: {exc}"
            ) from exc
        return HTMLResponse(html)

    # -- status --------------------------------------------------------------
    @app.get("/health")
    async def health() -> dict[str, Any]:
        configs = client.get_configs()
        status = client.status()
        return {
            "status": "ok",
            "appId": client.app_id,
            "cluster": client.cluster,
            "watching": client.is_running,
            "watchMode": status["watchMode"],
            "metaServers": status["metaServers"],
            "lastPullAt": status["lastPullAt"],
            "lastHeartbeatAt": status["lastHeartbeatAt"],
            "heartbeatOk": status["heartbeatOk"],
            "cacheDirectory": (
                str(client.cache_directory) if client.cache_directory else None
            ),
            "configServers": list(client.config_server_urls),
            "namespaces": {
                namespace: {
                    "releaseKey": config.release_key,
                    "source": config.source,
                }
                for namespace, config in configs.items()
            },
        }

    # -- config API ----------------------------------------------------------
    @app.get("/config")
    async def get_all_config() -> dict[str, Any]:
        configs = client.get_configs()
        return {
            "appId": client.app_id,
            "cluster": client.cluster,
            "watching": client.is_running,
            "namespaces": {
                namespace: config.to_dict(include_raw=True)
                for namespace, config in configs.items()
            },
        }

    @app.get("/config/{namespace}")
    async def get_namespace_config(namespace: str) -> dict[str, Any]:
        config = client.get_config(namespace, auto_fetch=False)
        if config is None:
            raise HTTPException(
                status_code=404,
                detail=f"namespace {namespace!r} is not loaded",
            )
        return config.to_dict(include_raw=True)

    @app.get("/config/{namespace}/{key:path}")
    async def get_config_value(namespace: str, key: str) -> dict[str, Any]:
        value = client.get_value(key, _MISSING, namespace=namespace)
        if value is _MISSING:
            raise HTTPException(
                status_code=404,
                detail=f"key {key!r} not found in namespace {namespace!r}",
            )
        return {
            "namespace": namespace,
            "key": key,
            "value": jsonable(value),
        }

    @app.post("/config/refresh")
    async def refresh_config(
        namespace: str | None = Query(default=None)
    ) -> dict[str, Any]:
        try:
            events = await run_in_threadpool(client.refresh, namespace)
        except ApolloError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        return {
            "refreshed": list(events),
            "events": {
                name: event.to_dict() if event is not None else None
                for name, event in events.items()
            },
        }

    # -- live events (SSE) ---------------------------------------------------
    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        queue: asyncio.Queue[ChangeEvent] = asyncio.Queue(maxsize=100)
        loop = asyncio.get_running_loop()

        def enqueue(event: ChangeEvent) -> None:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

        def on_change(event: ChangeEvent) -> None:
            try:
                loop.call_soon_threadsafe(enqueue, event)
            except RuntimeError:  # event loop already closed during shutdown
                pass

        unsubscribe = client.add_listener(on_change)

        async def stream():
            try:
                yield _sse(
                    "ready",
                    {
                        "appId": client.app_id,
                        "cluster": client.cluster,
                        "namespaces": list(client.namespaces),
                        "at": time.time(),
                    },
                )
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=2.0
                        )
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield _sse(
                        "config-change",
                        event.to_dict(),
                        event_id=event.sequence,
                    )
            finally:
                unsubscribe()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _sse(
    event: str,
    data: dict[str, Any],
    *,
    event_id: int | None = None,
) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(jsonable(data), ensure_ascii=False)
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


app = create_app()
