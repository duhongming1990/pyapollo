"""Exceptions raised by :mod:`apollo_client`."""

from __future__ import annotations


class ApolloError(Exception):
    """Base class for every error raised by this package."""


class ApolloConnectionError(ApolloError):
    """Raised when none of the configured Apollo config servers is reachable."""


class ApolloHTTPError(ApolloError):
    """Raised for unexpected HTTP responses from the Apollo config service."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - inherited behaviour with extra detail
        detail = super().__str__()
        if self.status_code is not None:
            detail = f"{detail} (HTTP {self.status_code})"
        return detail


class ApolloAuthError(ApolloHTTPError):
    """Raised when Apollo rejects the configured app id / secret."""


class NamespaceNotFoundError(ApolloHTTPError):
    """Raised when the requested namespace does not exist in Apollo."""


class ApolloParseError(ApolloError):
    """Raised when a configuration payload cannot be parsed."""
