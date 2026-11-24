"""Apollo (Ctrip) configuration client for Python.

Example
-------
>>> from apollo_client import ApolloClient
>>> client = ApolloClient(
...     app_id="demo-app",
...     config_server_url="http://127.0.0.1:18080",
...     namespaces=["application", "demo.yaml"],
... )
>>> client.start()
>>> client.get_value("app.name")
'Apollo Python Client'
>>> client.get_int("server.port", namespace="demo.yaml")
8080

For asyncio applications import ``AsyncApolloClient`` (requires the ``async``
extra: ``pip install "pyapollo-config[async]"``)::

    from apollo_client import AsyncApolloClient

    async with AsyncApolloClient(app_id="demo-app", ...) as client:
        client.get_value("app.name")
"""

from __future__ import annotations

from ._version import __version__
from .cache import DiskCache
from .client import WATCH_MODES, ApolloClient, ChangeListener
from .config import (
    SUPPORTED_FORMATS,
    ChangeEvent,
    ConfigChange,
    NamespaceConfig,
    detect_format,
    diff_configs,
    diff_mappings,
    flatten_mapping,
)
from .env import options_from_env
from .errors import (
    ApolloAuthError,
    ApolloConnectionError,
    ApolloError,
    ApolloHTTPError,
    ApolloParseError,
    NamespaceNotFoundError,
)
from .meta import MetaServerResolver, parse_meta_response
from .parser import parse_content, parse_json, parse_properties, parse_yaml

__all__ = [
    "SUPPORTED_FORMATS",
    "WATCH_MODES",
    "ApolloAuthError",
    "ApolloClient",
    "ApolloConnectionError",
    "ApolloError",
    "ApolloHTTPError",
    "ApolloParseError",
    "ChangeEvent",
    "ChangeListener",
    "ConfigChange",
    "DiskCache",
    "MetaServerResolver",
    "NamespaceConfig",
    "NamespaceNotFoundError",
    "__version__",
    "detect_format",
    "diff_configs",
    "diff_mappings",
    "flatten_mapping",
    "options_from_env",
    "parse_content",
    "parse_json",
    "parse_meta_response",
    "parse_properties",
    "parse_yaml",
]

_LAZY_ASYNC_NAMES = {"AsyncApolloClient", "AsyncApolloTransport", "AsyncChangeListener"}


def __getattr__(name: str):
    """Import the optional asyncio API only when it is actually requested."""

    if name in _LAZY_ASYNC_NAMES:
        from . import async_client

        return getattr(async_client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
