"""Pytest fixtures for the Apollo client / FastAPI demo tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from apollo_client import ApolloClient
from demo.main import create_app as create_demo_app
from mock_apollo import create_app as create_mock_app
from tests.helpers import UvicornTestServer


@pytest.fixture
def mock_apollo() -> Iterator[UvicornTestServer]:
    """A fresh mock Apollo config service for each test."""

    server = UvicornTestServer(create_mock_app()).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def apollo_client(
    mock_apollo: UvicornTestServer, tmp_path: Path
) -> Iterator[ApolloClient]:
    """A connected client watching properties + YAML namespaces."""

    client = ApolloClient(
        app_id="demo-app",
        config_server_url=mock_apollo.base_url,
        namespaces=["application", "demo.yaml"],
        timeout=2.0,
        long_poll_timeout=1.0,
        cache_dir=tmp_path / "apollo-cache",
    )
    client.start()
    try:
        yield client
    finally:
        client.stop(wait=True, timeout=3.0)


@pytest.fixture
def demo_server(
    apollo_client: ApolloClient,
) -> Iterator[UvicornTestServer]:
    """The FastAPI demo app wired to the connected test client."""

    server = UvicornTestServer(create_demo_app(apollo_client)).start()
    try:
        yield server
    finally:
        server.stop()
