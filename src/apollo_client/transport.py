"""HTTP transport for the Apollo config service.

Only the endpoints required by the client are implemented:

* ``/configs/{app}/{cluster}/{namespace}`` - release + configurations,
* ``/configfiles/{app}/{cluster}/{namespace}`` - raw file content fallback,
* ``/notifications/v2`` - long polling notification API,
* ``/apps/{app}/clusters/{cluster}/namespaces`` - namespace discovery.

Multiple config server URLs are supported and tried in a rotating order, which
matches the way Apollo's own clients handle a config service cluster.  The URL
list can be replaced at runtime (Meta Server refresh) while requests are in
flight.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

import requests

from ._version import __version__
from .errors import (
    ApolloAuthError,
    ApolloConnectionError,
    ApolloHTTPError,
    ApolloParseError,
    NamespaceNotFoundError,
)

_UNSET = object()


class ApolloTransport:
    """Low level, thread-safe HTTP access to one Apollo app/cluster."""

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
        self._servers_lock = threading.Lock()
        self._session_local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()
        self._closed = False

    # -- properties -----------------------------------------------------------
    @property
    def base_urls(self) -> tuple[str, ...]:
        return self._urls

    @property
    def closed(self) -> bool:
        return self._closed

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
    def secret(self) -> str | None:
        return self._secret

    @property
    def ip(self) -> str | None:
        return self._ip

    # -- runtime updates ------------------------------------------------------
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
    ) -> ApolloTransport:
        """Update transport parameters in place (thread-safe).

        Only explicitly passed arguments are changed.  Replacing the URL list
        resets the preferred-server cursor so the new cluster is tried first.
        """

        with self._servers_lock:
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

    # -- public API -----------------------------------------------------------
    def reopen(self) -> None:
        """Allow requests again after :meth:`close`."""

        with self._sessions_lock:
            self._closed = False
            self._session_local = threading.local()

    def close(self) -> None:
        """Close every session created by this transport."""

        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
            self._session_local = threading.local()
            self._closed = True
        for session in sessions:
            try:
                session.close()
            except Exception:  # pragma: no cover - closing is best effort
                pass

    def get_config(
        self, namespace: str, *, release_key: str | None = None
    ) -> dict[str, Any]:
        """Fetch ``/configs`` for one namespace."""

        params: dict[str, Any] = {}
        if release_key:
            params["releaseKey"] = release_key
        if self._ip:
            params["ip"] = self._ip

        path = (
            f"/configs/{quote(self._app_id, safe='')}"
            f"/{quote(self._cluster, safe='')}"
            f"/{quote(namespace, safe='')}"
        )
        response = self._request(path, params)
        self._raise_for_config_status(response, namespace)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApolloParseError(
                f"Apollo returned non-JSON data for namespace {namespace!r}: "
                f"{_body_snippet(response)}"
            ) from exc
        if not isinstance(payload, dict):
            raise ApolloParseError(
                f"Apollo returned an unexpected payload for namespace "
                f"{namespace!r}: {type(payload).__name__}"
            )
        return payload

    def get_config_file(self, namespace: str, fmt: str) -> str:
        """Fetch raw namespace content from ``/configfiles``."""

        params: dict[str, Any] = {"format": fmt}
        if self._ip:
            params["ip"] = self._ip
        path = (
            f"/configfiles/{quote(self._app_id, safe='')}"
            f"/{quote(self._cluster, safe='')}"
            f"/{quote(namespace, safe='')}"
        )
        response = self._request(path, params)
        self._raise_for_config_status(response, namespace)
        return response.text

    def list_namespaces(self) -> tuple[str, ...]:
        """Return the namespaces published for this app/cluster.

        Uses Apollo's legacy discovery endpoint
        ``/apps/{app}/clusters/{cluster}/namespaces``.  Newer Apollo releases
        may not expose it; callers should treat :class:`ApolloHTTPError` as
        "discovery unavailable" and keep their explicit namespace list.
        """

        path = (
            f"/apps/{quote(self._app_id, safe='')}"
            f"/clusters/{quote(self._cluster, safe='')}/namespaces"
        )
        response = self._request(path)
        if response.status_code in (401, 403):
            raise ApolloAuthError(
                "Apollo rejected the namespace discovery request",
                status_code=response.status_code,
                url=response.url,
                body=_body_snippet(response),
            )
        if response.status_code != 200:
            raise ApolloHTTPError(
                "namespace discovery is not available on this Apollo release",
                status_code=response.status_code,
                url=response.url,
                body=_body_snippet(response),
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApolloParseError(
                "Apollo namespace discovery returned non-JSON data: "
                f"{_body_snippet(response)}"
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

    def notifications(
        self,
        notifications: Mapping[str, int],
        *,
        timeout: float | None = None,
    ) -> list[dict[str, Any]] | None:
        """Call ``/notifications/v2``.

        Returns a list of changed namespaces, or ``None`` for Apollo's
        ``304 Not Modified`` response.
        """

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
        # Apollo's config service holds the request for its own timeout (60s by
        # default) and ignores client-side wait hints.  Be generous with the
        # read timeout so a too-small ``long_poll_timeout`` does not abort every
        # poll just before the server can answer 304, while keeping connection
        # establishment bounded by the normal request timeout.
        read_timeout = max(wait_seconds + 10.0, 70.0)
        response = self._request(
            "/notifications/v2",
            params,
            timeout=(min(self._timeout, 10.0), read_timeout),
        )

        if response.status_code == 304:
            return None
        if response.status_code in (401, 403):
            raise ApolloAuthError(
                "Apollo rejected the request (check app id / secret)",
                status_code=response.status_code,
                url=response.url,
                body=_body_snippet(response),
            )
        if response.status_code != 200:
            raise ApolloHTTPError(
                "unexpected response from Apollo notifications endpoint",
                status_code=response.status_code,
                url=response.url,
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

    # -- internals ------------------------------------------------------------
    def _request(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | tuple[float, float] | None = None,
    ) -> requests.Response:
        if self._closed:
            raise ApolloConnectionError("Apollo transport is closed")

        clean_params = {
            key: value for key, value in (params or {}).items() if value is not None
        }
        failures: list[str] = []

        with self._servers_lock:
            urls = self._urls
            secret = self._secret
            app_id = self._app_id
            headers = self._headers
            preferred = self._preferred
        if not urls:  # pragma: no cover - guarded by update_options
            raise ApolloConnectionError("no Apollo config server is configured")

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
                response = self._session().get(
                    url,
                    headers=request_headers,
                    timeout=timeout if timeout is not None else self._timeout,
                )
            except requests.RequestException as exc:
                failures.append(f"{base_url}: {exc}")
                continue

            if response.status_code >= 500:
                failures.append(f"{base_url}: HTTP {response.status_code}")
                continue

            with self._servers_lock:
                self._preferred = index
            return response

        raise ApolloConnectionError(
            "all Apollo config servers failed: " + "; ".join(failures)
        )

    def _prepare(
        self, base_url: str, path: str, params: Mapping[str, Any]
    ) -> tuple[str, dict[str, str]]:
        return prepare_signed_request(
            base_url,
            path,
            params,
            app_id=self._app_id,
            secret=self._secret,
            headers=self._headers,
        )

    def _session(self) -> requests.Session:
        if self._closed:
            raise ApolloConnectionError("Apollo transport is closed")
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = f"apollo-client-python/{__version__}"
            self._session_local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    @staticmethod
    def _raise_for_config_status(
        response: requests.Response, namespace: str
    ) -> None:
        if response.status_code == 200:
            return
        if response.status_code == 404:
            raise NamespaceNotFoundError(
                f"namespace {namespace!r} not found in Apollo",
                status_code=response.status_code,
                url=response.url,
                body=_body_snippet(response),
            )
        if response.status_code in (401, 403):
            raise ApolloAuthError(
                f"Apollo rejected access to namespace {namespace!r}",
                status_code=response.status_code,
                url=response.url,
                body=_body_snippet(response),
            )
        raise ApolloHTTPError(
            f"unexpected response while fetching namespace {namespace!r}",
            status_code=response.status_code,
            url=response.url,
            body=_body_snippet(response),
        )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ApolloTransport(app_id={self._app_id!r}, cluster={self._cluster!r}, "
            f"servers={list(self._urls)!r})"
        )


def prepare_signed_request(
    base_url: str,
    path: str,
    params: Mapping[str, Any] | None = None,
    *,
    app_id: str,
    secret: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Build the final URL and headers for one Apollo request.

    Shared by the synchronous and asynchronous transports so both produce the
    same HMAC-SHA1 ``Authorization`` signature for Apollo's ``secret`` feature.
    """

    prepared = requests.Request(
        "GET", f"{base_url}{path}", params=dict(params or {})
    ).prepare()

    result_headers = dict(headers or {})
    if secret:
        timestamp = str(int(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{prepared.path_url}"
        digest = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")
        result_headers["Authorization"] = f"Apollo {app_id}:{signature}"
        result_headers["Timestamp"] = timestamp
    result_headers.setdefault("Accept", "application/json")
    return prepared.url, result_headers


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


def _body_snippet(response: requests.Response, limit: int = 300) -> str:
    try:
        text = response.text or ""
    except Exception:  # pragma: no cover - defensive
        return ""
    return text[:limit]
