"""Immutable configuration snapshots and diffing helpers."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .parser import parse_content
from .utils import coerce_bool, jsonable

SUPPORTED_FORMATS = frozenset({"properties", "yaml", "json"})

_FORMAT_BY_SUFFIX = {
    ".yaml": "yaml",
    ".yml": "yaml",
    ".properties": "properties",
    ".json": "json",
}

_FORMAT_ALIASES = {
    "yaml": "yaml",
    "yml": "yaml",
    "properties": "properties",
    "property": "properties",
    "props": "properties",
    "json": "json",
}

_MISSING = object()


def normalize_format(value: str) -> str:
    """Normalise a user supplied format name."""

    key = value.strip().lower()
    try:
        return _FORMAT_ALIASES[key]
    except KeyError as exc:
        raise ValueError(
            f"unsupported config format {value!r}; "
            f"expected one of {sorted(SUPPORTED_FORMATS)}"
        ) from exc


def detect_format(namespace: str, explicit: str | None = None) -> str:
    """Infer a namespace format from its name (``*.yaml`` etc.).

    Apollo's default ``application`` namespace is a properties namespace, so
    namespaces without a known suffix fall back to ``properties``.
    """

    if explicit is not None:
        return normalize_format(explicit)

    name = namespace.rsplit("/", 1)[-1].lower()
    for suffix, fmt in _FORMAT_BY_SUFFIX.items():
        if name.endswith(suffix):
            return fmt
    return "properties"


def flatten_mapping(
    data: Mapping[str, Any], prefix: str = ""
) -> dict[str, Any]:
    """Flatten nested mappings into dotted keys.

    Lists are treated as leaf values.  Empty mappings are kept as a leaf so a
    change such as ``feature: {}`` -> ``feature: {enabled: true}`` is visible
    in change events.
    """

    flattened: dict[str, Any] = {}
    for raw_key, value in data.items():
        key = str(raw_key)
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            nested = flatten_mapping(value, full_key)
            if nested:
                flattened.update(nested)
            else:
                flattened[full_key] = {}
        else:
            flattened[full_key] = value
    return flattened


@dataclass(frozen=True, slots=True)
class NamespaceConfig:
    """An immutable snapshot of one Apollo namespace."""

    namespace: str
    format: str
    data: Mapping[str, Any]
    raw: str = ""
    release_key: str | None = None
    notification_id: int = -1
    source: str = "remote"
    loaded_at: float = field(default_factory=time.time)
    app_id: str | None = None
    cluster: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", normalize_format(self.format))
        if not isinstance(self.data, Mapping):
            raise TypeError("NamespaceConfig.data must be a mapping")

    # -- mapping style access -------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """Look up a key.

        Exact matches win (important for properties keys that contain dots),
        then dotted paths are resolved through nested mappings and lists, e.g.
        ``server.port`` or ``hosts.0.name``.
        """

        if not isinstance(key, str):
            return self.data.get(key, default)  # type: ignore[arg-type]

        if key in self.data:
            return self.data[key]

        current: Any = self.data
        for part in key.split("."):
            if isinstance(current, Mapping):
                if part not in current:
                    return default
                current = current[part]
                continue
            if isinstance(current, (list, tuple)) and part.lstrip("-").isdigit():
                try:
                    current = current[int(part)]
                except IndexError:
                    return default
                continue
            return default
        return current

    def __getitem__(self, key: str) -> Any:
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key, _MISSING) is not _MISSING

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def keys(self):
        return self.data.keys()

    def items(self):
        return self.data.items()

    def values(self):
        return self.data.values()

    def as_dict(self) -> dict[str, Any]:
        """Return a deep copy of the parsed values."""

        return copy.deepcopy(dict(self.data))

    def flattened(self) -> dict[str, Any]:
        """Return the dotted-key view used for change detection."""

        return flatten_mapping(self.data)

    # -- typed accessors ------------------------------------------------------
    def get_str(self, key: str, default: str | None = None) -> str | None:
        value = self.get(key, _MISSING)
        if value is _MISSING or value is None:
            return default
        return value if isinstance(value, str) else str(value)

    def get_int(self, key: str, default: int | None = None) -> int | None:
        value = self.get(key, _MISSING)
        if value is _MISSING or value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float | None = None) -> float | None:
        value = self.get(key, _MISSING)
        if value is _MISSING or value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool | None = None) -> bool | None:
        value = self.get(key, _MISSING)
        if value is _MISSING or value is None:
            return default
        parsed = coerce_bool(value)
        return default if parsed is None else parsed

    def get_json(self, key: str, default: Any = None) -> Any:
        value = self.get(key, _MISSING)
        if value is _MISSING or value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return default
        return value

    # -- serialisation --------------------------------------------------------
    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "namespace": self.namespace,
            "format": self.format,
            "releaseKey": self.release_key,
            "notificationId": self.notification_id,
            "source": self.source,
            "loadedAt": self.loaded_at,
            "data": jsonable(self.data),
        }
        if self.app_id is not None:
            payload["appId"] = self.app_id
        if self.cluster is not None:
            payload["cluster"] = self.cluster
        if include_raw:
            payload["raw"] = self.raw
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"NamespaceConfig(namespace={self.namespace!r}, format={self.format!r}, "
            f"release_key={self.release_key!r}, keys={len(self.data)})"
        )


@dataclass(frozen=True, slots=True)
class ConfigChange:
    """A single key level change between two snapshots."""

    key: str
    kind: str
    old: Any = None
    new: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "old": jsonable(self.old),
            "new": jsonable(self.new),
        }


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """Emitted after a namespace changed and listeners were invoked."""

    sequence: int
    namespace: str
    changes: Mapping[str, ConfigChange]
    old: NamespaceConfig | None
    new: NamespaceConfig
    at: float = field(default_factory=time.time)

    @property
    def changed_keys(self) -> tuple[str, ...]:
        return tuple(self.changes)

    @property
    def added_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key, change in self.changes.items() if change.kind == "added"
        )

    @property
    def updated_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key, change in self.changes.items() if change.kind == "updated"
        )

    @property
    def removed_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key, change in self.changes.items() if change.kind == "removed"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "namespace": self.namespace,
            "at": self.at,
            "releaseKey": self.new.release_key,
            "previousReleaseKey": self.old.release_key if self.old else None,
            "changedKeys": list(self.changed_keys),
            "changes": {
                key: change.to_dict() for key, change in self.changes.items()
            },
        }


def diff_mappings(
    old: Mapping[str, Any], new: Mapping[str, Any]
) -> dict[str, ConfigChange]:
    """Return added/updated/removed dotted keys between two mappings."""

    old_flat = flatten_mapping(old)
    new_flat = flatten_mapping(new)
    changes: dict[str, ConfigChange] = {}

    for key in old_flat.keys() - new_flat.keys():
        changes[key] = ConfigChange(key=key, kind="removed", old=old_flat[key])
    for key in new_flat.keys() - old_flat.keys():
        changes[key] = ConfigChange(key=key, kind="added", new=new_flat[key])
    for key in old_flat.keys() & new_flat.keys():
        old_value = old_flat[key]
        new_value = new_flat[key]
        if old_value != new_value:
            changes[key] = ConfigChange(
                key=key, kind="updated", old=old_value, new=new_value
            )

    return {key: changes[key] for key in sorted(changes)}


def diff_configs(
    old: NamespaceConfig | None, new: NamespaceConfig
) -> dict[str, ConfigChange]:
    """Diff two snapshots, treating a missing old snapshot as empty."""

    return diff_mappings(old.data if old is not None else {}, new.data)


def parse_namespace_content(
    namespace: str, fmt: str, text: str
) -> dict[str, Any]:
    """Parse ``text`` for a namespace, keeping error messages contextual."""

    try:
        return parse_content(text, fmt)
    except Exception as exc:  # pragma: no cover - re-raised with context
        from .errors import ApolloParseError

        if isinstance(exc, ApolloParseError):
            raise ApolloParseError(
                f"failed to parse namespace {namespace!r} as {fmt}: {exc}"
            ) from exc
        raise
