"""Tests for the properties / YAML / JSON parsers."""

from __future__ import annotations

import pytest

from apollo_client import (
    ApolloParseError,
    parse_json,
    parse_properties,
    parse_yaml,
)
from apollo_client.config import detect_format, normalize_format


def test_properties_supports_all_separators() -> None:
    text = (
        "equals=value\n"
        "colon:value\n"
        "space value\n"
        "spaced = value\n"
        "spaced-colon : value\n"
        "key.with.dots=1\n"
        "empty=\n"
        "bare\n"
    )
    assert parse_properties(text) == {
        "equals": "value",
        "colon": "value",
        "space": "value",
        "spaced": "value",
        "spaced-colon": "value",
        "key.with.dots": "1",
        "empty": "",
        "bare": "",
    }


def test_properties_comments_and_blank_lines() -> None:
    text = """
# leading comment
! another comment

key=value
   # indented comment
other=1
"""
    assert parse_properties(text) == {"key": "value", "other": "1"}


def test_properties_line_continuation() -> None:
    text = (
        "message = hello \\\n"
        "    world \\\n"
        "    again\n"
        "next=1\n"
    )
    parsed = parse_properties(text)
    assert parsed["message"] == "hello world again"
    assert parsed["next"] == "1"


def test_properties_escapes_and_unicode() -> None:
    text = (
        r"newline=line1\nline2"
        "\n"
        r"tab=a\tb"
        "\n"
        r"escaped-colon=a\:b"
        "\n"
        r"escaped-space=a\ b"
        "\n"
        r"backslash=a\\b"
        "\n"
        r"unicode=\u4f60\u597d"
        "\n"
        r"emoji=\uD83D\uDE00"
        "\n"
    )
    parsed = parse_properties(text)
    assert parsed["newline"] == "line1\nline2"
    assert parsed["tab"] == "a\tb"
    assert parsed["escaped-colon"] == "a:b"
    assert parsed["escaped-space"] == "a b"
    assert parsed["backslash"] == r"a\b"
    assert parsed["unicode"] == "你好"
    assert parsed["emoji"] == "😀"


def test_properties_last_value_wins() -> None:
    assert parse_properties("key=first\nkey=second\n") == {"key": "second"}


def test_properties_equals_inside_value() -> None:
    assert parse_properties("url=jdbc:mysql://db:3306/x?a=b") == {
        "url": "jdbc:mysql://db:3306/x?a=b"
    }


def test_properties_empty_input() -> None:
    assert parse_properties("") == {}
    assert parse_properties("\n\n# only comments\n") == {}


def test_properties_invalid_unicode_escape() -> None:
    with pytest.raises(ApolloParseError):
        parse_properties(r"key=\u12xy")


def test_yaml_nested_types() -> None:
    text = """
server:
  host: 0.0.0.0
  port: 8080
  enabled: true
feature:
  flags:
    - beta
    - dark-mode
  ratio: 1.5
  nothing: null
database:
  pool:
    min: 2
"""
    parsed = parse_yaml(text)
    assert parsed["server"]["host"] == "0.0.0.0"
    assert parsed["server"]["port"] == 8080
    assert parsed["server"]["enabled"] is True
    assert parsed["feature"]["flags"] == ["beta", "dark-mode"]
    assert parsed["feature"]["ratio"] == 1.5
    assert parsed["feature"]["nothing"] is None
    assert parsed["database"]["pool"]["min"] == 2


def test_yaml_empty_document() -> None:
    assert parse_yaml("") == {}
    assert parse_yaml("   \n# comment only\n") == {}


def test_yaml_numeric_keys_are_stringified() -> None:
    assert parse_yaml("1: one\n2: two\n") == {"1": "one", "2": "two"}


def test_yaml_root_must_be_mapping() -> None:
    with pytest.raises(ApolloParseError):
        parse_yaml("- one\n- two\n")


def test_yaml_invalid_syntax() -> None:
    with pytest.raises(ApolloParseError):
        parse_yaml("server: [unclosed\n")


def test_json_object() -> None:
    assert parse_json('{"a": 1, "b": {"c": true}}') == {
        "a": 1,
        "b": {"c": True},
    }
    assert parse_json("") == {}


def test_json_root_must_be_object() -> None:
    with pytest.raises(ApolloParseError):
        parse_json("[1, 2, 3]")


def test_json_invalid() -> None:
    with pytest.raises(ApolloParseError):
        parse_json("{not json}")


def test_detect_format() -> None:
    assert detect_format("application") == "properties"
    assert detect_format("application.properties") == "properties"
    assert detect_format("demo.yaml") == "yaml"
    assert detect_format("demo.YML") == "yaml"
    assert detect_format("feature.json") == "json"
    assert detect_format("demo.yaml", "json") == "json"


def test_normalize_format() -> None:
    assert normalize_format("YML") == "yaml"
    assert normalize_format("props") == "properties"
    with pytest.raises(ValueError):
        normalize_format("toml")
