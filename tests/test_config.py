"""Tests for immutable snapshots, flattening and change diffs."""

from __future__ import annotations

import json

import pytest

from apollo_client import (
    ChangeEvent,
    ConfigChange,
    NamespaceConfig,
    diff_configs,
    diff_mappings,
    flatten_mapping,
)


def make_config(data: dict) -> NamespaceConfig:
    return NamespaceConfig(
        namespace="demo.yaml",
        format="yaml",
        data=data,
        raw="raw-content",
        release_key="release-1",
        notification_id=7,
        app_id="demo-app",
        cluster="default",
    )


def test_namespace_config_lookup_prefers_exact_key() -> None:
    config = make_config(
        {
            "server": {"port": 8080, "hosts": ["a", "b"]},
            "a.b": "literal-key",
            "flag": True,
            "ratio": "1.5",
            "json": '{"x": 1}',
        }
    )

    assert config.get("server.port") == 8080
    assert config.get("server.hosts.1") == "b"
    assert config.get("a.b") == "literal-key"
    assert config.get("missing", "fallback") == "fallback"
    assert config["server"]["port"] == 8080
    assert "server.port" in config
    assert "missing" not in config
    assert len(config) == 5


def test_typed_accessors() -> None:
    config = make_config(
        {
            "int": "42",
            "float": "3.5",
            "true": "true",
            "false": "0",
            "yes": "yes",
            "bad-int": "not-a-number",
            "json": '{"a": [1, 2]}',
            "native": {"b": 2},
            "null": None,
        }
    )

    assert config.get_int("int") == 42
    assert config.get_float("float") == 3.5
    assert config.get_bool("true") is True
    assert config.get_bool("false") is False
    assert config.get_bool("yes") is True
    assert config.get_int("bad-int", 7) == 7
    assert config.get_json("json") == {"a": [1, 2]}
    assert config.get_json("native") == {"b": 2}
    assert config.get_str("null", "fallback") == "fallback"
    assert config.get_int("missing", 9) == 9


def test_flatten_mapping() -> None:
    data = {
        "server": {"host": "localhost", "port": 80},
        "empty": {},
        "flags": ["a", "b"],
    }
    assert flatten_mapping(data) == {
        "server.host": "localhost",
        "server.port": 80,
        "empty": {},
        "flags": ["a", "b"],
    }


def test_diff_mappings_reports_all_change_kinds() -> None:
    old = {"a": 1, "b": {"c": 2}, "remove": True}
    new = {"a": 1, "b": {"c": 3}, "add": "new"}

    changes = diff_mappings(old, new)

    assert list(changes) == ["add", "b.c", "remove"]
    assert changes["add"] == ConfigChange("add", "added", None, "new")
    assert changes["b.c"] == ConfigChange("b.c", "updated", 2, 3)
    assert changes["remove"] == ConfigChange("remove", "removed", True, None)


def test_diff_configs_with_missing_old_snapshot() -> None:
    new = make_config({"a": 1})
    assert diff_configs(None, new) == {
        "a": ConfigChange("a", "added", None, 1)
    }


def test_change_event_serialisation() -> None:
    old = make_config({"server": {"port": 8080}, "debug": True})
    new = make_config({"server": {"port": 9090}})
    event = ChangeEvent(
        sequence=3,
        namespace="demo.yaml",
        changes=diff_configs(old, new),
        old=old,
        new=new,
        at=123.5,
    )

    assert event.changed_keys == ("debug", "server.port")
    assert event.removed_keys == ("debug",)
    assert event.updated_keys == ("server.port",)

    payload = event.to_dict()
    assert payload["sequence"] == 3
    assert payload["namespace"] == "demo.yaml"
    assert payload["releaseKey"] == "release-1"
    assert payload["previousReleaseKey"] == "release-1"
    assert payload["changes"]["server.port"] == {
        "key": "server.port",
        "kind": "updated",
        "old": 8080,
        "new": 9090,
    }
    json.dumps(payload)  # must be JSON serialisable for SSE / FastAPI


def test_namespace_config_to_dict_and_copy() -> None:
    config = make_config({"nested": {"value": 1}})
    payload = config.to_dict(include_raw=True)
    assert payload["namespace"] == "demo.yaml"
    assert payload["format"] == "yaml"
    assert payload["source"] == "remote"
    assert payload["raw"] == "raw-content"
    assert payload["data"] == {"nested": {"value": 1}}

    copied = config.as_dict()
    copied["nested"]["value"] = 2
    assert config.data["nested"]["value"] == 1


def test_namespace_config_normalises_format_and_rejects_non_mapping() -> None:
    config = NamespaceConfig(namespace="x", format="YML", data={})
    assert config.format == "yaml"

    with pytest.raises(TypeError):
        NamespaceConfig(namespace="x", format="yaml", data=[])  # type: ignore[arg-type]
