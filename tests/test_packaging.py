"""Release metadata smoke tests."""

from __future__ import annotations

from importlib.metadata import version

from apollo_client import __version__


def test_distribution_metadata_matches_package_version() -> None:
    assert version("pyapollo-config-client") == __version__
    assert __version__ == "0.2.0"
