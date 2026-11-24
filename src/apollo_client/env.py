"""Shared ``APOLLO_*`` environment variable support.

Both :class:`apollo_client.ApolloClient` and
:class:`apollo_client.AsyncApolloClient` read the same environment variables so
switching between the sync and async APIs does not require a new configuration
format.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .utils import parse_bool_env

_STRING_OPTIONS: dict[str, tuple[str, ...]] = {
    "app_id": ("APOLLO_APP_ID",),
    "cluster": ("APOLLO_CLUSTER",),
    "secret": ("APOLLO_SECRET",),
    "ip": ("APOLLO_IP",),
    "cache_dir": ("APOLLO_CACHE_DIR",),
    "watch_mode": ("APOLLO_WATCH_MODE",),
    "meta_server_url": ("APOLLO_META_SERVER", "APOLLO_META_SERVER_URL"),
}

_CONFIG_SERVER_VARS = ("APOLLO_CONFIG_SERVER_URL", "APOLLO_CONFIG_SERVER")

_FLOAT_OPTIONS: dict[str, str] = {
    "timeout": "APOLLO_TIMEOUT",
    "long_poll_timeout": "APOLLO_LONG_POLL_TIMEOUT",
    "poll_interval": "APOLLO_POLL_INTERVAL",
    "heartbeat_interval": "APOLLO_HEARTBEAT_INTERVAL",
    "meta_refresh_interval": "APOLLO_META_REFRESH_INTERVAL",
}

_BOOL_OPTIONS: dict[str, str] = {
    "cache_enabled": "APOLLO_CACHE_ENABLED",
    "preload_cache": "APOLLO_PRELOAD_CACHE",
    "auto_discover": "APOLLO_AUTO_DISCOVER",
}


def options_from_env(
    defaults: Mapping[str, Any] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Merge ``APOLLO_*`` environment variables over ``defaults``.

    Environment values always win, which lets production deployments keep the
    client construction in code while pointing it at the right environment via
    environment variables.
    """

    env = os.environ if environ is None else environ
    options: dict[str, Any] = dict(defaults or {})

    for option, names in _STRING_OPTIONS.items():
        for name in names:
            if name in env:
                options[option] = env[name]
                break

    for name in _CONFIG_SERVER_VARS:
        if name in env:
            options["config_server_url"] = env[name]
            break

    if "APOLLO_NAMESPACES" in env:
        options["namespaces"] = [
            part.strip()
            for part in env["APOLLO_NAMESPACES"].split(",")
            if part.strip()
        ]

    for option, name in _FLOAT_OPTIONS.items():
        if name in env:
            options[option] = float(env[name])

    for option, name in _BOOL_OPTIONS.items():
        if name in env:
            options[option] = parse_bool_env(env[name])

    return options
