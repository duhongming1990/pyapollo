"""End-to-end tests for the FastAPI demo and its SSE stream."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator

import httpx
import requests

from tests.helpers import publish_config


def _read_sse_event(lines: Iterator[str]) -> tuple[str | None, dict | None]:
    event: str | None = None
    data: list[str] = []
    for line in lines:
        if line == "":
            if event is None and not data:
                continue
            return event, json.loads("\n".join(data)) if data else None
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    raise AssertionError("SSE stream ended before a complete event was received")


def test_health_config_and_dashboard(demo_server) -> None:
    base = demo_server.base_url

    health = requests.get(f"{base}/health", timeout=5.0).json()
    assert health["status"] == "ok"
    assert health["appId"] == "demo-app"
    assert health["watching"] is True
    assert health["watchMode"] == "long_poll"
    assert health["metaServers"] == []
    assert set(health["namespaces"]) == {"application", "demo.yaml"}

    config = requests.get(f"{base}/config", timeout=5.0).json()
    assert config["appId"] == "demo-app"
    assert set(config["namespaces"]) == {"application", "demo.yaml"}
    properties = config["namespaces"]["application"]
    assert properties["format"] == "properties"
    assert properties["data"]["app.name"] == "Apollo Python Client"
    assert "app.name=Apollo Python Client" in properties["raw"]

    yaml_config = config["namespaces"]["demo.yaml"]
    assert yaml_config["format"] == "yaml"
    assert yaml_config["data"]["server"]["port"] == 8080

    value = requests.get(
        f"{base}/config/application/app.name", timeout=5.0
    ).json()
    assert value == {
        "namespace": "application",
        "key": "app.name",
        "value": "Apollo Python Client",
    }

    yaml_value = requests.get(
        f"{base}/config/demo.yaml/server.port", timeout=5.0
    ).json()
    assert yaml_value["value"] == 8080

    namespace = requests.get(f"{base}/config/demo.yaml", timeout=5.0).json()
    assert namespace["namespace"] == "demo.yaml"
    assert namespace["data"]["server"]["host"] == "0.0.0.0"

    assert (
        requests.get(
            f"{base}/config/application/does-not-exist", timeout=5.0
        ).status_code
        == 404
    )
    assert (
        requests.get(f"{base}/config/not-loaded", timeout=5.0).status_code
        == 404
    )

    dashboard = requests.get(f"{base}/", timeout=5.0)
    assert dashboard.status_code == 200
    assert "Apollo Config Console" in dashboard.text


def test_refresh_endpoint_updates_values(demo_server, mock_apollo) -> None:
    base = demo_server.base_url
    publish_config(
        mock_apollo.base_url,
        "application",
        "app.name=Refreshed Now\napp.limits.max=777\n",
    )

    response = requests.post(
        f"{base}/config/refresh",
        params={"namespace": "application"},
        timeout=10.0,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["refreshed"] == ["application"]

    value = requests.get(
        f"{base}/config/application/app.name", timeout=5.0
    ).json()
    assert value["value"] == "Refreshed Now"
    maximum = requests.get(
        f"{base}/config/application/app.limits.max", timeout=5.0
    ).json()
    assert maximum["value"] == "777"


def test_refresh_unknown_namespace_returns_502(demo_server) -> None:
    response = requests.post(
        f"{demo_server.base_url}/config/refresh",
        params={"namespace": "does-not-exist.yaml"},
        timeout=10.0,
    )
    assert response.status_code == 502


def test_sse_stream_receives_hot_update(demo_server, mock_apollo) -> None:
    base = demo_server.base_url
    trigger = threading.Timer(
        0.3,
        lambda: publish_config(
            mock_apollo.base_url,
            "application",
            "app.name=SSE Updated\napp.limits.max=555\n",
        ),
    )
    trigger.daemon = True

    with httpx.Client(
        timeout=httpx.Timeout(10.0, connect=5.0, read=10.0)
    ) as client:
        with client.stream("GET", f"{base}/events") as response:
            assert response.status_code == 200
            lines = response.iter_lines()

            ready_event, ready_data = _read_sse_event(lines)
            assert ready_event == "ready"
            assert ready_data is not None
            assert ready_data["appId"] == "demo-app"
            assert "application" in ready_data["namespaces"]

            trigger.start()
            change_event, change_data = _read_sse_event(lines)
            trigger.join(timeout=5.0)

            assert change_event == "config-change"
            assert change_data is not None
            assert change_data["namespace"] == "application"
            assert "app.name" in change_data["changedKeys"]
            assert (
                change_data["changes"]["app.name"]["new"] == "SSE Updated"
            )
            assert (
                change_data["changes"]["app.name"]["old"]
                == "Apollo Python Client"
            )

    value = requests.get(
        f"{base}/config/application/app.name", timeout=5.0
    ).json()
    assert value["value"] == "SSE Updated"
