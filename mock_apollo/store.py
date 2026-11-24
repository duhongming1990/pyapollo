"""In-memory config store for the mock Apollo config service."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from apollo_client.config import detect_format, normalize_format
from apollo_client.parser import parse_content, parse_properties

DEFAULT_PROPERTIES = """\
# Demo properties namespace (Apollo "application")
app.name=Apollo Python Client
app.version=0.2.0
app.feature.new_ui=true
app.limits.max=100
logging.level=INFO
"""

DEFAULT_YAML = """\
server:
  host: 0.0.0.0
  port: 8080
  read_timeout: 15
feature:
  flags:
    - beta
    - dark-mode
  refresh_interval: 30
database:
  url: postgresql://localhost:5432/demo
  pool:
    min: 2
    max: 10
"""


@dataclass(slots=True)
class NamespaceEntry:
    """One namespace release held by the mock server."""

    app_id: str
    cluster: str
    namespace: str
    format: str
    content: str
    release_key: str
    notification_id: int
    updated_at: float

    @property
    def configurations(self) -> dict[str, str]:
        """Return the payload shape used by Apollo's ``/configs`` endpoint."""

        if self.format == "properties":
            return parse_properties(self.content)
        return {self.namespace: self.content}

    def summary(self, *, include_content: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "appId": self.app_id,
            "cluster": self.cluster,
            "namespace": self.namespace,
            "format": self.format,
            "releaseKey": self.release_key,
            "notificationId": self.notification_id,
            "updatedAt": self.updated_at,
        }
        if include_content:
            payload["content"] = self.content
        return payload


class ConfigStore:
    """Simple in-memory store with Apollo notification semantics."""

    def __init__(self, *, poll_timeout: float = 1.5) -> None:
        self.poll_timeout = poll_timeout
        self._entries: dict[tuple[str, str, str], NamespaceEntry] = {}
        self._condition = asyncio.Condition()
        self._next_notification_id = 0

    # -- reads ----------------------------------------------------------------
    def get(
        self, app_id: str, cluster: str, namespace: str
    ) -> NamespaceEntry | None:
        return self._entries.get((app_id, cluster, namespace))

    def list(
        self,
        *,
        app_id: str | None = None,
        cluster: str | None = None,
    ) -> list[NamespaceEntry]:
        entries = [
            entry
            for (entry_app, entry_cluster, _), entry in self._entries.items()
            if (app_id is None or entry_app == app_id)
            and (cluster is None or entry_cluster == cluster)
        ]
        return sorted(
            entries,
            key=lambda entry: (entry.app_id, entry.cluster, entry.namespace),
        )

    # -- writes ---------------------------------------------------------------
    async def set_config(
        self,
        app_id: str,
        cluster: str,
        namespace: str,
        content: str,
        *,
        fmt: str | None = None,
    ) -> NamespaceEntry:
        """Create a release and wake up all waiting long polls."""

        resolved_format = (
            normalize_format(fmt) if fmt else detect_format(namespace)
        )
        # Validate before mutating so a bad admin request cannot poison the store.
        parse_content(content, resolved_format)

        async with self._condition:
            entry = self._put(
                app_id, cluster, namespace, resolved_format, content
            )
            self._condition.notify_all()
            return entry

    async def delete_config(
        self, app_id: str, cluster: str, namespace: str
    ) -> bool:
        async with self._condition:
            removed = self._entries.pop((app_id, cluster, namespace), None)
            if removed is not None:
                self._condition.notify_all()
            return removed is not None

    # -- long polling ---------------------------------------------------------
    async def wait_for_changes(
        self,
        app_id: str,
        cluster: str,
        requested: list[dict[str, Any]],
        timeout: float,
    ) -> list[dict[str, Any]] | None:
        """Block until a requested namespace changes or ``timeout`` expires.

        Returns the Apollo notification list, or ``None`` for "no change"
        (which the HTTP layer turns into ``304 Not Modified``).
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async with self._condition:
            while True:
                changes: list[dict[str, Any]] = []
                for item in requested:
                    namespace = str(item.get("namespaceName", ""))
                    try:
                        requested_id = int(item.get("notificationId", -1))
                    except (TypeError, ValueError):
                        requested_id = -1
                    entry = self._entries.get((app_id, cluster, namespace))
                    current_id = entry.notification_id if entry else -1
                    if current_id != requested_id:
                        changes.append(
                            {
                                "namespaceName": namespace,
                                "notificationId": current_id,
                            }
                        )
                if changes:
                    return changes

                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    return None

    # -- demo data ------------------------------------------------------------
    def seed_demo(
        self, app_id: str = "demo-app", cluster: str = "default"
    ) -> list[NamespaceEntry]:
        """Populate one properties and one YAML namespace."""

        return [
            self._put(
                app_id,
                cluster,
                "application",
                "properties",
                DEFAULT_PROPERTIES,
            ),
            self._put(app_id, cluster, "demo.yaml", "yaml", DEFAULT_YAML),
        ]

    # -- internals ------------------------------------------------------------
    def _put(
        self,
        app_id: str,
        cluster: str,
        namespace: str,
        fmt: str,
        content: str,
    ) -> NamespaceEntry:
        self._next_notification_id += 1
        entry = NamespaceEntry(
            app_id=app_id,
            cluster=cluster,
            namespace=namespace,
            format=fmt,
            content=content,
            release_key=f"{self._next_notification_id}-{uuid.uuid4().hex[:12]}",
            notification_id=self._next_notification_id,
            updated_at=time.time(),
        )
        self._entries[(app_id, cluster, namespace)] = entry
        return entry
