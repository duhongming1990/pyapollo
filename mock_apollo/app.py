"""A tiny, protocol-compatible Apollo config service for demos and tests.

It implements the same endpoints the client uses:

* ``GET /configs/{appId}/{cluster}/{namespace}``
* ``GET /configfiles/{appId}/{cluster}/{namespace}``
* ``GET /notifications/v2`` (long polling)
* ``GET /configfiles/json/{appId}/{cluster}/{namespace}``
* ``GET /apps/{appId}/clusters/{cluster}/namespaces`` (discovery)
* ``GET /services/config`` (Meta Server discovery)

Plus an ``/admin`` API used to publish new releases and trigger hot updates.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from apollo_client.errors import ApolloParseError

from .store import ConfigStore, NamespaceEntry

_DEMO_APP_ID = "demo-app"
_DEMO_CLUSTER = "default"


class UpdateConfigRequest(BaseModel):
    """Admin payload used to publish a new namespace release."""

    content: str
    format: str | None = None


def create_app(
    store: ConfigStore | None = None,
    *,
    seed: bool = True,
    poll_timeout: float | None = None,
) -> FastAPI:
    """Build the mock Apollo FastAPI application."""

    store = store or ConfigStore(
        poll_timeout=poll_timeout if poll_timeout is not None else 1.5
    )
    if seed and not store.list(app_id=_DEMO_APP_ID, cluster=_DEMO_CLUSTER):
        store.seed_demo()

    app = FastAPI(
        title="Mock Apollo Config Service",
        version="0.2.0",
        description="Local Apollo config service used by the Apollo client demo.",
    )
    app.state.store = store

    # -- health / documentation ----------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index() -> dict[str, Any]:
        return {
            "service": "mock-apollo",
            "appId": _DEMO_APP_ID,
            "cluster": _DEMO_CLUSTER,
            "namespaces": [entry.namespace for entry in store.list()],
            "endpoints": [
                "/configs/{appId}/{cluster}/{namespace}",
                "/configfiles/{appId}/{cluster}/{namespace}",
                "/configfiles/json/{appId}/{cluster}/{namespace}",
                "/apps/{appId}/clusters/{cluster}/namespaces",
                "/services/config",
                "/notifications/v2",
                "/admin/configs",
                "/docs",
            ],
        }

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "namespaces": len(store.list()),
            "pollTimeout": store.poll_timeout,
        }

    # -- Apollo client protocol ----------------------------------------------
    @app.get("/configs/{app_id}/{cluster}/{namespace}")
    async def configs(
        app_id: str, cluster: str, namespace: str
    ) -> dict[str, Any]:
        entry = _require_entry(store, app_id, cluster, namespace)
        return {
            "appId": entry.app_id,
            "cluster": entry.cluster,
            "namespaceName": entry.namespace,
            "configurations": entry.configurations,
            "releaseKey": entry.release_key,
        }

    @app.get("/configfiles/json/{app_id}/{cluster}/{namespace}")
    async def config_file_json(
        app_id: str, cluster: str, namespace: str
    ) -> dict[str, str]:
        entry = _require_entry(store, app_id, cluster, namespace)
        return entry.configurations

    @app.get("/configfiles/{app_id}/{cluster}/{namespace}")
    async def config_file(
        app_id: str,
        cluster: str,
        namespace: str,
        format: str | None = Query(default=None),
    ) -> PlainTextResponse:
        entry = _require_entry(store, app_id, cluster, namespace)
        media_type = "text/plain"
        if format:
            media_type += f"; format={format}"
        return PlainTextResponse(entry.content, media_type=media_type)

    @app.get("/apps/{app_id}/clusters/{cluster}/namespaces")
    async def list_namespaces(app_id: str, cluster: str) -> list[str]:
        """Legacy Apollo namespace discovery endpoint."""

        return [entry.namespace for entry in store.list(app_id=app_id, cluster=cluster)]

    @app.get("/services/config")
    async def services_config(
        request: Request,
        appId: str | None = Query(default=None),
        ip: str | None = Query(default=None),
    ) -> list[str]:
        """Minimal Meta Server response pointing back at this mock instance."""

        host = request.url.hostname or "127.0.0.1"
        port = request.url.port or 80
        return [f"{host}:{port}"]

    @app.get("/notifications/v2")
    async def notifications(
        appId: str = Query(...),
        cluster: str = Query(default="default"),
        notifications: str = Query(...),
        timeout: float | None = Query(default=None),
    ) -> Response:
        try:
            requested = json.loads(notifications)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="notifications must be valid JSON"
            ) from exc
        if not isinstance(requested, list):
            raise HTTPException(
                status_code=400, detail="notifications must be a JSON list"
            )

        wait_seconds = min(
            max(timeout if timeout is not None else store.poll_timeout, 0.05),
            30.0,
        )
        changes = await store.wait_for_changes(
            appId, cluster, requested, wait_seconds
        )
        if changes is None:
            return Response(status_code=304)
        return Response(
            content=json.dumps(changes, ensure_ascii=False),
            media_type="application/json",
            headers={"Cache-Control": "no-cache"},
        )

    # -- admin API ------------------------------------------------------------
    @app.get("/admin/configs")
    async def admin_list(
        app_id: str | None = Query(default=None),
        cluster: str | None = Query(default=None),
    ) -> dict[str, Any]:
        entries = store.list(app_id=app_id, cluster=cluster)
        return {
            "count": len(entries),
            "namespaces": [entry.summary() for entry in entries],
        }

    @app.get("/admin/configs/{app_id}/{cluster}/{namespace}")
    async def admin_get(
        app_id: str, cluster: str, namespace: str
    ) -> dict[str, Any]:
        entry = _require_entry(store, app_id, cluster, namespace)
        return entry.summary(include_content=True)

    @app.put("/admin/configs/{app_id}/{cluster}/{namespace}")
    async def admin_put(
        app_id: str,
        cluster: str,
        namespace: str,
        payload: UpdateConfigRequest,
    ) -> dict[str, Any]:
        try:
            entry = await store.set_config(
                app_id,
                cluster,
                namespace,
                payload.content,
                fmt=payload.format,
            )
        except ApolloParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return entry.summary(include_content=True)

    @app.delete("/admin/configs/{app_id}/{cluster}/{namespace}")
    async def admin_delete(
        app_id: str, cluster: str, namespace: str
    ) -> dict[str, Any]:
        removed = await store.delete_config(app_id, cluster, namespace)
        if not removed:
            raise HTTPException(status_code=404, detail="namespace not found")
        return {"deleted": True, "namespace": namespace}

    return app


def _require_entry(
    store: ConfigStore, app_id: str, cluster: str, namespace: str
) -> NamespaceEntry:
    entry = store.get(app_id, cluster, namespace)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"namespace {namespace!r} not found for app "
                f"{app_id!r} cluster {cluster!r}"
            ),
        )
    return entry
