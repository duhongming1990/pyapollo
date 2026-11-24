"""Parsers for the configuration formats supported by Apollo.

Apollo stores every namespace as text.  ``properties`` namespaces are returned
by the config service as a ready-made key/value map, while YAML/JSON namespaces
are returned as a single "file" value that has to be parsed by the client.  The
functions below normalise both shapes into a ``dict``.
"""

from __future__ import annotations

import json
import string
from collections.abc import Iterator, Mapping
from typing import Any

import yaml

from .errors import ApolloParseError

_PROPERTY_WHITESPACE = " \t\f"
_PROPERTY_SEPARATORS = frozenset("=: \t\f")
_PROPERTY_ESCAPES = {
    "t": "\t",
    "n": "\n",
    "r": "\r",
    "f": "\f",
}


def parse_properties(text: str) -> dict[str, str]:
    """Parse a Java ``.properties`` document into a string dictionary.

    The implementation follows the semantics of ``java.util.Properties``:

    * ``#`` and ``!`` start a comment line,
    * ``=``, ``:`` or whitespace separates key and value,
    * a trailing odd number of backslashes continues on the next line,
    * ``\\t``, ``\\n``, ``\\r``, ``\\f`` and ``\\uXXXX`` escapes are decoded,
    * when a key is repeated the last value wins.
    """

    if not text:
        return {}

    result: dict[str, str] = {}
    for logical_line in _logical_lines(text):
        if not logical_line.strip(_PROPERTY_WHITESPACE):
            continue
        key, value = _split_property(logical_line)
        result[key] = value
    return result


def parse_yaml(text: str) -> dict[str, Any]:
    """Parse a YAML document and require a mapping at the root."""

    if not text.strip():
        return {}
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ApolloParseError(f"invalid YAML config: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ApolloParseError(
            "YAML config must contain a mapping at the top level, "
            f"got {type(data).__name__}"
        )
    return _stringify_keys(data)


def parse_json(text: str) -> dict[str, Any]:
    """Parse a JSON object and require a mapping at the root."""

    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ApolloParseError(f"invalid JSON config: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ApolloParseError(
            "JSON config must contain an object at the top level, "
            f"got {type(data).__name__}"
        )
    return _stringify_keys(data)


def parse_content(text: str, fmt: str) -> dict[str, Any]:
    """Parse ``text`` according to ``fmt`` (properties/yaml/json)."""

    normalised = fmt.strip().lower()
    if normalised in {"properties", "props"}:
        return parse_properties(text)
    if normalised in {"yaml", "yml"}:
        return parse_yaml(text)
    if normalised == "json":
        return parse_json(text)
    raise ApolloParseError(f"unsupported Apollo config format: {fmt!r}")


def serialize_properties(data: Mapping[str, Any]) -> str:
    """Render a key/value map as a readable properties document.

    This is used for the ``raw`` value exposed by :class:`NamespaceConfig` and
    in the disk cache.  Values containing newlines are preserved in the parsed
    data, so the rendered form is informational rather than round-trip exact.
    """

    lines = []
    for key, value in data.items():
        rendered = "" if value is None else str(value)
        lines.append(f"{key}={rendered}")
    return "\n".join(lines)


def _logical_lines(text: str) -> Iterator[str]:
    """Yield logical properties lines after joining continuation lines."""

    buffer: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.lstrip(_PROPERTY_WHITESPACE)
        if not line:
            continue
        if buffer is None and line[0] in "#!":
            continue
        if _has_trailing_continuation(line):
            buffer = (buffer or "") + line[:-1]
            continue
        yield (buffer or "") + line
        buffer = None
    if buffer is not None:
        yield buffer


def _has_trailing_continuation(line: str) -> bool:
    backslashes = 0
    for char in reversed(line):
        if char != "\\":
            break
        backslashes += 1
    return backslashes % 2 == 1


def _split_property(line: str) -> tuple[str, str]:
    """Split one logical line into an unescaped key and value."""

    key_chars: list[str] = []
    index = 0
    length = len(line)

    while index < length:
        char = line[index]
        if char == "\\" and index + 1 < length:
            key_chars.append(char)
            key_chars.append(line[index + 1])
            index += 2
            continue
        if char in _PROPERTY_SEPARATORS:
            break
        key_chars.append(char)
        index += 1

    key = _unescape("".join(key_chars), context=line)
    while index < length and line[index] in _PROPERTY_WHITESPACE:
        index += 1
    if index < length and line[index] in "=:":
        index += 1
        while index < length and line[index] in _PROPERTY_WHITESPACE:
            index += 1

    value = _unescape(line[index:], context=line)
    return key, value


def _unescape(text: str, *, context: str) -> str:
    if "\\" not in text:
        return text

    result: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\":
            result.append(char)
            index += 1
            continue

        index += 1
        if index >= length:  # dangling backslash at EOF
            break
        escaped = text[index]
        index += 1

        if escaped == "u":
            code = _read_unicode_escape(text, index, context=context)
            result.append(chr(code))
            index += 4

            # Combine UTF-16 surrogate pairs produced by \uXXXX escapes.
            if 0xD800 <= code <= 0xDBFF and text[index : index + 2] == "\\u":
                low = _read_unicode_escape(text, index + 2, context=context)
                if 0xDC00 <= low <= 0xDFFF:
                    result[-1] = chr(
                        0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    )
                    index += 6
        elif escaped in _PROPERTY_ESCAPES:
            result.append(_PROPERTY_ESCAPES[escaped])
        else:
            # java.util.Properties drops the backslash for unknown escapes.
            result.append(escaped)

    return "".join(result)


def _read_unicode_escape(text: str, index: int, *, context: str) -> int:
    digits = text[index : index + 4]
    if len(digits) != 4 or any(char not in string.hexdigits for char in digits):
        raise ApolloParseError(
            f"invalid unicode escape in properties line: {context!r}"
        )
    return int(digits, 16)


def _stringify_keys(value: Any) -> Any:
    """Recursively convert mapping keys to strings.

    YAML permits non-string keys (numbers, booleans).  Apollo property lookups
    are string based, so normalising keys here keeps dotted access predictable.
    """

    if isinstance(value, Mapping):
        return {str(key): _stringify_keys(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_keys(item) for item in value]
    return value
