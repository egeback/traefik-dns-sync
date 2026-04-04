"""OPNsense Unbound DNS provider.

Note: OPNsense Unbound only supports A-record host overrides.
TXT ownership records are not supported — this provider returns empty lists
for TXT operations. Ownership tracking falls back to the state file.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from traefik_dns_sync.config import OpnsenseConfig
from traefik_dns_sync.models import DnsRecord, TxtRecord

logger = logging.getLogger(__name__)


class OpnsenseProvider:
    """Manage DNS host overrides in OPNsense Unbound."""

    # OPNsense Unbound does not support TXT records via host overrides API
    supports_txt = False

    def __init__(self, config: OpnsenseConfig) -> None:
        self._config = config
        self._base_url = f"https://{config.host}"
        self._auth = (config.api_key, config.api_secret)

    @property
    def name(self) -> str:
        return "opnsense"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            auth=self._auth,
            verify=not self._config.skip_tls_verify,
            timeout=30.0,
        )

    async def get_records(self, domain_filter: list[str] | None = None) -> list[DnsRecord]:
        """Fetch all host overrides from OPNsense Unbound."""
        async with self._client() as client:
            resp = await client.get("/api/unbound/settings/searchHostOverride")
            resp.raise_for_status()
            data = resp.json()

        records: list[DnsRecord] = []
        for row in data.get("rows", []):
            hostname = row.get("hostname", "")
            domain = row.get("domain", "")
            ip = row.get("server", "")
            if not hostname or not ip:
                continue

            record = DnsRecord(hostname=hostname, ip=ip, domain=domain or None)
            matches = any(
                record.fqdn.endswith(f".{d}") or record.fqdn == d
                for d in domain_filter
            )
            if domain_filter and not matches:
                continue
            records.append(record)

        return records

    async def _find_override(self, record: DnsRecord) -> dict[str, Any] | None:
        """Find an existing host override matching the record."""
        async with self._client() as client:
            resp = await client.get("/api/unbound/settings/searchHostOverride")
            resp.raise_for_status()
            data = resp.json()

        for row in data.get("rows", []):
            if (
                row.get("hostname") == record.hostname
                and row.get("domain") == (record.domain or "")
            ):
                return row
        return None

    async def create_record(self, record: DnsRecord) -> str | None:
        """Create a host override in OPNsense Unbound."""
        payload = {
            "host": {
                "enabled": "1",
                "hostname": record.hostname,
                "domain": record.domain or "",
                "server": record.ip,
                "description": "Managed by traefik-dns-sync",
            }
        }

        async with self._client() as client:
            resp = await client.post("/api/unbound/settings/addHostOverride", json=payload)
            resp.raise_for_status()
            result = resp.json()

            uuid = result.get("uuid")
            if uuid:
                await self._reconfigure(client)
                logger.info(
                    "Created OPNsense record: %s -> %s (uuid: %s)",
                    record.fqdn, record.ip, uuid,
                )
            else:
                logger.warning("OPNsense create returned no UUID: %s", result)

            return uuid

    async def update_record(self, record: DnsRecord, provider_id: str | None = None) -> None:
        """Update an existing host override."""
        uuid = provider_id
        if not uuid:
            existing = await self._find_override(record)
            if not existing:
                logger.warning("Cannot update — record not found: %s", record.fqdn)
                return
            uuid = existing.get("uuid")

        payload = {
            "host": {
                "enabled": "1",
                "hostname": record.hostname,
                "domain": record.domain or "",
                "server": record.ip,
                "description": "Managed by traefik-dns-sync",
            }
        }

        async with self._client() as client:
            resp = await client.post(f"/api/unbound/settings/setHostOverride/{uuid}", json=payload)
            resp.raise_for_status()
            await self._reconfigure(client)
            logger.info("Updated OPNsense record: %s -> %s", record.fqdn, record.ip)

    async def delete_record(self, record: DnsRecord, provider_id: str | None = None) -> None:
        """Delete a host override."""
        uuid = provider_id
        if not uuid:
            existing = await self._find_override(record)
            if not existing:
                logger.warning("Cannot delete — record not found: %s", record.fqdn)
                return
            uuid = existing.get("uuid")

        async with self._client() as client:
            resp = await client.post(f"/api/unbound/settings/delHostOverride/{uuid}")
            resp.raise_for_status()
            await self._reconfigure(client)
            logger.info("Deleted OPNsense record: %s", record.fqdn)

    async def create_txt_record(self, record: TxtRecord) -> str | None:
        """Not supported — Unbound has no TXT host override API."""
        logger.debug("OPNsense: skipping TXT create (not supported): %s", record.fqdn)
        return None

    async def delete_txt_record(
        self, record: TxtRecord, provider_id: str | None = None,
    ) -> None:
        """Not supported — Unbound has no TXT host override API."""
        logger.debug("OPNsense: skipping TXT delete (not supported): %s", record.fqdn)

    async def get_txt_records(
        self, prefix: str, domain_filter: list[str] | None = None,
    ) -> list[TxtRecord]:
        """Not supported — returns empty list."""
        return []

    async def _reconfigure(self, client: httpx.AsyncClient) -> None:
        """Apply changes by reconfiguring Unbound."""
        resp = await client.post("/api/unbound/service/reconfigure")
        resp.raise_for_status()
