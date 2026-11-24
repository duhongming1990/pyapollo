"""Integration tests for pull, hot update, cache and transport behaviour."""

from __future__ import annotations

import base64
import hashlib
import hmac
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from apollo_client import (
    ApolloClient,
    NamespaceNotFoundError,
)
from apollo_client.transport import ApolloTransport
from tests.helpers import publish_config, wait_until

PROPERTIES_MAX_200 = """\
app.name=Apollo Python Client
app.version=0.2.0
app.feature.new_ui=true
app.limits.max=200
logging.level=INFO
"""

PROPERTIES_NAME_UPDATED = """\
app.name=Filtered Update
app.version=0.2.0
app.feature.new_ui=true
app.limits.max=200
logging.level=INFO
"""

YAML_V2 = """\
server:
  host: 127.0.0.1
  port: 9090
  read_timeout: 30
feature:
  flags:
    - stable
  refresh_interval: 30
database:
  url: postgresql://localhost:5432/demo
  pool:
    min: 2
    max: 20
"""


def test_initial_pull_properties_and_yaml(apollo_client: ApolloClient) -> None:
    properties = apollo_client.get_config("application")
    assert properties is not None
    assert properties.format == "properties"
    assert properties.source == "remote"
    assert properties.release_key
    assert apollo_client.get_value("app.name") == "Apollo Python Client"
    assert apollo_client.get_str("app.name") == "Apollo Python Client"
    assert apollo_client.get_int("app.limits.max") == 100
    assert apollo_client.get_float("app.limits.max") == 100.0
    assert apollo_client.get_bool("app.feature.new_ui") is True

    yaml_config = apollo_client.get_config("demo.yaml")
    assert yaml_config is not None
    assert yaml_config.format == "yaml"
    assert apollo_client.get_value("server.port", namespace="demo.yaml") == 8080
    assert apollo_client.get_str("server.host", namespace="demo.yaml") == "0.0.0.0"
    assert apollo_client.get_value(
        "feature.flags", namespace="demo.yaml"
    ) == ["beta", "dark-mode"]
    assert apollo_client.get_value(
        "database.pool.max", namespace="demo.yaml"
    ) == 10
    assert apollo_client.get_json(
        "database.pool", namespace="demo.yaml"
    ) == {"min": 2, "max": 10}


def test_properties_hot_reload(
    apollo_client: ApolloClient, mock_apollo
) -> None:
    events = []
    unsubscribe = apollo_client.add_listener(events.append)

    publish_config(mock_apollo.base_url, "application", PROPERTIES_MAX_200)
    event = wait_until(
        lambda: events[0] if events else None,
        message="properties change event",
    )

    assert event.namespace == "application"
    assert event.changes["app.limits.max"].old == "100"
    assert event.changes["app.limits.max"].new == "200"
    assert apollo_client.get_int("app.limits.max") == 200
    assert event.old.release_key != event.new.release_key
    unsubscribe()


def test_yaml_hot_reload_with_nested_changes(
    apollo_client: ApolloClient, mock_apollo
) -> None:
    events = []
    apollo_client.add_listener(events.append, namespace="demo.yaml")

    publish_config(
        mock_apollo.base_url,
        "demo.yaml",
        YAML_V2,
        fmt="yaml",
    )
    wait_until(
        lambda: apollo_client.get_value("server.port", namespace="demo.yaml")
        == 9090,
        message="YAML server.port update",
    )
    event = wait_until(
        lambda: next(
            (item for item in events if item.namespace == "demo.yaml"), None
        ),
        message="YAML change event",
    )

    assert event.changes["server.port"].old == 8080
    assert event.changes["server.port"].new == 9090
    assert event.changes["feature.flags"].new == ["stable"]
    assert event.changes["database.pool.max"].new == 20
    assert apollo_client.get_value(
        "server.read_timeout", namespace="demo.yaml"
    ) == 30
    assert apollo_client.get_value(
        "feature.flags", namespace="demo.yaml"
    ) == ["stable"]


def test_listener_namespace_and_key_filters(
    apollo_client: ApolloClient, mock_apollo
) -> None:
    all_events = []
    filtered_events = []
    apollo_client.add_listener(all_events.append)
    apollo_client.add_listener(
        filtered_events.append,
        namespace="application",
        keys=["app.name"],
    )

    publish_config(mock_apollo.base_url, "application", PROPERTIES_MAX_200)
    wait_until(lambda: all_events, message="all-events listener")
    assert filtered_events == []

    publish_config(
        mock_apollo.base_url, "application", PROPERTIES_NAME_UPDATED
    )
    event = wait_until(
        lambda: filtered_events[0] if filtered_events else None,
        message="filtered listener",
    )
    assert event.namespace == "application"
    assert event.changes["app.name"].new == "Filtered Update"
    assert apollo_client.get_value("app.name") == "Filtered Update"


def test_manual_refresh_without_watcher(mock_apollo, tmp_path: Path) -> None:
    client = ApolloClient(
        app_id="demo-app",
        config_server_url=mock_apollo.base_url,
        namespaces=["application"],
        timeout=2.0,
        long_poll_timeout=1.0,
        cache_dir=tmp_path / "cache",
    )
    client.start(fetch=True, watch=False)
    try:
        publish_config(
            mock_apollo.base_url,
            "application",
            "app.name=Manual Refresh\napp.limits.max=42\n",
        )
        events = client.refresh("application")
        event = events["application"]
        assert event is not None
        assert event.changes["app.name"].new == "Manual Refresh"
        assert client.get_value("app.name") == "Manual Refresh"
        assert client.get_int("app.limits.max") == 42
    finally:
        client.stop()


def test_disk_cache_fallback_when_server_unreachable(
    mock_apollo, tmp_path: Path
) -> None:
    cache_dir = tmp_path / "apollo-cache"
    first = ApolloClient(
        app_id="demo-app",
        config_server_url=mock_apollo.base_url,
        namespaces=["application", "demo.yaml"],
        timeout=2.0,
        long_poll_timeout=1.0,
        cache_dir=cache_dir,
    )
    first.start(watch=False)
    assert first.get_value("app.name") == "Apollo Python Client"
    first.stop()

    cached_files = list(cache_dir.rglob("*.json"))
    assert len(cached_files) == 2

    offline = ApolloClient(
        app_id="demo-app",
        config_server_url="http://127.0.0.1:1",
        namespaces=["application", "demo.yaml"],
        timeout=0.3,
        long_poll_timeout=1.0,
        cache_dir=cache_dir,
    )
    # preload_cache=True means values are available before any network call.
    assert offline.get_value("app.name") == "Apollo Python Client"
    assert offline.get_value("server.port", namespace="demo.yaml") == 8080

    offline.start(fetch=True, watch=False)  # unreachable, must not raise
    cached = offline.get_config("application", auto_fetch=False)
    assert cached is not None
    assert cached.source == "cache"
    assert offline.get_value("app.name") == "Apollo Python Client"
    offline.stop()


def test_missing_namespace_uses_defaults(apollo_client: ApolloClient) -> None:
    assert (
        apollo_client.get_value(
            "anything", "fallback", namespace="missing.yaml"
        )
        == "fallback"
    )
    assert (
        apollo_client.get_config("missing.yaml", auto_fetch=False) is None
    )
    with pytest.raises(NamespaceNotFoundError):
        apollo_client.refresh("missing.yaml")


def test_multiple_config_servers_fail_over(mock_apollo) -> None:
    client = ApolloClient(
        app_id="demo-app",
        config_server_url=[
            "http://127.0.0.1:1",
            mock_apollo.base_url,
        ],
        namespaces=["application"],
        timeout=0.3,
        long_poll_timeout=1.0,
        cache_enabled=False,
    )
    client.start(fetch=True, watch=False)
    try:
        assert client.get_value("app.name") == "Apollo Python Client"
        assert len(client.config_server_urls) == 2
    finally:
        client.stop()


def test_dynamic_namespace_is_registered_and_watched(
    apollo_client: ApolloClient, mock_apollo
) -> None:
    publish_config(
        mock_apollo.base_url,
        "extra.yaml",
        "answer: 42\nnested:\n  enabled: true\n",
        fmt="yaml",
    )

    assert apollo_client.get_int("answer", namespace="extra.yaml") == 42
    assert "extra.yaml" in apollo_client.namespaces

    events = []
    apollo_client.add_listener(events.append, namespace="extra.yaml")
    publish_config(
        mock_apollo.base_url,
        "extra.yaml",
        "answer: 43\nnested:\n  enabled: false\n",
        fmt="yaml",
    )
    event = wait_until(
        lambda: events[0] if events else None,
        message="dynamic namespace change",
    )
    assert event.changes["answer"].new == 43
    assert apollo_client.get_int("answer", namespace="extra.yaml") == 43


def test_secret_signature_headers() -> None:
    transport = ApolloTransport(
        "http://example.com:8080",
        "demo-app",
        "default",
        secret="top-secret",
        timeout=1.0,
    )
    try:
        url, headers = transport._prepare(
            "http://example.com:8080",
            "/configs/demo-app/default/application",
            {"ip": "10.0.0.1"},
        )
    finally:
        transport.close()

    parts = urlsplit(url)
    path_with_query = parts.path
    if parts.query:
        path_with_query += f"?{parts.query}"
    timestamp = headers["Timestamp"]
    expected = base64.b64encode(
        hmac.new(
            b"top-secret",
            f"{timestamp}\n{path_with_query}".encode(),
            hashlib.sha1,
        ).digest()
    ).decode()
    assert headers["Authorization"] == f"Apollo demo-app:{expected}"


def test_no_secret_means_no_authorization_header() -> None:
    transport = ApolloTransport(
        "http://example.com:8080", "demo-app", "default", timeout=1.0
    )
    try:
        _, headers = transport._prepare(
            "http://example.com:8080",
            "/configs/demo-app/default/application",
            {},
        )
    finally:
        transport.close()
    assert "Authorization" not in headers


def test_long_poll_read_timeout_is_generous(monkeypatch) -> None:
    """Apollo holds long polls for 60s; avoid aborting before its 304."""

    class _NoChangeResponse:
        status_code = 304

    transport = ApolloTransport(
        "http://example.com:8080",
        "demo-app",
        "default",
        timeout=2.0,
        long_poll_timeout=10.0,
    )
    captured = {}

    def fake_request(path, params=None, *, timeout=None):
        captured["path"] = path
        captured["timeout"] = timeout
        return _NoChangeResponse()

    monkeypatch.setattr(transport, "_request", fake_request)
    try:
        assert transport.notifications({"application": -1}) is None
    finally:
        transport.close()

    assert captured["path"] == "/notifications/v2"
    connect_timeout, read_timeout = captured["timeout"]
    assert connect_timeout == 2.0
    assert read_timeout >= 60.0
