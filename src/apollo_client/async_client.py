"""Native ``asyncio`` Apollo client.

The async client mirrors :class:`apollo_client.ApolloClient`: it supports the
same formats, long-polling/polling hot updates, namespace discovery, Meta
Server discovery, heartbeat reconciliation, disk caching and runtime option
updates, but performs all HTTP work with :mod:`httpx` on the running event
loop.

``httpx`` is an optional dependency::

    pip install "pyapollo-config-client[async]"

Configuration reads are plain in-memory lookups and stay synchronous (they
never touch the network); only operations that may perform I/O are ``async``.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised only when the extra is missing
    import httpx
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "AsyncApolloClient requires httpx. Install it with "
        '`pip install "pyapollo-config-client[async]"`.'
    ) from exc

from ._version import __version__
from .cache import DiskCache
from .client import (
    WATCH_MODES,
    _looks_like_properties_file,
    _single_configuration_value,
    _stringify_property_value,
)
from .config import (
    ChangeEvent,
    NamespaceConfig,
    detect_format,
    diff_configs,
)
from .env import options_from_env
from .errors import (
    ApolloAuthError,
    ApolloConnectionError,
    ApolloError,
    ApolloHTTPError,
    ApolloParseError,
    NamespaceNotFoundError,
)
from .meta import MetaServerResolver
from .parser import parse_content, serialize_properties
from .transport import prepare_signed_request
from .utils import coerce_bool

AsyncChangeListener = Callable[[ChangeEvent], Any]

_MISSING = object()
_NOT_FOUND_RETRY_SECONDS = 30.0
_MAX_BACKOFF_SECONDS = 30.0

_UNSET = object()


class AsyncApolloTransport:
    """Async equivalent of :class:`apollo_client.transport.ApolloTransport`."""

    def __init__(
        self,
        config_server_url: str | Sequence[str],
        app_id: str,
        cluster: str,
        *,
        secret: str | None = None,
        timeout: float = 10.0,
        long_poll_timeout: float = 60.0,
        ip: str | None = None,
        headers: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._urls = _normalise_server_urls(config_server_url)
        self._app_id = app_id
        self._cluster = cluster
        self._secret = secret
        self._timeout = timeout
        self._long_poll_timeout = long_poll_timeout
        self._ip = ip
        self._headers = dict(headers or {})
        self._logger = logger or logging.getLogger(__name__)

        self._preferred = 0
        self._state_lock = threading.Lock()
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    # -- state ----------------------------------------------------------------
    @property
    def base_urls(self) -> tuple[str, ...]:
        with self._state_lock:
            return self._urls

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def cluster(self) -> str:
        return self._cluster

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def long_poll_timeout(self) -> float:
        return self._long_poll_timeout

    @property
    def ip(self) -> str | None:
        return self._ip

    def update_options(
        self,
        *,
        config_server_url: str | Sequence[str] | object = _UNSET,
        app_id: str | object = _UNSET,
        cluster: str | object = _UNSET,
        secret: str | None | object = _UNSET,
        timeout: float | object = _UNSET,
        long_poll_timeout: float | object = _UNSET,
        ip: str | None | object = _UNSET,
        headers: Mapping[str, str] | None | object = _UNSET,
    ) -> AsyncApolloTransport:
        with self._state_lock:
            if config_server_url is not _UNSET:
                self._urls = _normalise_server_urls(config_server_url)  # type: ignore[arg-type]
                self._preferred = 0
            if app_id is not _UNSET:
                self._app_id = str(app_id)
            if cluster is not _UNSET:
                self._cluster = str(cluster)
            if secret is not _UNSET:
                self._secret = secret  # type: ignore[assignment]
            if timeout is not _UNSET:
                self._timeout = float(timeout)  # type: ignore[arg-type]
            if long_poll_timeout is not _UNSET:
                self._long_poll_timeout = float(long_poll_timeout)  # type: ignore[arg-type]
            if ip is not _UNSET:
                self._ip = ip  # type: ignore[assignment]
            if headers is not _UNSET:
                self._headers = dict(headers or {})  # type: ignore[arg-type]
        return self

    def reopen(self) -> None:
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - closing is best effort
                pass

    # -- protocol -------------------------------------------------------------
    async def get_config(
        self, namespace: str, *, release_key: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if release_key:
            params["releaseKey"] = release_key
        if self._ip:
            params["ip"] = self._ip
        path = (
            f"/configs/{_quote(self._app_id)}"
            f"/{_quote(self._cluster)}/{_quote(namespace)}"
        )
        response = await self._request(path, params)
        _raise_for_config_status(response, namespace)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApolloParseError(
                f"Apollo returned non-JSON data for namespace {namespace!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ApolloParseError(
                f"Apollo returned an unexpected payload for namespace "
                f"{namespace!r}: {type(payload).__name__}"
            )
        return payload

    async def get_config_file(self, namespace: str, fmt: str) -> str:
        params: dict[str, Any] = {"format": fmt}
        if self._ip:
            params["ip"] = self._ip
        path = (
            f"/configfiles/{_quote(self._app_id)}"
            f"/{_quote(self._cluster)}/{_quote(namespace)}"
        )
        response = await self._request(path, params)
        _raise_for_config_status(response, namespace)
        return response.text

    async def notifications(
        self,
        notifications: Mapping[str, int],
        *,
        timeout: float | None = None,
    ) -> list[dict[str, Any]] | None:
        if not notifications:
            return None
        payload = [
            {"namespaceName": namespace, "notificationId": int(notification_id)}
            for namespace, notification_id in notifications.items()
        ]
        params: dict[str, Any] = {
            "appId": self._app_id,
            "cluster": self._cluster,
            "notifications": json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ),
        }
        if self._ip:
            params["ip"] = self._ip

        wait_seconds = (
            timeout if timeout is not None else self._long_poll_timeout
        )
        read_timeout = max(wait_seconds + 10.0, 70.0)
        response = await self._request(
            "/notifications/v2",
            params,
            timeout=httpx.Timeout(
                min(self._timeout, 10.0), read=read_timeout
            ),
        )
        if response.status_code == 304:
            return None
        if response.status_code in (401, 403):
            raise ApolloAuthError(
                "Apollo rejected the request (check app id / secret)",
                status_code=response.status_code,
                url=str(response.url),
                body=_body_snippet(response),
            )
        if response.status_code != 200:
            raise ApolloHTTPError(
                "unexpected response from Apollo notifications endpoint",
                status_code=response.status_code,
                url=str(response.url),
                body=_body_snippet(response),
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ApolloParseError(
                "Apollo notifications response is not valid JSON"
            ) from exc
        if not isinstance(data, list):
            raise ApolloParseError(
                "Apollo notifications response must be a JSON list"
            )

        changes: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            namespace = item.get("namespaceName")
            notification_id = item.get("notificationId")
            if not isinstance(namespace, str) or notification_id is None:
                continue
            try:
                notification_id = int(notification_id)
            except (TypeError, ValueError):
                continue
            changes.append(
                {
                    "namespaceName": namespace,
                    "notificationId": notification_id,
                }
            )
        return changes

    async def list_namespaces(self) -> tuple[str, ...]:
        path = (
            f"/apps/{_quote(self._app_id)}"
            f"/clusters/{_quote(self._cluster)}/namespaces"
        )
        response = await self._request(path)
        if response.status_code in (401, 403):
            raise ApolloAuthError(
                "Apollo rejected the namespace discovery request",
                status_code=response.status_code,
                url=str(response.url),
                body=_body_snippet(response),
            )
        if response.status_code != 200:
            raise ApolloHTTPError(
                "namespace discovery is not available on this Apollo release",
                status_code=response.status_code,
                url=str(response.url),
                body=_body_snippet(response),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ApolloParseError(
                "Apollo namespace discovery returned non-JSON data"
            ) from exc
        if isinstance(payload, Mapping):
            payload = payload.get("namespaces") or payload.get("data") or []
        if not isinstance(payload, list):
            raise ApolloParseError(
                "Apollo namespace discovery must return a JSON list"
            )
        names: list[str] = []
        for item in payload:
            if isinstance(item, str):
                name = item
            elif isinstance(item, Mapping):
                name = item.get("namespaceName") or item.get("namespace")
            else:
                continue
            if isinstance(name, str) and name and name not in names:
                names.append(name)
        return tuple(names)

    # -- internals ------------------------------------------------------------
    def prepare(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> tuple[str, dict[str, str]]:
        """Build URL+headers for the first configured server (debug/tests)."""

        urls = self.base_urls
        return prepare_signed_request(
            urls[0],
            path,
            params or {},
            app_id=self._app_id,
            secret=self._secret,
            headers=self._headers,
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise ApolloConnectionError("Apollo transport is closed")
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": f"apollo-client-python/{__version__}"}
            )
        return self._client

    async def _request(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        if self._closed:
            raise ApolloConnectionError("Apollo transport is closed")

        clean_params = {
            key: value for key, value in (params or {}).items() if value is not None
        }
        with self._state_lock:
            urls = self._urls
            secret = self._secret
            app_id = self._app_id
            headers = self._headers
            preferred = self._preferred
        if not urls:  # pragma: no cover - guarded by update_options
            raise ApolloConnectionError("no Apollo config server is configured")

        client = await self._get_client()
        failures: list[str] = []
        for offset in range(len(urls)):
            index = (preferred + offset) % len(urls)
            base_url = urls[index]
            url, request_headers = prepare_signed_request(
                base_url,
                path,
                clean_params,
                app_id=app_id,
                secret=secret,
                headers=headers,
            )
            try:
                response = await client.get(
                    url,
                    headers=request_headers,
                    timeout=timeout if timeout is not None else self._timeout,
                )
            except httpx.HTTPError as exc:
                failures.append(f"{base_url}: {exc}")
                continue
            if response.status_code >= 500:
                failures.append(f"{base_url}: HTTP {response.status_code}")
                continue
            with self._state_lock:
                self._preferred = index
            return response
        raise ApolloConnectionError(
            "all Apollo config servers failed: " + "; ".join(failures)
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"AsyncApolloTransport(app_id={self._app_id!r}, "
            f"cluster={self._cluster!r}, servers={list(self.base_urls)!r})"
        )


@dataclass(frozen=True, slots=True)
class _Listener:
    callback: AsyncChangeListener
    namespace: str | None
    keys: frozenset[str] | None


class AsyncApolloClient:
    """Native asyncio Apollo client (see :class:`ApolloClient` for options)."""

    def __init__(
        self,
        app_id: str,
        config_server_url: str | Sequence[str] = "http://localhost:8080",
        cluster: str = "default",
        namespaces: Iterable[str] | None = ("application",),
        *,
        secret: str | None = None,
        timeout: float = 10.0,
        long_poll_timeout: float = 60.0,
        ip: str | None = None,
        cache_dir: str | os.PathLike[str] | None = None,
        cache_enabled: bool = True,
        preload_cache: bool = True,
        namespace_formats: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        watch_mode: str = "long_poll",
        poll_interval: float = 60.0,
        meta_server_url: str | Sequence[str] | None = None,
        meta_refresh_interval: float = 300.0,
        auto_discover: bool = False,
        heartbeat_interval: float | None = 600.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if not app_id or not str(app_id).strip():
            raise ValueError("app_id is required")
        if not cluster or not str(cluster).strip():
            raise ValueError("cluster is required")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if long_poll_timeout <= 0:
            raise ValueError("long_poll_timeout must be greater than zero")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if meta_refresh_interval <= 0:
            raise ValueError("meta_refresh_interval must be greater than zero")
        if heartbeat_interval is not None and heartbeat_interval < 0:
            raise ValueError("heartbeat_interval must be zero (disabled) or positive")
        mode = str(watch_mode).strip().lower()
        if mode not in WATCH_MODES:
            raise ValueError(
                f"unsupported watch_mode {watch_mode!r}; expected one of "
                f"{sorted(WATCH_MODES)}"
            )

        self.app_id = str(app_id)
        self.cluster = str(cluster)
        self._logger = logger or logging.getLogger(f"apollo_client.{self.app_id}")
        self._format_overrides = {
            str(namespace): fmt
            for namespace, fmt in (namespace_formats or {}).items()
        }
        self._watch_mode = mode
        self._poll_interval = float(poll_interval)
        self._meta_refresh_interval = float(meta_refresh_interval)
        self._auto_discover = bool(auto_discover)
        self._heartbeat_interval: float | None = (
            float(heartbeat_interval) if heartbeat_interval else None
        )
        self._ip = str(ip) if ip else None
        self._cache_enabled = cache_enabled
        self._cache_dir = cache_dir

        if isinstance(namespaces, str):
            namespace_list = [namespaces]
        else:
            namespace_list = list(namespaces or [])
        if not namespace_list:
            namespace_list = ["application"]

        self._namespaces: dict[str, None] = {}
        self._formats: dict[str, str] = {}
        for namespace in namespace_list:
            name = str(namespace)
            if name in self._namespaces:
                continue
            self._namespaces[name] = None
            self._formats[name] = detect_format(
                name, self._format_overrides.get(name)
            )

        self._configs: dict[str, NamespaceConfig] = {}
        self._notification_ids: dict[str, int] = {
            namespace: -1 for namespace in self._namespaces
        }
        self._not_found_until: dict[str, float] = {}
        self._fetch_errors: dict[str, str] = {}
        self._listeners: list[_Listener] = []

        self._lock = threading.RLock()
        self._fetch_lock = asyncio.Lock()
        self._sequence = itertools.count(1)
        self._started = False
        self._long_poll_timeout = float(long_poll_timeout)
        self._watch_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None

        self._last_pull_at: float = 0.0
        self._last_heartbeat_at: float = 0.0
        self._last_heartbeat_mono: float = 0.0
        self._heartbeat_count: int = 0
        self._heartbeat_ok: bool | None = None
        self._heartbeat_error: str | None = None
        self._last_meta_refresh_mono: float = 0.0

        self._transport = AsyncApolloTransport(
            config_server_url,
            self.app_id,
            self.cluster,
            secret=secret,
            timeout=timeout,
            long_poll_timeout=self._long_poll_timeout,
            ip=self._ip,
            headers=headers,
            logger=self._logger,
        )
        self._meta_resolver = (
            MetaServerResolver(
                meta_server_url,
                self.app_id,
                ip=self._ip,
                timeout=min(float(timeout), 10.0),
                headers=headers,
                logger=self._logger,
            )
            if meta_server_url is not None
            else None
        )
        self._cache = (
            DiskCache(cache_dir, self.app_id, self.cluster, logger=self._logger)
            if cache_enabled
            else None
        )
        if preload_cache and self._cache is not None:
            self._preload_cache()

    # -- lifecycle ------------------------------------------------------------
    async def start(
        self, *, fetch: bool = True, watch: bool = True
    ) -> AsyncApolloClient:
        self._transport.reopen()
        with self._lock:
            self._started = True

        if self._meta_resolver is not None:
            await self._refresh_meta()

        if self._auto_discover:
            try:
                discovered = await self.discover_namespaces()
                self._logger.info(
                    "discovered Apollo namespaces for app=%s: %s",
                    self.app_id,
                    ", ".join(discovered) or "<none>",
                )
            except ApolloError as exc:
                self._logger.warning(
                    "Apollo namespace discovery failed for app=%s: %s",
                    self.app_id,
                    exc,
                )

        if fetch:
            for namespace in self._snapshot_namespaces():
                await self._fetch_and_store(namespace, emit=False)

        if watch:
            with self._lock:
                self._last_heartbeat_mono = time.monotonic()
                self._last_meta_refresh_mono = time.monotonic()
            if self._watch_task is None or self._watch_task.done():
                self._watch_task = asyncio.create_task(
                    self._watch_loop(), name=f"apollo-watch-{self.app_id}"
                )
            if (
                self._maintenance_task is None
                or self._maintenance_task.done()
            ):
                self._maintenance_task = asyncio.create_task(
                    self._maintenance_loop(),
                    name=f"apollo-maintenance-{self.app_id}",
                )
        return self

    async def stop(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        with self._lock:
            self._started = False
            tasks = [self._watch_task, self._maintenance_task]
            self._watch_task = None
            self._maintenance_task = None

        pending = [task for task in tasks if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending and wait:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:  # pragma: no cover - slow request
                self._logger.debug("async Apollo tasks still shutting down")
        await self._transport.close()
        self._logger.info("stopped async Apollo client for app=%s", self.app_id)

    async def __aenter__(self) -> AsyncApolloClient:
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> bool:
        await self.stop()
        return False

    # -- refreshing -----------------------------------------------------------
    async def refresh(
        self, namespace: str | None = None
    ) -> dict[str, ChangeEvent | None]:
        if namespace is not None:
            namespace = str(namespace)
            self._register_namespace(namespace)
            targets = [namespace]
        else:
            targets = self._snapshot_namespaces()

        events: dict[str, ChangeEvent | None] = {}
        for target in targets:
            events[target] = await self._fetch_and_store(
                target, emit=True, raise_on_error=True
            )
        return events

    async def aget_config(
        self, namespace: str = "application", *, auto_fetch: bool = True
    ) -> NamespaceConfig | None:
        """Async variant of :meth:`get_config` that may perform a pull."""

        namespace = str(namespace)
        with self._lock:
            config = self._configs.get(namespace)
        if config is not None:
            return config

        self._register_namespace(namespace)
        if self._cache is not None:
            cached = await asyncio.to_thread(self._cache.load, namespace)
            if cached is not None:
                with self._lock:
                    self._configs.setdefault(namespace, cached)
                config = cached
        if not auto_fetch:
            with self._lock:
                return self._configs.get(namespace)

        with self._lock:
            retry_at = self._not_found_until.get(namespace)
        if retry_at is not None and time.monotonic() < retry_at:
            return config

        await self._fetch_and_store(namespace, emit=False, raise_on_error=False)
        with self._lock:
            return self._configs.get(namespace)

    def get_config(
        self, namespace: str = "application"
    ) -> NamespaceConfig | None:
        """Return the current snapshot (never touches the network).

        The disk cache is consulted on first access.  Use :meth:`aget_config`
        or :meth:`start` when a network pull is needed.
        """

        namespace = str(namespace)
        with self._lock:
            config = self._configs.get(namespace)
        if config is not None:
            return config
        self._register_namespace(namespace)
        if self._cache is not None:
            cached = self._cache.load(namespace)
            if cached is not None:
                with self._lock:
                    self._configs.setdefault(namespace, cached)
                config = cached
        return config

    async def discover_namespaces(
        self, *, refresh: bool = True
    ) -> tuple[str, ...]:
        names = await self._transport.list_namespaces()
        added = [name for name in names if self._register_namespace(name)]
        if refresh:
            for name in added:
                await self._fetch_and_store(name, emit=False)
        return names

    # -- runtime options ------------------------------------------------------
    async def update_options(self, **options: Any) -> AsyncApolloClient:
        """Async variant of :meth:`ApolloClient.update_options`."""

        allowed = {
            "app_id",
            "cluster",
            "config_server_url",
            "meta_server_url",
            "secret",
            "timeout",
            "long_poll_timeout",
            "ip",
            "watch_mode",
            "poll_interval",
            "heartbeat_interval",
            "meta_refresh_interval",
            "namespaces",
            "headers",
        }
        unknown = sorted(set(options) - allowed)
        if unknown:
            raise TypeError(
                "unknown AsyncApolloClient option(s): " + ", ".join(unknown)
            )
        if not options:
            return self

        new_app_id = options.get("app_id")
        new_cluster = options.get("cluster")
        if new_app_id is not None and (
            not isinstance(new_app_id, str) or not new_app_id.strip()
        ):
            raise ValueError("app_id must be a non-empty string")
        if new_cluster is not None and (
            not isinstance(new_cluster, str) or not new_cluster.strip()
        ):
            raise ValueError("cluster must be a non-empty string")
        new_watch_mode = options.get("watch_mode")
        if new_watch_mode is not None:
            new_watch_mode = str(new_watch_mode).strip().lower()
            if new_watch_mode not in WATCH_MODES:
                raise ValueError(
                    f"unsupported watch_mode {new_watch_mode!r}; expected one "
                    f"of {sorted(WATCH_MODES)}"
                )
        if options.get("poll_interval") is not None and float(
            options["poll_interval"]
        ) <= 0:
            raise ValueError("poll_interval must be greater than zero")
        if options.get("meta_refresh_interval") is not None and float(
            options["meta_refresh_interval"]
        ) <= 0:
            raise ValueError("meta_refresh_interval must be greater than zero")
        if (
            "heartbeat_interval" in options
            and options["heartbeat_interval"] is not None
            and float(options["heartbeat_interval"]) < 0
        ):
            raise ValueError(
                "heartbeat_interval must be zero (disabled) or positive"
            )
        for name in ("timeout", "long_poll_timeout"):
            if name in options and float(options[name]) <= 0:
                raise ValueError(f"{name} must be greater than zero")

        meta_provided = "meta_server_url" in options
        meta_value = options.get("meta_server_url")
        new_resolver: MetaServerResolver | None = None

        with self._lock:
            old_app, old_cluster = self.app_id, self.cluster
            transport_options: dict[str, Any] = {}
            if "config_server_url" in options:
                transport_options["config_server_url"] = options[
                    "config_server_url"
                ]
            if new_app_id is not None:
                self.app_id = new_app_id
                transport_options["app_id"] = self.app_id
            if new_cluster is not None:
                self.cluster = new_cluster
                transport_options["cluster"] = self.cluster
            for name in ("secret", "timeout", "long_poll_timeout", "headers"):
                if name in options:
                    transport_options[name] = options[name]
            if "ip" in options:
                self._ip = str(options["ip"]) if options["ip"] else None
                transport_options["ip"] = self._ip
            if transport_options:
                self._transport.update_options(**transport_options)

            if new_watch_mode is not None:
                self._watch_mode = new_watch_mode
            if options.get("poll_interval") is not None:
                self._poll_interval = float(options["poll_interval"])
            if options.get("meta_refresh_interval") is not None:
                self._meta_refresh_interval = float(
                    options["meta_refresh_interval"]
                )
            if "heartbeat_interval" in options:
                heartbeat = options["heartbeat_interval"]
                self._heartbeat_interval = float(heartbeat) if heartbeat else None

            if meta_provided:
                if meta_value is None:
                    self._meta_resolver = None
                else:
                    new_resolver = MetaServerResolver(
                        meta_value,
                        self.app_id,
                        ip=self._ip,
                        timeout=min(
                            float(
                                options.get("timeout", self._transport.timeout)
                            ),
                            10.0,
                        ),
                        headers=options.get("headers"),
                        logger=self._logger,
                    )
                    self._meta_resolver = new_resolver
            elif self._meta_resolver is not None and (
                new_app_id is not None or "ip" in options
            ):
                self._meta_resolver.update_options(
                    app_id=self.app_id, ip=self._ip
                )

            identity_changed = (
                self.app_id != old_app or self.cluster != old_cluster
            )
            if identity_changed:
                self._configs.clear()
                self._notification_ids = {
                    namespace: -1 for namespace in self._namespaces
                }
                self._not_found_until.clear()
                self._fetch_errors.clear()
                self._last_pull_at = 0.0
                self._cache = (
                    DiskCache(
                        self._cache_dir,
                        self.app_id,
                        self.cluster,
                        logger=self._logger,
                    )
                    if self._cache_enabled
                    else None
                )
                if self._cache is not None:
                    self._preload_cache()
                self._logger.info(
                    "async Apollo client switched to app=%s cluster=%s",
                    self.app_id,
                    self.cluster,
                )

            namespaces = options.get("namespaces")
            added: list[str] = []
            if namespaces is not None:
                if isinstance(namespaces, str):
                    namespaces = [namespaces]
                for namespace in namespaces:
                    name = str(namespace)
                    if self._register_namespace(name):
                        added.append(name)

        for namespace in added:
            await self._fetch_and_store(namespace, emit=False)

        if new_resolver is not None:
            await self._refresh_meta()
        return self

    # -- reading --------------------------------------------------------------
    def get_value(
        self,
        key: str,
        default: Any = None,
        namespace: str = "application",
    ) -> Any:
        config = self.get_config(namespace)
        if config is None:
            return default
        return config.get(key, default)

    def get_str(
        self,
        key: str,
        default: str | None = None,
        namespace: str = "application",
    ) -> str | None:
        value = self.get_value(key, _MISSING, namespace)
        if value is _MISSING or value is None:
            return default
        return value if isinstance(value, str) else str(value)

    def get_int(
        self,
        key: str,
        default: int | None = None,
        namespace: str = "application",
    ) -> int | None:
        value = self.get_value(key, _MISSING, namespace)
        if value is _MISSING or value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_float(
        self,
        key: str,
        default: float | None = None,
        namespace: str = "application",
    ) -> float | None:
        value = self.get_value(key, _MISSING, namespace)
        if value is _MISSING or value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_bool(
        self,
        key: str,
        default: bool | None = None,
        namespace: str = "application",
    ) -> bool | None:
        value = self.get_value(key, _MISSING, namespace)
        if value is _MISSING or value is None:
            return default
        parsed = coerce_bool(value)
        return default if parsed is None else parsed

    def get_json(
        self,
        key: str,
        default: Any = None,
        namespace: str = "application",
    ) -> Any:
        value = self.get_value(key, _MISSING, namespace)
        if value is _MISSING or value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return default
        return value

    def get_configs(self) -> dict[str, NamespaceConfig]:
        with self._lock:
            return dict(self._configs)

    # -- listeners ------------------------------------------------------------
    def add_listener(
        self,
        callback: AsyncChangeListener,
        *,
        namespace: str | None = None,
        keys: Iterable[str] | None = None,
    ) -> Callable[[], None]:
        """Register a sync or async change listener.

        Coroutine callbacks are awaited by the watcher task.  Exceptions are
        logged and do not stop the watcher.
        """

        if not callable(callback):
            raise TypeError("listener must be callable")
        entry = _Listener(
            callback=callback,
            namespace=str(namespace) if namespace is not None else None,
            keys=frozenset(str(key) for key in keys) if keys else None,
        )
        with self._lock:
            self._listeners.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners.remove(entry)
                except ValueError:
                    pass

        return unsubscribe

    def on_change(
        self,
        callback: AsyncChangeListener | None = None,
        *,
        namespace: str | None = None,
        keys: Iterable[str] | None = None,
    ):
        def register(func: AsyncChangeListener) -> AsyncChangeListener:
            self.add_listener(func, namespace=namespace, keys=keys)
            return func

        if callback is not None:
            return register(callback)
        return register

    # -- introspection --------------------------------------------------------
    @property
    def namespaces(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._namespaces)

    @property
    def config_server_urls(self) -> tuple[str, ...]:
        return self._transport.base_urls

    @property
    def cache_directory(self) -> Path | None:
        return self._cache.directory if self._cache is not None else None

    @property
    def watch_mode(self) -> str:
        return self._watch_mode

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

    @property
    def heartbeat_interval(self) -> float | None:
        return self._heartbeat_interval

    @property
    def meta_server_urls(self) -> tuple[str, ...]:
        resolver = self._meta_resolver
        return resolver.meta_server_urls if resolver is not None else ()

    @property
    def is_running(self) -> bool:
        return (
            self._watch_task is not None
            and not self._watch_task.done()
        )

    @property
    def is_started(self) -> bool:
        return self._started

    def format_for(self, namespace: str) -> str:
        with self._lock:
            return self._formats.get(str(namespace)) or detect_format(
                str(namespace), self._format_overrides.get(str(namespace))
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "appId": self.app_id,
                "cluster": self.cluster,
                "started": self._started,
                "watching": self.is_running,
                "watchMode": self._watch_mode,
                "pollInterval": self._poll_interval,
                "heartbeatInterval": self._heartbeat_interval,
                "heartbeatCount": self._heartbeat_count,
                "lastHeartbeatAt": self._last_heartbeat_at or None,
                "heartbeatOk": self._heartbeat_ok,
                "heartbeatError": self._heartbeat_error,
                "lastPullAt": self._last_pull_at or None,
                "namespaces": list(self._namespaces),
                "formats": dict(self._formats),
                "configServers": list(self._transport.base_urls),
                "metaServers": (
                    list(self._meta_resolver.meta_server_urls)
                    if self._meta_resolver is not None
                    else []
                ),
                "cacheDirectory": (
                    str(self._cache.directory) if self._cache is not None else None
                ),
            }

    @classmethod
    def from_env(cls, **defaults: Any) -> AsyncApolloClient:
        return cls(**options_from_env(defaults))

    # -- internals ------------------------------------------------------------
    def _preload_cache(self) -> None:
        assert self._cache is not None
        for namespace in self._snapshot_namespaces():
            cached = self._cache.load(namespace)
            if cached is None:
                continue
            with self._lock:
                self._configs.setdefault(namespace, cached)
                if namespace not in self._format_overrides:
                    self._formats[namespace] = cached.format

    def _snapshot_namespaces(self) -> list[str]:
        with self._lock:
            return list(self._namespaces)

    def _register_namespace(self, namespace: str) -> bool:
        namespace = str(namespace)
        if not namespace:
            raise ValueError("namespace must not be empty")
        with self._lock:
            if namespace in self._namespaces:
                return False
            self._namespaces[namespace] = None
            self._formats[namespace] = detect_format(
                namespace, self._format_overrides.get(namespace)
            )
            self._notification_ids.setdefault(namespace, -1)
            return True

    async def _fetch_and_store(
        self,
        namespace: str,
        *,
        notification_id: int | None = None,
        emit: bool = True,
        raise_on_error: bool = False,
    ) -> ChangeEvent | None:
        identity = (self.app_id, self.cluster)
        try:
            new_config = await self._fetch_remote(
                namespace, notification_id=notification_id
            )
        except NamespaceNotFoundError:
            with self._lock:
                if notification_id is not None:
                    self._notification_ids[namespace] = int(notification_id)
                self._not_found_until[namespace] = (
                    time.monotonic() + _NOT_FOUND_RETRY_SECONDS
                )
                self._fetch_errors.pop(namespace, None)
            if raise_on_error:
                raise
            self._logger.warning(
                "namespace %s does not exist in Apollo app %s",
                namespace,
                self.app_id,
            )
            return None
        except ApolloError as exc:
            with self._lock:
                self._fetch_errors[namespace] = str(exc)
            if raise_on_error:
                raise
            self._logger.warning(
                "failed to pull namespace %s from Apollo: %s", namespace, exc
            )
            return None

        if new_config is None:
            return None

        should_save = False
        cache = None
        with self._lock:
            if (self.app_id, self.cluster) != identity:
                return None
            self._not_found_until.pop(namespace, None)
            self._fetch_errors.pop(namespace, None)
            self._last_pull_at = time.time()
            if notification_id is not None:
                self._notification_ids[namespace] = int(notification_id)
            old_config = self._configs.get(namespace)
            data_changed = old_config is None or old_config.data != new_config.data
            release_changed = (
                old_config is None
                or old_config.release_key != new_config.release_key
            )
            source_changed = (
                old_config is None or old_config.source != new_config.source
            )
            self._configs[namespace] = new_config

            event: ChangeEvent | None = None
            if emit and old_config is not None and data_changed:
                changes = diff_configs(old_config, new_config)
                if changes:
                    event = ChangeEvent(
                        sequence=next(self._sequence),
                        namespace=namespace,
                        changes=changes,
                        old=old_config,
                        new=new_config,
                    )

            if self._cache is not None and (
                data_changed or release_changed or source_changed
            ):
                should_save = True
                cache = self._cache

        if should_save and cache is not None:
            await asyncio.to_thread(cache.save, new_config)

        if event is not None:
            await self._dispatch(event)
        return event

    async def _fetch_remote(
        self, namespace: str, *, notification_id: int | None
    ) -> NamespaceConfig | None:
        fmt = self.format_for(namespace)
        current_notification_id = (
            int(notification_id)
            if notification_id is not None
            else self._notification_ids.get(namespace, -1)
        )
        async with self._fetch_lock:
            payload = await self._transport.get_config(namespace)
            if payload is None:  # pragma: no cover
                return None
            return await self._build_config(
                namespace,
                fmt,
                payload,
                notification_id=current_notification_id,
            )

    async def _build_config(
        self,
        namespace: str,
        fmt: str,
        payload: Mapping[str, Any],
        *,
        notification_id: int,
    ) -> NamespaceConfig:
        configurations = payload.get("configurations") or {}
        if isinstance(configurations, str):
            try:
                configurations = json.loads(configurations)
            except ValueError as exc:
                raise ApolloParseError(
                    f"namespace {namespace!r} returned malformed configurations"
                ) from exc
        if not isinstance(configurations, Mapping):
            raise ApolloParseError(
                f"namespace {namespace!r} returned configurations of type "
                f"{type(configurations).__name__}"
            )

        if fmt == "properties" and not _looks_like_properties_file(
            configurations, namespace
        ):
            data: dict[str, Any] = {
                str(key): _stringify_property_value(value)
                for key, value in configurations.items()
            }
            raw = serialize_properties(data)
        else:
            raw = _single_configuration_value(configurations)
            if raw is None:
                try:
                    raw = await self._transport.get_config_file(namespace, fmt)
                except ApolloError as exc:
                    raise ApolloParseError(
                        f"cannot find {fmt} content for namespace "
                        f"{namespace!r} in the Apollo payload"
                    ) from exc
            data = parse_content(raw, fmt)

        release_key = payload.get("releaseKey")
        if release_key is not None and not isinstance(release_key, str):
            release_key = str(release_key)

        return NamespaceConfig(
            namespace=namespace,
            format=fmt,
            data=data,
            raw=raw,
            release_key=release_key,
            notification_id=notification_id,
            source="remote",
            app_id=self.app_id,
            cluster=self.cluster,
        )

    async def _dispatch(self, event: ChangeEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            if (
                listener.namespace is not None
                and listener.namespace != event.namespace
            ):
                continue
            if (
                listener.keys is not None
                and not listener.keys.intersection(event.changes)
            ):
                continue
            try:
                result = listener.callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # pragma: no cover - user callback failure
                self._logger.exception(
                    "Apollo change listener failed for namespace %s",
                    event.namespace,
                )

    # -- background tasks -----------------------------------------------------
    async def _wait_for(
        self,
        seconds: float,
        *,
        expected_mode: str | None = None,
    ) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if expected_mode is not None and self._watch_mode != expected_mode:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.25))

    async def _watch_loop(self) -> None:
        backoff = 1.0
        self._logger.info(
            "async Apollo watcher started for app=%s", self.app_id
        )
        try:
            while True:
                if self._watch_mode == "poll":
                    failed = await self._poll_once()
                    if failed:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2.0, _MAX_BACKOFF_SECONDS)
                    else:
                        backoff = 1.0
                        await self._wait_for(
                            self._poll_interval, expected_mode="poll"
                        )
                    continue

                with self._lock:
                    notifications = {
                        namespace: self._notification_ids.get(namespace, -1)
                        for namespace in self._namespaces
                    }
                    timeout = self._long_poll_timeout
                if not notifications:
                    await asyncio.sleep(1.0)
                    continue

                try:
                    changes = await self._transport.notifications(
                        notifications, timeout=timeout
                    )
                except ApolloError as exc:
                    self._logger.warning(
                        "async Apollo long poll failed for app=%s: %s "
                        "(retrying in %.1fs)",
                        self.app_id,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, _MAX_BACKOFF_SECONDS)
                    continue
                except Exception:  # pragma: no cover - defensive
                    self._logger.exception(
                        "unexpected async Apollo watcher error for app=%s",
                        self.app_id,
                    )
                    await asyncio.sleep(1.0)
                    continue

                backoff = 1.0
                if changes is None:
                    continue
                for item in changes:
                    namespace = item["namespaceName"]
                    notification_id = item["notificationId"]
                    if namespace not in self._namespaces:
                        self._register_namespace(namespace)
                    await self._fetch_and_store(
                        namespace,
                        notification_id=notification_id,
                        emit=True,
                    )
        except asyncio.CancelledError:
            self._logger.info(
                "async Apollo watcher stopped for app=%s", self.app_id
            )
            raise

    async def _poll_once(self) -> bool:
        failed = False
        for namespace in self._snapshot_namespaces():
            try:
                await self._fetch_and_store(
                    namespace, emit=True, raise_on_error=True
                )
            except NamespaceNotFoundError:
                continue
            except ApolloError:
                failed = True
        return failed

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                now = time.monotonic()
                wait = 1.0
                deadlines: list[float] = []
                if self._meta_resolver is not None:
                    deadlines.append(
                        self._last_meta_refresh_mono
                        + self._meta_refresh_interval
                    )
                    if (
                        now
                        >= self._last_meta_refresh_mono
                        + self._meta_refresh_interval
                    ):
                        await self._refresh_meta()
                heartbeat_interval = self._heartbeat_interval
                if heartbeat_interval:
                    deadlines.append(
                        self._last_heartbeat_mono + heartbeat_interval
                    )
                    if (
                        now
                        >= self._last_heartbeat_mono + heartbeat_interval
                    ):
                        await self._heartbeat_once()
                if deadlines:
                    wait = min(deadlines) - time.monotonic()
                await asyncio.sleep(max(0.05, min(wait, 0.5)))
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                self._logger.exception(
                    "unexpected async Apollo maintenance error for app=%s",
                    self.app_id,
                )
                await asyncio.sleep(1.0)

    async def _heartbeat_once(self) -> None:
        errors: list[str] = []
        with self._lock:
            namespaces = list(self._namespaces)
            retry_at = dict(self._not_found_until)
        now = time.monotonic()
        for namespace in namespaces:
            retry = retry_at.get(namespace)
            if retry is not None and now < retry:
                continue
            await self._fetch_and_store(namespace, emit=True)
            with self._lock:
                error = self._fetch_errors.get(namespace)
            if error:
                errors.append(f"{namespace}: {error}")

        with self._lock:
            self._last_heartbeat_at = time.time()
            self._last_heartbeat_mono = time.monotonic()
            self._heartbeat_count += 1
            self._heartbeat_ok = not errors
            self._heartbeat_error = "; ".join(errors) if errors else None
        if errors:
            self._logger.warning(
                "async Apollo heartbeat for app=%s reported errors: %s",
                self.app_id,
                self._heartbeat_error,
            )

    async def _refresh_meta(self) -> bool:
        resolver = self._meta_resolver
        if resolver is None:
            return False
        try:
            urls = await asyncio.to_thread(resolver.refresh)
        except ApolloError as exc:
            self._last_meta_refresh_mono = time.monotonic()
            self._logger.warning(
                "Apollo Meta Server refresh failed for app=%s: %s",
                self.app_id,
                exc,
            )
            return False

        with self._lock:
            if urls and urls != self._transport.base_urls:
                self._transport.update_options(config_server_url=urls)
            self._last_meta_refresh_mono = time.monotonic()
        return True

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"AsyncApolloClient(app_id={self.app_id!r}, "
            f"cluster={self.cluster!r}, namespaces={list(self._namespaces)!r}, "
            f"mode={self._watch_mode!r}, running={self.is_running})"
        )


def _normalise_server_urls(
    config_server_url: str | Sequence[str],
) -> tuple[str, ...]:
    if isinstance(config_server_url, str):
        raw_urls = [
            part.strip()
            for part in config_server_url.split(",")
            if part.strip()
        ]
    else:
        raw_urls = [
            str(part).strip() for part in config_server_url if str(part).strip()
        ]
    urls: list[str] = []
    for raw in raw_urls:
        url = raw if "://" in raw else f"http://{raw}"
        urls.append(url.rstrip("/"))
    if not urls:
        raise ValueError("at least one Apollo config server URL is required")
    return tuple(urls)


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _raise_for_config_status(
    response: httpx.Response, namespace: str
) -> None:
    if response.status_code == 200:
        return
    if response.status_code == 404:
        raise NamespaceNotFoundError(
            f"namespace {namespace!r} not found in Apollo",
            status_code=response.status_code,
            url=str(response.url),
            body=_body_snippet(response),
        )
    if response.status_code in (401, 403):
        raise ApolloAuthError(
            f"Apollo rejected access to namespace {namespace!r}",
            status_code=response.status_code,
            url=str(response.url),
            body=_body_snippet(response),
        )
    raise ApolloHTTPError(
        f"unexpected response while fetching namespace {namespace!r}",
        status_code=response.status_code,
        url=str(response.url),
        body=_body_snippet(response),
    )


def _body_snippet(response: httpx.Response, limit: int = 300) -> str:
    try:
        return (response.text or "")[:limit]
    except Exception:  # pragma: no cover - defensive
        return ""
