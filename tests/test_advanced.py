"""Tests for discovery, polling, Meta Server, heartbeat and runtime options."""

from __future__ import annotations

import pytest

from apollo_client import (
    ApolloClient,
    ApolloConnectionError,
    ApolloHTTPError,
    ApolloParseError,
    MetaServerResolver,
    parse_meta_response,
)
from apollo_client.env import options_from_env
from tests.helpers import publish_config, wait_until

PROPERTIES_HEARTBEAT = """\
app.name=Heartbeat Update
app.version=0.2.0
app.feature.new_ui=true
app.limits.max=321
logging.level=INFO
"""


def make_client(mock_apollo, tmp_path, **overrides) -> ApolloClient:
    options = {
        "app_id": "demo-app",
        "config_server_url": mock_apollo.base_url,
        "namespaces": ["application"],
        "timeout": 2.0,
        "long_poll_timeout": 1.0,
        "cache_dir": tmp_path / "apollo-cache",
        "heartbeat_interval": None,
    }
    options.update(overrides)
    return ApolloClient(**options)


# -- Meta Server parsing ------------------------------------------------------
def test_parse_meta_response_accepts_registry_shapes() -> None:
    assert parse_meta_response(["10.0.0.1:8080", "https://cfg:8443/"]) == (
        "http://10.0.0.1:8080",
        "https://cfg:8443",
    )
    assert parse_meta_response(
        {"configServers": [{"homepageUrl": "http://cfg-a:8080"}]}
    ) == ("http://cfg-a:8080",)
    assert parse_meta_response([{"ip": "10.0.0.2", "port": 9090}]) == (
        "http://10.0.0.2:9090",
    )
    assert parse_meta_response(["cfg-a:8080", "cfg-a:8080"]) == (
        "http://cfg-a:8080",
    )


def test_parse_meta_response_rejects_invalid_payload() -> None:
    with pytest.raises(ApolloParseError):
        parse_meta_response("not-a-list")


def test_meta_resolver_reports_unreachable_meta_server() -> None:
    resolver = MetaServerResolver(
        "http://127.0.0.1:1", "demo-app", timeout=0.2
    )
    with pytest.raises(ApolloConnectionError):
        resolver.resolve()
    assert resolver.last_error


# -- Namespace discovery ------------------------------------------------------
def test_namespace_discovery_registers_and_pulls(
    mock_apollo, tmp_path
) -> None:
    publish_config(
        mock_apollo.base_url, "extra.yaml", "answer: 42\n", fmt="yaml"
    )
    client = make_client(mock_apollo, tmp_path, cache_enabled=False)
    client.start(watch=False)
    try:
        names = client.discover_namespaces()
        assert {"application", "demo.yaml", "extra.yaml"}.issubset(names)
        assert "extra.yaml" in client.namespaces
        assert client.get_int("answer", namespace="extra.yaml") == 42
    finally:
        client.stop()


def test_auto_discover_at_start(mock_apollo, tmp_path) -> None:
    client = make_client(mock_apollo, tmp_path, auto_discover=True)
    client.start(watch=False)
    try:
        assert "demo.yaml" in client.namespaces
        assert client.get_int("server.port", namespace="demo.yaml") == 8080
    finally:
        client.stop()


def test_discovery_failure_does_not_block_start(
    mock_apollo, tmp_path, monkeypatch
) -> None:
    client = make_client(mock_apollo, tmp_path, auto_discover=True)

    def unavailable():
        raise ApolloHTTPError("discovery unavailable", status_code=404)

    monkeypatch.setattr(client._transport, "list_namespaces", unavailable)
    client.start(watch=False)
    try:
        assert client.get_value("app.name") == "Apollo Python Client"
        assert "application" in client.namespaces
        with pytest.raises(ApolloHTTPError):
            client.discover_namespaces()
    finally:
        client.stop()


# -- Scheduled polling --------------------------------------------------------
def test_scheduled_polling_hot_update(mock_apollo, tmp_path) -> None:
    client = make_client(
        mock_apollo, tmp_path, watch_mode="poll", poll_interval=0.1
    )
    events = []
    client.add_listener(events.append, namespace="application")
    client.start()
    try:
        publish_config(
            mock_apollo.base_url,
            "application",
            "app.name=Polled Update\napp.limits.max=777\n",
        )
        event = wait_until(
            lambda: events[0] if events else None,
            message="poll mode change event",
        )
        assert event.changes["app.name"].new == "Polled Update"
        assert client.get_int("app.limits.max") == 777
    finally:
        client.stop()


def test_watch_mode_defaults_to_long_poll(mock_apollo, tmp_path) -> None:
    client = make_client(mock_apollo, tmp_path)
    assert client.watch_mode == "long_poll"
    assert client.poll_interval > 0
    client.stop()


# -- Meta Server discovery ----------------------------------------------------
def test_meta_server_discovery_replaces_bootstrap_url(
    mock_apollo, tmp_path
) -> None:
    client = make_client(
        mock_apollo,
        tmp_path,
        config_server_url="http://127.0.0.1:1",
        meta_server_url=mock_apollo.base_url,
    )
    client.start(watch=False)
    try:
        assert mock_apollo.base_url in client.config_server_urls
        assert client.get_value("app.name") == "Apollo Python Client"
        status = client.status()
        assert status["metaServers"] == [mock_apollo.base_url]
        assert status["configServers"] == [mock_apollo.base_url]
    finally:
        client.stop()


# -- Heartbeat / reconciliation ----------------------------------------------
def test_heartbeat_reconciles_missed_changes(mock_apollo, tmp_path) -> None:
    # A very long poll interval means only the heartbeat can pick up the change.
    client = make_client(
        mock_apollo,
        tmp_path,
        watch_mode="poll",
        poll_interval=30.0,
        heartbeat_interval=0.2,
    )
    events = []
    client.add_listener(events.append, namespace="application")
    client.start()
    try:
        publish_config(
            mock_apollo.base_url, "application", PROPERTIES_HEARTBEAT
        )
        event = wait_until(
            lambda: events[0] if events else None,
            message="heartbeat reconciliation event",
        )
        assert event.changes["app.name"].new == "Heartbeat Update"

        status = wait_until(
            _heartbeat_status(client),
            message="heartbeat status",
        )
        assert status["lastHeartbeatAt"]
        assert status["heartbeatError"] is None
    finally:
        client.stop()


# -- Runtime option updates ---------------------------------------------------
def test_update_options_applies_at_runtime(mock_apollo, tmp_path) -> None:
    client = make_client(
        mock_apollo, tmp_path, watch_mode="poll", poll_interval=0.1
    )
    client.start()
    try:
        publish_config(
            mock_apollo.base_url, "extra.yaml", "answer: 42\n", fmt="yaml"
        )
        client.update_options(
            namespaces=["extra.yaml"],
            ip="10.1.2.3",
            timeout=3.0,
            heartbeat_interval=0.2,
        )
        assert client.get_int("answer", namespace="extra.yaml") == 42
        assert client.heartbeat_interval == 0.2
        assert client._transport.ip == "10.1.2.3"
        assert client._transport.timeout == 3.0

        # Switching mode must take effect without restarting the client.
        events = []
        client.add_listener(events.append, namespace="application")
        client.update_options(watch_mode="long_poll", poll_interval=0.05)
        publish_config(
            mock_apollo.base_url,
            "application",
            "app.name=Switched Mode\napp.limits.max=9\n",
        )
        event = wait_until(
            lambda: events[0] if events else None,
            message="long poll after mode switch",
        )
        assert event.changes["app.name"].new == "Switched Mode"
    finally:
        client.stop()


def test_update_options_switches_identity_and_cache(
    mock_apollo, tmp_path
) -> None:
    client = make_client(mock_apollo, tmp_path)
    client.start(watch=False)
    try:
        assert client.get_value("app.name") == "Apollo Python Client"
        old_cache_dir = client.cache_directory
        assert old_cache_dir is not None

        client.update_options(app_id="other-app")
        assert client.app_id == "other-app"
        assert client.get_config("application", auto_fetch=False) is None
        assert client.cache_directory != old_cache_dir
        assert "other-app" in str(client.cache_directory)

        client.update_options(app_id="demo-app")
        cached = client.get_config("application", auto_fetch=False)
        assert cached is not None
        assert cached.source == "cache"
        assert cached.data["app.name"] == "Apollo Python Client"
    finally:
        client.stop()


def test_update_options_validates_input(mock_apollo, tmp_path) -> None:
    client = make_client(mock_apollo, tmp_path)
    try:
        with pytest.raises(TypeError):
            client.update_options(unknown_option=1)
        with pytest.raises(ValueError):
            client.update_options(watch_mode="bogus")
        with pytest.raises(ValueError):
            client.update_options(poll_interval=0)
        with pytest.raises(ValueError):
            client.update_options(heartbeat_interval=-1)
        with pytest.raises(ValueError):
            client.update_options(app_id="")
        with pytest.raises(ValueError):
            client.update_options(timeout=0)
    finally:
        client.stop()


def test_update_options_server_urls_and_error_path(
    mock_apollo, tmp_path
) -> None:
    client = make_client(mock_apollo, tmp_path)
    client.start(watch=False)
    try:
        client.update_options(config_server_url="http://127.0.0.1:1")
        assert client.config_server_urls == ("http://127.0.0.1:1",)
        with pytest.raises(ApolloConnectionError):
            client.refresh("application")

        client.update_options(config_server_url=mock_apollo.base_url)
        events = client.refresh("application")
        assert "application" in events
        assert client.get_value("app.name") == "Apollo Python Client"
    finally:
        client.stop()


# -- Environment --------------------------------------------------------------
def test_options_from_env_merges_environment(monkeypatch) -> None:
    env = {
        "APOLLO_APP_ID": "env-app",
        "APOLLO_CONFIG_SERVER": "http://env:8080",
        "APOLLO_NAMESPACES": "application, demo.yaml",
        "APOLLO_CLUSTER": "prod",
        "APOLLO_TIMEOUT": "3.5",
        "APOLLO_HEARTBEAT_INTERVAL": "0",
        "APOLLO_AUTO_DISCOVER": "true",
        "APOLLO_WATCH_MODE": "poll",
        "APOLLO_META_SERVER": "http://meta:8080",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    options = options_from_env({"app_id": "fallback"})

    assert options["app_id"] == "env-app"
    assert options["cluster"] == "prod"
    assert options["config_server_url"] == "http://env:8080"
    assert options["namespaces"] == ["application", "demo.yaml"]
    assert options["timeout"] == 3.5
    assert options["heartbeat_interval"] == 0.0
    assert options["auto_discover"] is True
    assert options["watch_mode"] == "poll"
    assert options["meta_server_url"] == "http://meta:8080"

    client = ApolloClient.from_env(
        config_server_url="http://127.0.0.1:1",
        namespaces=["application"],
        cache_enabled=False,
    )
    try:
        assert client.app_id == "env-app"
        assert client.cluster == "prod"
        assert client.watch_mode == "poll"
        assert client.heartbeat_interval is None
        assert client.meta_server_urls == ("http://meta:8080",)
    finally:
        client.stop()


def _heartbeat_status(client: ApolloClient):
    def check():
        status = client.status()
        if status["heartbeatCount"] >= 1 and status["heartbeatOk"] is True:
            return status
        return None

    return check
