"""A small, format-aware Apollo (Ctrip) config client.

Features
--------
* pull ``properties``, ``yaml``/``yml`` and ``json`` namespaces,
* long-polling **or** scheduled-polling hot updates with change events and
  per-key filters,
* namespace auto-discovery through ``/apps/.../namespaces``,
* Meta Server (``/services/config``) service discovery with periodic refresh,
* periodic heartbeat / reconciliation pulls that repair missed notifications,
* runtime parameter updates (``update_options``) without rebuilding the client,
* immutable namespace snapshots that are safe to read from request handlers,
* disk cache fallback when the config service is unavailable,
* HMAC-SHA1 ``secret`` authentication and multiple config server URLs.

The client is intentionally synchronous: config reads are in-memory dictionary
lookups, so they are cheap to call from FastAPI (or any other) request handlers.
Watcher, Meta Server refresh and heartbeat run in daemon threads per client.
For native ``asyncio`` use see :class:`apollo_client.AsyncApolloClient`.
"""

from __future__ import annotations

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

from .cache import DiskCache
from .config import (
    ChangeEvent,
    NamespaceConfig,
    detect_format,
    diff_configs,
)
from .env import options_from_env
from .errors import (
    ApolloError,
    ApolloParseError,
    NamespaceNotFoundError,
)
from .meta import MetaServerResolver
from .parser import parse_content, serialize_properties
from .transport import ApolloTransport
from .utils import coerce_bool

ChangeListener = Callable[[ChangeEvent], None]

WATCH_MODES = frozenset({"long_poll", "poll"})

_MISSING = object()
_NOT_FOUND_RETRY_SECONDS = 30.0
_MAX_BACKOFF_SECONDS = 30.0
_MAINTENANCE_SLICE_SECONDS = 0.5
_WAIT_SLICE_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class _Listener:
    callback: ChangeListener
    namespace: str | None
    keys: frozenset[str] | None


class ApolloClient:
    """Apollo configuration client with hot updates.

    Parameters
    ----------
    app_id:
        Apollo application id.
    config_server_url:
        One URL, a comma separated list, or a sequence of URLs.  A config
        server cluster is usually addressed as ``http://host:8080``.  When a
        Meta Server is configured these URLs act as the bootstrap list until
        the first successful discovery.
    cluster:
        Apollo cluster name (``default`` by default).
    namespaces:
        Namespaces to pull and watch.  Formats are inferred from suffixes:
        ``application`` -> properties, ``demo.yaml`` -> YAML, ``feature.json``
        -> JSON.  Use ``namespace_formats`` to override the inference.
    secret:
        Optional Apollo access secret (HMAC-SHA1 signing).
    timeout:
        Normal HTTP request timeout in seconds.
    long_poll_timeout:
        How long one ``/notifications/v2`` request may block server-side.
    ip:
        Client IP used by Apollo's grey (canary) release rules.
    watch_mode:
        ``"long_poll"`` (default) uses ``/notifications/v2``; ``"poll"``
        refreshes every ``poll_interval`` seconds instead.
    poll_interval:
        Seconds between scheduled pulls when ``watch_mode="poll"``.
    meta_server_url:
        Apollo Meta Server URL(s).  When set, ``/services/config`` is queried
        at start-up and every ``meta_refresh_interval`` seconds.
    meta_refresh_interval:
        How often the Meta Server is queried again.
    auto_discover:
        Discover namespaces from Apollo at start-up instead of relying only on
        ``namespaces``.
    heartbeat_interval:
        Interval between reconciliation pulls, or ``None``/``0`` to disable.
        Apollo notifications can be missed across restarts; the heartbeat
        re-verifies every namespace and emits regular change events.
    cache_dir:
        Base directory for the disk cache.  Defaults to ``APOLLO_CACHE_DIR``
        or ``~/.cache/apollo``.
    preload_cache:
        Load cached snapshots during construction so reads keep working before
        the first network pull.
    namespace_formats:
        Explicit ``{namespace: format}`` overrides.
    """

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
        watch_mode = _normalise_watch_mode(watch_mode)

        self.app_id = str(app_id)
        self.cluster = str(cluster)
        self._logger = logger or logging.getLogger(f"apollo_client.{self.app_id}")
        self._format_overrides = {
            str(namespace): fmt
            for namespace, fmt in (namespace_formats or {}).items()
        }
        self._watch_mode = watch_mode
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
        self._fetch_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._watch_generation = 0
        self._thread: threading.Thread | None = None
        self._maintenance_thread: threading.Thread | None = None
        self._started = False
        self._long_poll_timeout = float(long_poll_timeout)
        self._sequence = itertools.count(1)

        self._last_pull_at: float = 0.0
        self._last_heartbeat_at: float = 0.0
        self._last_heartbeat_mono: float = 0.0
        self._heartbeat_count: int = 0
        self._heartbeat_ok: bool | None = None
        self._heartbeat_error: str | None = None
        self._last_meta_refresh_mono: float = 0.0

        self._transport = ApolloTransport(
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
    def start(
        self, *, fetch: bool = True, watch: bool = True
    ) -> ApolloClient:
        """Pull the configured namespaces and optionally start hot updates.

        ``fetch`` performs a synchronous initial pull (remote first, cached
        snapshots remain usable on failure).  ``watch`` starts the watcher,
        heartbeat and Meta Server refresh daemon threads.  Calling ``start``
        again is harmless.
        """

        self._transport.reopen()
        with self._lock:
            self._started = True
            self._stop_event.clear()

        if self._meta_resolver is not None:
            self._refresh_meta()

        if self._auto_discover:
            try:
                discovered = self.discover_namespaces()
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
                try:
                    self._fetch_and_store(namespace, emit=False)
                except ApolloError as exc:  # pragma: no cover - defensive
                    self._logger.warning(
                        "initial pull for namespace %s failed: %s",
                        namespace,
                        exc,
                    )

        if watch:
            with self._lock:
                self._last_heartbeat_mono = time.monotonic()
                self._last_meta_refresh_mono = time.monotonic()
                if self._thread is None or not self._thread.is_alive():
                    self._watch_generation += 1
                    generation = self._watch_generation
                    self._thread = threading.Thread(
                        target=self._watch_loop,
                        args=(generation,),
                        name=f"apollo-watch-{self.app_id}",
                        daemon=True,
                    )
                    self._thread.start()
                else:
                    generation = self._watch_generation
                if (
                    self._maintenance_thread is None
                    or not self._maintenance_thread.is_alive()
                ):
                    self._maintenance_thread = threading.Thread(
                        target=self._maintenance_loop,
                        args=(generation,),
                        name=f"apollo-maintenance-{self.app_id}",
                        daemon=True,
                    )
                    self._maintenance_thread.start()
                self._logger.info(
                    "started Apollo watcher for app=%s mode=%s namespaces=%s",
                    self.app_id,
                    self._watch_mode,
                    self._snapshot_namespaces(),
                )
        return self

    def stop(self, *, wait: bool = True, timeout: float = 5.0) -> None:
        """Stop watcher/heartbeat threads and close HTTP sessions."""

        with self._lock:
            self._started = False
            self._watch_generation += 1
            self._stop_event.set()
            watch_thread = self._thread
            maintenance_thread = self._maintenance_thread
            self._thread = None
            self._maintenance_thread = None

        for thread in (watch_thread, maintenance_thread):
            if (
                wait
                and thread is not None
                and thread is not threading.current_thread()
            ):
                thread.join(timeout)

        alive = [
            thread
            for thread in (watch_thread, maintenance_thread)
            if thread is not None and thread.is_alive()
        ]
        if not alive:
            self._transport.close()
            self._logger.info("stopped Apollo client for app=%s", self.app_id)
        else:  # pragma: no cover - long poll still winding down
            self._logger.debug(
                "Apollo client for app=%s still finishing a request", self.app_id
            )

    def __enter__(self) -> ApolloClient:
        return self.start()

    def __exit__(self, *exc_info: object) -> bool:
        self.stop()
        return False

    # -- refreshing -----------------------------------------------------------
    def refresh(
        self, namespace: str | None = None
    ) -> dict[str, ChangeEvent | None]:
        """Force a pull for one (or all) namespaces and emit change events.

        Unlike the watcher, errors are raised so callers can report them.
        """

        if namespace is not None:
            namespace = str(namespace)
            self._register_namespace(namespace)
            targets = [namespace]
        else:
            targets = self._snapshot_namespaces()

        events: dict[str, ChangeEvent | None] = {}
        for target in targets:
            events[target] = self._fetch_and_store(
                target, emit=True, raise_on_error=True
            )
        return events

    def discover_namespaces(self, *, refresh: bool = True) -> tuple[str, ...]:
        """Discover namespaces published for this app/cluster.

        Uses ``/apps/{app}/clusters/{cluster}/namespaces``.  Newly discovered
        namespaces are registered (and pulled when ``refresh`` is true).  The
        complete namespace list is returned.  Raises :class:`ApolloError` when
        the discovery endpoint is unavailable.
        """

        names = self._transport.list_namespaces()
        added = [name for name in names if self._register_namespace(name)]
        if refresh:
            for name in added:
                try:
                    self._fetch_and_store(name, emit=False)
                except ApolloError:  # pragma: no cover - defensive
                    continue
        return names

    # -- runtime options ------------------------------------------------------
    def update_options(self, **options: Any) -> ApolloClient:
        """Update client parameters without rebuilding the client.

        Supported options: ``app_id``, ``cluster``, ``config_server_url``,
        ``meta_server_url``, ``secret``, ``timeout``, ``long_poll_timeout``,
        ``ip``, ``watch_mode``, ``poll_interval``, ``heartbeat_interval``,
        ``meta_refresh_interval``, ``namespaces`` and ``headers``.

        Changing ``app_id``/``cluster`` clears the in-memory snapshots (they
        belong to the old application) and switches the disk cache directory.
        Watcher and heartbeat threads pick up new intervals on their next
        iteration; a ``meta_server_url`` change also triggers an immediate
        Meta Server refresh.
        """

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
                "unknown ApolloClient option(s): " + ", ".join(unknown)
            )
        if not options:
            return self

        new_app_id = options.get("app_id")
        new_cluster = options.get("cluster")
        if new_app_id is not None and (not isinstance(new_app_id, str) or not new_app_id.strip()):
            raise ValueError("app_id must be a non-empty string")
        if new_cluster is not None and (
            not isinstance(new_cluster, str) or not new_cluster.strip()
        ):
            raise ValueError("cluster must be a non-empty string")

        new_watch_mode = options.get("watch_mode")
        if new_watch_mode is not None:
            new_watch_mode = _normalise_watch_mode(str(new_watch_mode))
        new_poll_interval = options.get("poll_interval")
        if new_poll_interval is not None and float(new_poll_interval) <= 0:
            raise ValueError("poll_interval must be greater than zero")
        new_meta_refresh_interval = options.get("meta_refresh_interval")
        if (
            new_meta_refresh_interval is not None
            and float(new_meta_refresh_interval) <= 0
        ):
            raise ValueError("meta_refresh_interval must be greater than zero")
        heartbeat = options.get("heartbeat_interval", _MISSING)
        if (
            heartbeat is not _MISSING
            and heartbeat is not None
            and float(heartbeat) < 0
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

            if "watch_mode" in options and new_watch_mode is not None:
                self._watch_mode = new_watch_mode
            if new_poll_interval is not None:
                self._poll_interval = float(new_poll_interval)
            if new_meta_refresh_interval is not None:
                self._meta_refresh_interval = float(new_meta_refresh_interval)
            if heartbeat is not _MISSING:
                self._heartbeat_interval = (
                    float(heartbeat) if heartbeat else None
                )

            if meta_provided:
                if meta_value is None:
                    self._meta_resolver = None
                else:
                    new_resolver = MetaServerResolver(
                        meta_value,
                        self.app_id,
                        ip=self._ip,
                        timeout=min(
                            float(options.get("timeout", self._transport.timeout)),
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
                    "Apollo client switched to app=%s cluster=%s",
                    self.app_id,
                    self.cluster,
                )

            namespaces = options.get("namespaces")
            if namespaces is not None:
                if isinstance(namespaces, str):
                    namespaces = [namespaces]
                for namespace in namespaces:
                    self._register_namespace(str(namespace))

        if new_resolver is not None:
            self._refresh_meta()

        return self

    # -- reading --------------------------------------------------------------
    def get_config(
        self, namespace: str = "application", *, auto_fetch: bool = True
    ) -> NamespaceConfig | None:
        """Return the current snapshot for ``namespace``.

        The first access to an unknown namespace registers it for watching and
        optionally performs a blocking pull.  ``auto_fetch=False`` is useful in
        status endpoints that must never block on the network.
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

        if not auto_fetch:
            with self._lock:
                return self._configs.get(namespace)

        with self._lock:
            retry_at = self._not_found_until.get(namespace)
        if retry_at is not None and time.monotonic() < retry_at:
            return config

        self._fetch_and_store(namespace, emit=False, raise_on_error=False)
        with self._lock:
            return self._configs.get(namespace)

    def get_value(
        self,
        key: str,
        default: Any = None,
        namespace: str = "application",
    ) -> Any:
        """Read a single value.

        Properties keys are matched exactly (dots included).  YAML/JSON keys
        may be addressed as dotted paths, e.g. ``server.port``.
        """

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
        """Return a copy of all currently loaded snapshots."""

        with self._lock:
            return dict(self._configs)

    # -- listeners ------------------------------------------------------------
    def add_listener(
        self,
        callback: ChangeListener,
        *,
        namespace: str | None = None,
        keys: Iterable[str] | None = None,
    ) -> Callable[[], None]:
        """Register a change listener and return an unsubscribe callable.

        ``namespace`` limits events to one namespace.  ``keys`` limits events
        to changes touching at least one of the given (dotted) keys.
        Callbacks run on the watcher thread and must be fast; exceptions are
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
        callback: ChangeListener | None = None,
        *,
        namespace: str | None = None,
        keys: Iterable[str] | None = None,
    ):
        """Decorator-friendly version of :meth:`add_listener`."""

        def register(func: ChangeListener) -> ChangeListener:
            self.add_listener(func, namespace=namespace, keys=keys)
            return func

        if callback is not None:
            return register(callback)
        return register

    # -- introspection --------------------------------------------------------
    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(self._snapshot_namespaces())

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
        with self._lock:
            thread = self._thread
            return (
                thread is not None
                and thread.is_alive()
                and not self._stop_event.is_set()
            )

    @property
    def is_started(self) -> bool:
        with self._lock:
            return self._started

    def format_for(self, namespace: str) -> str:
        with self._lock:
            return self._formats.get(str(namespace)) or detect_format(
                str(namespace), self._format_overrides.get(str(namespace))
            )

    def status(self) -> dict[str, Any]:
        """Return a JSON-friendly snapshot of client health and activity."""

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

    # -- environment ----------------------------------------------------------
    @classmethod
    def from_env(cls, **defaults: Any) -> ApolloClient:
        """Build a client from ``APOLLO_*`` environment variables.

        Environment values win over keyword ``defaults``, which makes
        ``ApolloClient.from_env(app_id="demo-app", ...)`` convenient for demo
        apps while keeping production configuration external.
        """

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
            self._logger.info(
                "preloaded cached Apollo config for %s (release=%s)",
                namespace,
                cached.release_key,
            )

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
            self._logger.info(
                "registered namespace %s for Apollo app %s",
                namespace,
                self.app_id,
            )
            return True

    def _fetch_and_store(
        self,
        namespace: str,
        *,
        notification_id: int | None = None,
        emit: bool = True,
        raise_on_error: bool = False,
    ) -> ChangeEvent | None:
        identity = (self.app_id, self.cluster)
        try:
            new_config = self._fetch_remote(
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

        with self._lock:
            if (self.app_id, self.cluster) != identity:
                self._logger.info(
                    "discarding stale Apollo config for %s (%s -> %s)",
                    namespace,
                    identity,
                    (self.app_id, self.cluster),
                )
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
                self._cache.save(new_config)

        if event is not None:
            if event.changed_keys:
                self._logger.info(
                    "namespace %s changed (release %s): %s",
                    namespace,
                    new_config.release_key,
                    ", ".join(event.changed_keys),
                )
            self._dispatch(event)
        return event

    def _fetch_remote(
        self, namespace: str, *, notification_id: int | None
    ) -> NamespaceConfig | None:
        fmt = self.format_for(namespace)
        current_notification_id = (
            int(notification_id)
            if notification_id is not None
            else self._notification_ids.get(namespace, -1)
        )
        with self._fetch_lock:
            payload = self._transport.get_config(namespace)
            if payload is None:  # pragma: no cover - /configs never returns 304 here
                return None
            return self._build_config(
                namespace,
                fmt,
                payload,
                notification_id=current_notification_id,
            )

    def _build_config(
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
                    raw = self._transport.get_config_file(namespace, fmt)
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

    def _dispatch(self, event: ChangeEvent) -> None:
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
                listener.callback(event)
            except Exception:  # pragma: no cover - user callback failure
                self._logger.exception(
                    "Apollo change listener failed for namespace %s",
                    event.namespace,
                )

    # -- background loops -----------------------------------------------------
    def _alive(self, generation: int) -> bool:
        return (
            not self._stop_event.is_set()
            and generation == self._watch_generation
        )

    def _wait_for(
        self,
        seconds: float,
        generation: int,
        *,
        expected_mode: str | None = None,
    ) -> bool:
        """Sleep in slices so stop/mode changes are noticed quickly."""

        deadline = time.monotonic() + max(0.0, seconds)
        while self._alive(generation):
            if expected_mode is not None and self._watch_mode != expected_mode:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self._stop_event.wait(min(remaining, _WAIT_SLICE_SECONDS))
        return False

    def _watch_loop(self, generation: int) -> None:
        backoff = 1.0
        self._logger.info(
            "Apollo watcher started for app=%s generation=%s",
            self.app_id,
            generation,
        )

        while self._alive(generation):
            if self._watch_mode == "poll":
                failed = self._poll_once(generation)
                if failed:
                    backoff = self._backoff(backoff, generation)
                else:
                    backoff = 1.0
                    self._wait_for(
                        self._poll_interval,
                        generation,
                        expected_mode="poll",
                    )
                continue

            with self._lock:
                notifications = {
                    namespace: self._notification_ids.get(namespace, -1)
                    for namespace in self._namespaces
                }
                timeout = self._long_poll_timeout

            if not notifications:
                self._wait_for(1.0, generation)
                continue

            try:
                changes = self._transport.notifications(
                    notifications, timeout=timeout
                )
            except ApolloError as exc:
                self._logger.warning(
                    "Apollo long poll failed for app=%s: %s "
                    "(retrying in %.1fs)",
                    self.app_id,
                    exc,
                    backoff,
                )
                backoff = self._backoff(backoff, generation)
                continue
            except Exception:  # pragma: no cover - defensive
                self._logger.exception(
                    "unexpected error in Apollo watcher for app=%s", self.app_id
                )
                self._wait_for(1.0, generation)
                continue

            backoff = 1.0
            if changes is None:
                continue

            for item in changes:
                if not self._alive(generation):
                    break
                namespace = item["namespaceName"]
                notification_id = item["notificationId"]
                if namespace not in self._namespaces:
                    self._register_namespace(namespace)
                self._fetch_and_store(
                    namespace,
                    notification_id=notification_id,
                    emit=True,
                )

        self._logger.info(
            "Apollo watcher stopped for app=%s generation=%s",
            self.app_id,
            generation,
        )

    def _poll_once(self, generation: int) -> bool:
        """Pull every registered namespace once.  Returns "any failure"."""

        failed = False
        for namespace in self._snapshot_namespaces():
            if not self._alive(generation):
                break
            try:
                self._fetch_and_store(
                    namespace, emit=True, raise_on_error=True
                )
            except NamespaceNotFoundError:
                continue
            except ApolloError:
                failed = True
        return failed

    def _backoff(self, backoff: float, generation: int) -> float:
        self._wait_for(backoff, generation)
        return min(backoff * 2.0, _MAX_BACKOFF_SECONDS)

    def _maintenance_loop(self, generation: int) -> None:
        self._logger.info(
            "Apollo maintenance loop started for app=%s "
            "(heartbeat=%ss metaRefresh=%ss)",
            self.app_id,
            self._heartbeat_interval,
            self._meta_refresh_interval,
        )

        while self._alive(generation):
            now = time.monotonic()
            wait = 1.0
            deadlines: list[float] = []

            if self._meta_resolver is not None:
                deadlines.append(self._last_meta_refresh_mono + self._meta_refresh_interval)
                if now >= self._last_meta_refresh_mono + self._meta_refresh_interval:
                    self._refresh_meta()

            heartbeat_interval = self._heartbeat_interval
            if heartbeat_interval:
                deadlines.append(self._last_heartbeat_mono + heartbeat_interval)
                if now >= self._last_heartbeat_mono + heartbeat_interval:
                    self._heartbeat_once(generation)

            if deadlines:
                wait = min(deadlines) - time.monotonic()
            self._stop_event.wait(
                max(0.05, min(wait, _MAINTENANCE_SLICE_SECONDS))
            )

        self._logger.info(
            "Apollo maintenance loop stopped for app=%s generation=%s",
            self.app_id,
            generation,
        )

    def _heartbeat_once(self, generation: int) -> None:
        errors: list[str] = []
        with self._lock:
            namespaces = list(self._namespaces)
            retry_at = dict(self._not_found_until)
        now = time.monotonic()
        for namespace in namespaces:
            if not self._alive(generation):
                break
            retry = retry_at.get(namespace)
            if retry is not None and now < retry:
                continue
            self._fetch_and_store(namespace, emit=True)
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
                "Apollo heartbeat for app=%s reported errors: %s",
                self.app_id,
                self._heartbeat_error,
            )
        else:
            self._logger.debug(
                "Apollo heartbeat #%s for app=%s completed",
                self._heartbeat_count,
                self.app_id,
            )

    def _refresh_meta(self) -> bool:
        resolver = self._meta_resolver
        if resolver is None:
            return False
        try:
            urls = resolver.refresh()
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
                self._logger.info(
                    "Apollo config servers updated from meta server: %s",
                    ", ".join(urls),
                )
            self._last_meta_refresh_mono = time.monotonic()
        return True

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ApolloClient(app_id={self.app_id!r}, cluster={self.cluster!r}, "
            f"namespaces={list(self._namespaces)!r}, mode={self._watch_mode!r}, "
            f"running={self.is_running})"
        )


def _normalise_watch_mode(value: str) -> str:
    mode = str(value).strip().lower()
    if mode not in WATCH_MODES:
        raise ValueError(
            f"unsupported watch_mode {value!r}; expected one of "
            f"{sorted(WATCH_MODES)}"
        )
    return mode


def _looks_like_properties_file(
    configurations: Mapping[str, Any], namespace: str
) -> bool:
    """Detect Apollo's "whole file under one key" properties shape."""

    if len(configurations) != 1:
        return False
    key, value = next(iter(configurations.items()))
    if not isinstance(value, str):
        return False
    candidates = {
        namespace,
        namespace.rsplit("/", 1)[-1],
        namespace.rsplit(".", 1)[0],
    }
    if str(key) not in candidates:
        return False
    return (
        "\n" in value
        or "\r" in value
        or "=" in value
        or ":" in value
    )


def _single_configuration_value(
    configurations: Mapping[str, Any],
) -> str | None:
    if not configurations:
        return ""
    if len(configurations) != 1:
        return None
    value = next(iter(configurations.values()))
    return value if isinstance(value, str) else None


def _stringify_property_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
