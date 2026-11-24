"""Best-effort disk cache for Apollo configuration snapshots.

The cache lets a service start (or keep serving) when the Apollo config
service is temporarily unreachable.  Cache failures never break the client:
they are logged and the cache is disabled for writes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .config import NamespaceConfig, detect_format
from .parser import parse_content
from .utils import jsonable

_CACHE_SCHEMA = 1
_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class DiskCache:
    """Store one JSON file per namespace below ``base/app/cluster``."""

    def __init__(
        self,
        directory: str | os.PathLike[str] | None,
        app_id: str,
        cluster: str,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self.enabled = True

        if directory is None:
            directory = os.environ.get("APOLLO_CACHE_DIR") or (
                Path.home() / ".cache" / "apollo"
            )
        base = Path(directory).expanduser()
        self.directory = base / _safe_part(app_id) / _safe_part(cluster)

    # -- public API -----------------------------------------------------------
    def load(self, namespace: str) -> NamespaceConfig | None:
        """Load a cached snapshot, returning ``None`` when unavailable."""

        if not self.enabled:
            return None

        path = self._path(namespace)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            self._logger.warning("failed to read Apollo cache %s: %s", path, exc)
            return None

        if not isinstance(payload, dict) or payload.get("schema") != _CACHE_SCHEMA:
            return None
        if str(payload.get("namespace")) != namespace:
            return None

        fmt = str(payload.get("format") or detect_format(namespace)).lower()
        data = payload.get("data")
        raw = payload.get("raw")
        if not isinstance(data, dict):
            if not isinstance(raw, str):
                return None
            try:
                data = parse_content(raw, fmt)
            except Exception as exc:
                self._logger.warning(
                    "failed to parse cached Apollo config for %s: %s", namespace, exc
                )
                return None

        try:
            return NamespaceConfig(
                namespace=namespace,
                format=fmt,
                data=data,
                raw=raw if isinstance(raw, str) else "",
                release_key=_optional_str(payload.get("releaseKey")),
                notification_id=_optional_int(payload.get("notificationId"), -1),
                source="cache",
                loaded_at=_optional_float(payload.get("cachedAt"), time.time()),
                app_id=_optional_str(payload.get("appId")),
                cluster=_optional_str(payload.get("cluster")),
            )
        except (TypeError, ValueError) as exc:
            self._logger.warning(
                "invalid cached Apollo config for %s: %s", namespace, exc
            )
            return None

    def save(self, config: NamespaceConfig) -> None:
        """Persist a snapshot atomically.  Errors disable further writes."""

        if not self.enabled:
            return

        path = self._path(config.namespace)
        payload = {
            "schema": _CACHE_SCHEMA,
            "appId": config.app_id,
            "cluster": config.cluster,
            "namespace": config.namespace,
            "format": config.format,
            "releaseKey": config.release_key,
            "notificationId": config.notification_id,
            "cachedAt": time.time(),
            "raw": config.raw,
            "data": jsonable(config.data),
        }
        temp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self._ensure_directory()
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        except OSError as exc:
            self.enabled = False
            self._logger.warning(
                "failed to write Apollo cache %s, disk cache disabled: %s",
                path,
                exc,
            )
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self, namespace: str | None = None) -> None:
        """Remove one cached file or the whole app/cluster cache."""

        if namespace is not None:
            try:
                self._path(namespace).unlink(missing_ok=True)
            except OSError as exc:
                self._logger.warning("failed to remove cached config: %s", exc)
            return

        try:
            for child in self.directory.glob("*.json"):
                child.unlink(missing_ok=True)
        except OSError as exc:
            self._logger.warning("failed to clear Apollo cache: %s", exc)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"DiskCache({str(self.directory)!r}, enabled={self.enabled})"

    # -- internals ------------------------------------------------------------
    def _path(self, namespace: str) -> Path:
        return self.directory / f"{_safe_part(namespace)}.json"

    def _ensure_directory(self) -> None:
        if self.directory.exists():
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass


def _safe_part(value: str) -> str:
    cleaned = _SAFE_PART_RE.sub("_", str(value)).strip("._")
    return cleaned or "_"


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
