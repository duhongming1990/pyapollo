"""In-memory Apollo config service used for tests and the FastAPI demo."""

from __future__ import annotations

from .app import create_app
from .store import (
    DEFAULT_PROPERTIES,
    DEFAULT_YAML,
    ConfigStore,
    NamespaceEntry,
)

__all__ = [
    "DEFAULT_PROPERTIES",
    "DEFAULT_YAML",
    "ConfigStore",
    "NamespaceEntry",
    "create_app",
]
