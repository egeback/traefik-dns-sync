# traefik-dns-sync

Automatically sync DNS records from Traefik Docker labels to DNS providers.

Like [external-dns](https://github.com/kubernetes-sigs/external-dns) but for Docker/Traefik — reads `Host()` rules from container labels and creates A-records in your DNS provider.

## Supported Providers

| Provider | Status | TXT Ownership | API |
|----------|--------|---------------|-----|
| **OPNsense Unbound** | Stable | Yes (native TXT >= 25.7, description fallback) | Host Override API |
| **UniFi Gateway** | Stable | Yes (native TXT records) | Static DNS API |
| **RFC 2136** | Stable | Yes (native TXT records) | Dynamic DNS Update (TSIG) |

> \* On OPNsense >= 25.7, native TXT records are created via the host override
> API (`rr=TXT`, `txtdata` field). On older versions, ownership is stored in
> the A-record's `description` field as a fallback. Detection is automatic —
> native TXT is attempted first, and if it fails the provider falls back to
> description-based tracking.

## Features

- **TXT ownership records** — like external-dns, creates `_tdns.a-<hostname>` TXT records to track which records are managed. Survives container restarts without persistent storage.
- **Docker event-driven** — syncs immediately on container start/stop/die, in addition to the regular polling interval.
- **Health endpoint** — HTTP health check at `/health` with JSON status.
- **Adopt existing** — optionally take over unmanaged DNS records by adding TXT ownership.
- **Multiple providers** — sync to one or more providers simultaneously.
- **Stateless by design** — state file is a cache, TXT records in DNS are the source of truth.

## How It Works

1. Reads Traefik routing rules from Docker container labels (or Traefik API)
2. Extracts hostnames from `Host()` matchers
3. On startup, reads TXT ownership records from DNS to rebuild state
4. Creates/updates A-records pointing to the configured host IP
5. Creates TXT ownership records alongside each A-record (where supported)
6. Skips records that already exist without ownership (unless `SYNC_ADOPT_EXISTING=true`)
7. Listens for Docker container events for immediate sync
8. Repeats on a configurable interval (default: 5 minutes)

## Quick Start

```yaml
# compose.yaml
services:
  traefik-dns-sync:
    image: ghcr.io/egeback/traefik-dns-sync:latest
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "8080:8080"  # Health endpoint
    environment:
      SYNC_HOST_IP: "192.168.1.10"
      SYNC_OWNER_ID: "docker-myhost"

      UNIFI_ENABLED: "true"
      UNIFI_HOST: "192.168.1.2"
      UNIFI_API_KEY: "${UNIFI_API_KEY}"
```

## Configuration

All configuration is via environment variables.

### Sync Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SYNC_HOST_IP` | *(required)* | IP address for A-records |
| `SYNC_INTERVAL` | `300` | Sync interval in seconds |
| `SYNC_OWNER_ID` | `traefik-dns-sync` | Owner ID for state tracking |
| `SYNC_POLICY` | `upsert-only` | `upsert-only` or `sync` (deletes stale) |
| `SYNC_DRY_RUN` | `false` | Log changes without applying |
| `SYNC_STATE_FILE` | `/data/state.json` | Path to state file (optional cache) |
| `SYNC_DOMAIN_FILTERS` | `[]` | JSON list of allowed domains |
| `SYNC_TXT_PREFIX` | `_tdns` | Prefix for TXT ownership records |
| `SYNC_ADOPT_EXISTING` | `false` | Take over unmanaged records by adding TXT ownership |
| `SYNC_HEALTH_PORT` | `8080` | Port for health endpoint (0 to disable) |

### Traefik Source

| Variable | Default | Description |
|----------|---------|-------------|
| `TRAEFIK_USE_DOCKER` | `true` | Read from Docker labels |
| `TRAEFIK_URL` | *(none)* | Traefik API URL (alternative to Docker) |
| `TRAEFIK_DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker socket path |

### OPNsense Provider

On OPNsense >= 25.7, native TXT records are supported via the host override API. On older versions, ownership is stored in the A-record `description` field as a fallback. Detection is automatic.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPNSENSE_ENABLED` | `false` | Enable OPNsense provider |
| `OPNSENSE_HOST` | | OPNsense hostname/IP |
| `OPNSENSE_API_KEY` | | API key |
| `OPNSENSE_API_SECRET` | | API secret |
| `OPNSENSE_SKIP_TLS_VERIFY` | `true` | Skip TLS verification |

### UniFi Provider

| Variable | Default | Description |
|----------|---------|-------------|
| `UNIFI_ENABLED` | `false` | Enable UniFi provider |
| `UNIFI_HOST` | | UniFi Gateway hostname/IP |
| `UNIFI_API_KEY` | | API key |
| `UNIFI_SKIP_TLS_VERIFY` | `true` | Skip TLS verification |
| `UNIFI_SITE` | `default` | UniFi site name |

### RFC 2136 Provider

Uses TSIG-authenticated DNS UPDATE messages. Compatible with BIND, PowerDNS, Knot DNS, and other servers supporting RFC 2136.

| Variable | Default | Description |
|----------|---------|-------------|
| `RFC2136_ENABLED` | `false` | Enable RFC 2136 provider |
| `RFC2136_HOST` | | DNS server address |
| `RFC2136_PORT` | `53` | DNS server port |
| `RFC2136_ZONE` | | DNS zone (e.g. `internal.example.com`) |
| `RFC2136_TSIG_KEY_NAME` | | TSIG key name |
| `RFC2136_TSIG_KEY_SECRET` | | TSIG key secret (base64) |
| `RFC2136_TSIG_KEY_ALGORITHM` | `hmac-sha256` | TSIG algorithm |

Supported TSIG algorithms: `hmac-sha256`, `hmac-sha512`, `hmac-sha384`, `hmac-sha224`, `hmac-sha1`, `hmac-md5`.

## TXT Ownership Records

traefik-dns-sync creates TXT records to track which A-records it manages, similar to external-dns:

```
A     grafana.internal.example.com          192.168.1.10
TXT   _tdns.a-grafana.internal.example.com  "heritage=traefik-dns-sync,traefik-dns-sync/owner=docker-myhost"
```

This means:
- **No persistent volume required** — state is recovered from DNS on startup
- **Multiple instances** can coexist using different `SYNC_OWNER_ID` values
- **Safe deletion** — only deletes records with matching TXT ownership
- **Existing records are safe** — records without TXT ownership are never modified or deleted

## Sync Policies

- **`upsert-only`** (default): Creates new records and updates existing ones. Never deletes. Safe for getting started.
- **`sync`**: Full reconciliation — also deletes DNS records that no longer have a matching Traefik route. Only deletes records with matching TXT ownership.

## Health Endpoint

When running as a daemon, an HTTP health endpoint is available:

```bash
curl http://localhost:8080/health
```

```json
{
  "status": "ok",
  "last_sync": "2024-01-15T10:30:00+00:00",
  "last_result": "created=2 updated=0 deleted=0 unchanged=10 errors=0",
  "last_error": null,
  "sync_count": 42
}
```

Returns HTTP 200 when healthy, 503 on error.

## One-Shot Mode

Run a single sync cycle and exit (no health endpoint, no Docker event watching):

```bash
traefik-dns-sync --once
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check src/ tests/
```

## License

MIT
