"""Apollo Meta Server discovery.

Apollo deployments usually expose a Meta Server (``/services/config``) that
returns the Config Service addresses an application should use.  Resolving the
list at start-up (and refreshing it periodically) lets the client follow
Config Service scaling and failovers without a redeploy.

The public endpoint returns a JSON list of ``"host:port"`` strings.  Some
deployments wrap the list in an object or return ``{"homepageUrl": ...}``
entries instead; :func:`parse_meta_response` accepts both shapes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

import requests

from .errors import (
    ApolloConnectionError,
    ApolloParseError,
)


class MetaServerResolver:
    """Resolve Config Service URLs from one or more Apollo Meta Servers."""

    def __init__(
        self,
        meta_server_url: str | Sequence[str],
        app_id: str,
        *,
        ip: str | None = None,
        timeout: float = 5.0,
        headers: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        urls = _normalise_meta_urls(meta_server_url)
        if not urls:
            raise ValueError("at least one Apollo Meta Server URL is required")
        if not app_id or not str(app_id).strip():
            raise ValueError("app_id is required for Meta Server discovery")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")

        self._urls = urls
        self._app_id = str(app_id)
        self._ip = ip
        self._timeout = timeout
        self._headers = dict(headers or {})
        self._logger = logger or logging.getLogger(__name__)

        self._lock = threading.Lock()
        self._cached: tuple[str, ...] = ()
        self._resolved_at: float = 0.0
        self._last_error: str | None = None

    # -- introspection --------------------------------------------------------
    @property
    def meta_server_urls(self) -> tuple[str, ...]:
        return self._urls

    @property
    def cached_urls(self) -> tuple[str, ...]:
        with self._lock:
            return self._cached

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def app_id(self) -> str:
        return self._app_id

    def update_options(
        self,
        *,
        app_id: str | None = None,
        ip: str | None = None,
        timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> MetaServerResolver:
        """Update parameters used by the next resolution."""

        with self._lock:
            if app_id is not None:
                self._app_id = str(app_id)
            if ip is not None:
                self._ip = ip
            if timeout is not None:
                if timeout <= 0:
                    raise ValueError("timeout must be greater than zero")
                self._timeout = float(timeout)
            if headers is not None:
                self._headers = dict(headers)
        return self

    # -- resolution -----------------------------------------------------------
    def resolve(self, *, force: bool = False) -> tuple[str, ...]:
        """Return Config Service URLs, querying the Meta Server when needed.

        Without ``force`` a previously resolved list is returned as-is; the
        client decides when to refresh it.  Raises :class:`ApolloError` when
        every Meta Server fails.
        """

        if not force:
            with self._lock:
                if self._cached:
                    return self._cached
        return self.refresh()

    def refresh(self) -> tuple[str, ...]:
        """Force a Meta Server query and update the cached URL list."""

        urls = self._fetch()
        with self._lock:
            self._cached = urls
            self._resolved_at = time.time()
            self._last_error = None
        self._logger.info(
            "resolved Apollo config services for app=%s from meta server: %s",
            self._app_id,
            ", ".join(urls),
        )
        return urls

    # -- internals ------------------------------------------------------------
    def _fetch(self) -> tuple[str, ...]:
        params: dict[str, str] = {"appId": self._app_id}
        if self._ip:
            params["ip"] = self._ip

        failures: list[str] = []
        for base_url in self._urls:
            url = f"{base_url}/services/config"
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers=self._headers or None,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                failures.append(f"{base_url}: {exc}")
                continue

            if response.status_code != 200:
                failures.append(f"{base_url}: HTTP {response.status_code}")
                continue

            try:
                payload = response.json()
            except ValueError as exc:
                failures.append(f"{base_url}: invalid JSON")
                self._record_error(f"{base_url}: invalid JSON")
                raise ApolloParseError(
                    f"Apollo Meta Server {base_url!r} returned invalid JSON"
                ) from exc

            urls = parse_meta_response(payload)
            if not urls:
                failures.append(f"{base_url}: no config services")
                continue
            return urls

        message = "Apollo Meta Server discovery failed: " + "; ".join(failures)
        self._record_error(message)
        raise ApolloConnectionError(message)

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        self._logger.warning(message)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"MetaServerResolver(app_id={self._app_id!r}, "
            f"meta_servers={list(self._urls)!r})"
        )


def parse_meta_response(payload: Any) -> tuple[str, ...]:
    """Extract Config Service URLs from a Meta Server payload."""

    if isinstance(payload, Mapping):
        payload = (
            payload.get("configServers")
            or payload.get("services")
            or payload.get("data")
            or []
        )
    if not isinstance(payload, list):
        raise ApolloParseError(
            "Apollo Meta Server must return a JSON list of config services"
        )

    urls: list[str] = []
    for item in payload:
        candidate: str | None = None
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, Mapping):
            for key in ("homepageUrl", "url", "address", "host"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    candidate = value
                    break
            if candidate is None:
                host = item.get("ip") or item.get("host")
                port = item.get("port", 8080)
                if isinstance(host, str) and host:
                    candidate = f"{host}:{port}"
        if not candidate:
            continue

        candidate = candidate.strip()
        if not candidate:
            continue
        url = candidate if "://" in candidate else f"http://{candidate}"
        url = url.rstrip("/")
        if url not in urls:
            urls.append(url)
    return tuple(urls)


def _normalise_meta_urls(
    meta_server_url: str | Sequence[str],
) -> tuple[str, ...]:
    if isinstance(meta_server_url, str):
        raw = [
            part.strip()
            for part in meta_server_url.split(",")
            if part.strip()
        ]
    else:
        raw = [str(part).strip() for part in meta_server_url if str(part).strip()]

    urls: list[str] = []
    for item in raw:
        url = item if "://" in item else f"http://{item}"
        urls.append(url.rstrip("/"))
    return tuple(urls)
