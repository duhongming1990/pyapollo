# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-11-24

### Added

- **Namespace auto-discovery** through `/apps/{appId}/clusters/{cluster}/namespaces`
  (`auto_discover=True` and `discover_namespaces()`); falls back to the explicit
  namespace list when the endpoint is unavailable.
- **Scheduled polling** mode (`watch_mode="poll"`, `poll_interval`) as an
  alternative to `/notifications/v2` long polling.
- **Meta Server discovery**: `meta_server_url` resolves Config Service addresses
  from `/services/config` at start-up and refreshes them every
  `meta_refresh_interval` seconds, supporting string and object payloads.
- **Heartbeat / reconciliation pulls** (`heartbeat_interval`, default 600s):
  re-verify every namespace and repair notifications missed across restarts.
- **Runtime parameter updates** with `ApolloClient.update_options()`: URLs,
  app/cluster, secret, timeouts, IP, watch mode, intervals, namespaces and
  Meta Server can change without rebuilding the client.
- **Native asyncio client** `AsyncApolloClient` (optional `[async]` extra,
  httpx) with long polling, polling, discovery, heartbeat, cache and
  sync/async listeners.
- `ApolloClient.status()` / `AsyncApolloClient.status()` expose watch mode,
  Config Service URLs, Meta Servers, last pull/heartbeat and heartbeat errors.
- New `APOLLO_*` environment variables for watch mode, intervals, Meta Server
  and auto discovery.
- Mock Apollo now implements namespace discovery and `/services/config`.
- New tests for discovery, polling, Meta Server, heartbeat, runtime updates and
  the asyncio client.
- GitHub Actions workflow (`.github/workflows/publish.yml`) that runs tests,
  verifies the tag against the package version, builds wheel/sdist and
  publishes to PyPI through Trusted Publishing when a `v*` tag is pushed.

### Changed

- The watcher is split into a watch thread/task and a maintenance thread/task,
  so long polls no longer delay heartbeat or Meta Server refresh work.
- `pyproject.toml` gains the `async` optional dependency group and the
  `Framework :: AsyncIO` classifier; package version is now 0.2.0.
- `dist/` build outputs are no longer committed; releases are built by the
  publish workflow on tag pushes.

## [0.1.0] - 2026-11-24

### Added

- Properties, YAML and JSON namespace parsing with format inference.
- Initial pull, manual `refresh()`, and `/notifications/v2` long-polling hot reload.
- Immutable `NamespaceConfig` snapshots with dotted-path and typed accessors.
- Structured change events (`added` / `updated` / `removed`) and namespace/key filters.
- Disk cache with atomic writes and offline fallback.
- Apollo HMAC-SHA1 `secret` authentication.
- Multiple Config Service URLs with automatic failover.
- FastAPI demo with config endpoints, SSE stream and live dashboard.
- In-memory Mock Apollo config service.
- Test suite covering parsers, pull, hot reload, cache, auth, FastAPI and SSE.
