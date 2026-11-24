"""Small shared helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, time
from typing import Any

_TRUE_VALUES = {"1", "true", "yes", "on", "y", "t"}
_FALSE_VALUES = {"0", "false", "no", "off", "n", "f"}


def jsonable(value: Any) -> Any:
    """Return a JSON-serialisable copy of ``value``.

    Apollo configs can contain YAML timestamps, sets, bytes and other objects
    that the standard library JSON encoder does not understand.  Keeping the
    conversion in one place makes ``/config`` and SSE responses safe.
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def coerce_bool(value: Any) -> bool | None:
    """Best-effort conversion to bool, returning ``None`` when ambiguous."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
    return None


def parse_bool_env(value: str, *, default: bool = False) -> bool:
    """Parse a boolean environment variable value."""

    parsed = coerce_bool(value)
    return default if parsed is None else parsed


def dumps_compact(value: Any) -> str:
    """JSON dump used for query parameters / SSE payloads."""

    return json.dumps(jsonable(value), ensure_ascii=False, separators=(",", ":"))
