"""Tests for the native asyncio client."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from pathlib import Path
from urllib.parse import urlsplit

from apollo_client import AsyncApolloClient
from apollo_client.async_client import AsyncApolloTransport
from tests.helpers import publish_config


async def wait_for(
    predicate,
    *,
    timeout: float = 10.0,
    interval: float = 0.05,
    message: str = "async condition",
):
    """Async counterpart of :func:`tests.helpers.wait_until`."""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}")


def make_client(mock_apollo, tmp_path: Path, **overrides) -> AsyncApolloClient:
    options = {
        "app_id": "demo-app",
        "config_server_url": mock_apollo.base_url,
        "namespaces": ["application"],
        "timeout": 2.0,
        "long_poll_timeout": 0.5,
        "cache_dir": tmp_path / "async-cache",
        "heartbeat_interval": None,
    }
    options.update(overrides)
    return AsyncApolloClient(**options)


def test_async_pull_and_typed_reads(mock_apollo, tmp_path: Path) -> None:
    async def scenario() -> None:
        client = make_client(
            mock_apollo, tmp_path, namespaces=["application", "demo.yaml"]
        )
        await client.start(watch=False)
        try:
            assert client.get_value("app.name") == "Apollo Python Client"
            assert client.get_int("app.limits.max") == 100
            assert client.get_bool("app.feature.new_ui") is True
            assert (
                client.get_int("server.port", namespace="demo.yaml") == 8080
            )
            fetched = await client.aget_config("demo.yaml")
            assert fetched is not None
            assert fetched.format == "yaml"
            assert client.is_started is True
            assert client.is_running is False
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_async_long_poll_hot_update_with_coroutine_listener(
    mock_apollo, tmp_path: Path
) -> None:
    async def scenario() -> None:
        client = make_client(mock_apollo, tmp_path)
        events = []
        received = asyncio.Event()

        async def on_change(event) -> None:
            events.append(event)
            received.set()

        client.add_listener(on_change, namespace="application")
        await client.start()
        try:
            await asyncio.to_thread(
                publish_config,
                mock_apollo.base_url,
                "application",
                "app.name=Async Hot\napp.limits.max=123\n",
            )
            await asyncio.wait_for(received.wait(), timeout=10.0)
            assert events[0].changes["app.name"].new == "Async Hot"
            assert client.get_int("app.limits.max") == 123
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_async_poll_mode_and_refresh(mock_apollo, tmp_path: Path) -> None:
    async def scenario() -> None:
        client = make_client(
            mock_apollo,
            tmp_path,
            watch_mode="poll",
            poll_interval=0.1,
            heartbeat_interval=None,
        )
        events = []
        client.add_listener(events.append, namespace="application")
        await client.start()
        try:
            await asyncio.to_thread(
                publish_config,
                mock_apollo.base_url,
                "application",
                "app.name=Poll Async\napp.limits.max=5\n",
            )
            event = await wait_for(
                lambda: events[0] if events else None,
                message="async poll change",
            )
            assert event.changes["app.name"].new == "Poll Async"

            result = await client.refresh("application")
            assert "application" in result
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_async_discovery_and_meta_server(mock_apollo, tmp_path: Path) -> None:
    async def scenario() -> None:
        await asyncio.to_thread(
            publish_config,
            mock_apollo.base_url,
            "extra.yaml",
            "answer: 42\n",
            fmt="yaml",
        )
        client = make_client(
            mock_apollo,
            tmp_path,
            config_server_url="http://127.0.0.1:1",
            meta_server_url=mock_apollo.base_url,
            namespaces=["application"],
        )
        await client.start(watch=False)
        try:
            assert mock_apollo.base_url in client.config_server_urls
            assert client.status()["metaServers"] == [mock_apollo.base_url]
            names = await client.discover_namespaces()
            assert {"application", "demo.yaml", "extra.yaml"}.issubset(names)
            assert client.get_int("answer", namespace="extra.yaml") == 42
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_async_update_options(mock_apollo, tmp_path: Path) -> None:
    async def scenario() -> None:
        client = make_client(
            mock_apollo, tmp_path, watch_mode="poll", poll_interval=0.1
        )
        await client.start()
        try:
            await asyncio.to_thread(
                publish_config,
                mock_apollo.base_url,
                "extra.yaml",
                "answer: 7\n",
                fmt="yaml",
            )
            await client.update_options(
                namespaces=["extra.yaml"],
                ip="10.9.8.7",
                timeout=3.0,
                heartbeat_interval=0.2,
            )
            assert client.get_int("answer", namespace="extra.yaml") == 7
            assert client.heartbeat_interval == 0.2
            assert client._transport.ip == "10.9.8.7"

            events = []
            client.add_listener(events.append, namespace="application")
            await client.update_options(watch_mode="long_poll")
            await asyncio.to_thread(
                publish_config,
                mock_apollo.base_url,
                "application",
                "app.name=Async Switched\napp.limits.max=6\n",
            )
            event = await wait_for(
                lambda: events[0] if events else None,
                message="async mode switch change",
            )
            assert event.changes["app.name"].new == "Async Switched"
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_async_cache_offline_fallback(mock_apollo, tmp_path: Path) -> None:
    async def scenario() -> None:
        cache_dir = tmp_path / "async-cache"
        first = make_client(
            mock_apollo,
            tmp_path,
            namespaces=["application", "demo.yaml"],
            cache_dir=cache_dir,
        )
        await first.start(watch=False)
        assert first.get_value("app.name") == "Apollo Python Client"
        await first.stop()

        offline = make_client(
            mock_apollo,
            tmp_path,
            namespaces=["application", "demo.yaml"],
            config_server_url="http://127.0.0.1:1",
            timeout=0.2,
            cache_dir=cache_dir,
        )
        # The cache is preloaded at construction, before any network call.
        assert offline.get_value("app.name") == "Apollo Python Client"
        loaded = await offline.aget_config("application")
        assert loaded is not None
        assert loaded.source == "cache"
        await offline.start(watch=False)  # unreachable, must not raise
        assert offline.get_value("app.name") == "Apollo Python Client"
        await offline.stop()

    asyncio.run(scenario())


def test_async_heartbeat_status(mock_apollo, tmp_path: Path) -> None:
    async def scenario() -> None:
        client = make_client(
            mock_apollo,
            tmp_path,
            watch_mode="poll",
            poll_interval=30.0,
            heartbeat_interval=0.2,
        )

        def heartbeat_ready():
            status = client.status()
            if (
                status["heartbeatCount"] >= 1
                and status["heartbeatOk"] is True
            ):
                return status
            return None

        await client.start()
        try:
            status = await wait_for(
                heartbeat_ready, message="async heartbeat status"
            )
            assert status["lastHeartbeatAt"]
            assert status["heartbeatError"] is None
        finally:
            await client.stop()

    asyncio.run(scenario())


def test_async_context_manager(mock_apollo, tmp_path: Path) -> None:
    async def scenario() -> None:
        client = make_client(mock_apollo, tmp_path)
        async with client:
            assert client.is_running is True
            assert client.get_value("app.name") == "Apollo Python Client"
        assert client.is_started is False

    asyncio.run(scenario())


def test_async_transport_signature_matches_sync_format() -> None:
    transport = AsyncApolloTransport(
        "http://example.com:8080",
        "demo-app",
        "default",
        secret="top-secret",
        timeout=1.0,
    )
    url, headers = transport.prepare(
        "/configs/demo-app/default/application", {"ip": "10.0.0.1"}
    )

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


def test_async_client_requires_httpx() -> None:
    # httpx is installed for tests, but a missing extra must produce a clear
    # ImportError instead of an obscure module-level failure.  A subprocess
    # keeps the simulated missing dependency out of this test process.
    import subprocess
    import sys
    import textwrap

    root = Path(__file__).resolve().parents[1]
    code = textwrap.dedent(
        """
        import builtins
        import sys

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "httpx" or name.startswith("httpx."):
                raise ImportError("No module named 'httpx'")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        sys.path.insert(0, "src")
        import apollo_client

        apollo_client.AsyncApolloClient
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "pyapollo-config[async]" in result.stderr
