"""UniFi Gateway DNS provider."""

from __future__ import annotations

import logging

import httpx

from traefik_dns_sync.config import UnifiConfig
from traefik_dns_sync.models import DnsRecord, TxtRecord

logger = logging.getLogger(__name__)


class UnifiProvider:
    """Manage static DNS records on UniFi Gateway."""

    def __init__(self, config: UnifiConfig) -> None:
        self._config = config
        self._base_url = f"https://{config.host}"

    @property
    def name(self) -> str:
        return "unifi"

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "X-API-KEY": self._config.api_key,
                "Content-Type": "application/json",
            },
            verify=not self._config.skip_tls_verify,
            timeout=30.0,
        )

    def _dns_path(self) -> str:
        return f"/proxy/network/v2/api/site/{self._config.site}/static-dns"

    async def get_records(self, domain_filter: list[str] | None = None) -> list[DnsRecord]:
        """Fetch all static DNS records from UniFi."""
        async with self._client() as client:
            resp = await client.get(self._dns_path())
            resp.raise_for_status()
            data = resp.json()

        records: list[DnsRecord] = []
        for entry in data:
            key = entry.get("key", "")
            value = entry.get("value", "")
            record_type = entry.get("record_type", "A")
            if not key or not value or record_type != "A":
                continue

            record = DnsRecord.from_fqdn(key, value)
            if domain_filter and not any(
                record.fqdn.endswith(f".{d}") or record.fqdn == d for d in domain_filter
            ):
                continue
            records.append(record)

        return records

    async def create_record(self, record: DnsRecord) -> str | None:
        """Create a static DNS record on UniFi Gateway."""
        payload = {
            "key": record.fqdn,
            "record_type": "A",
            "value": record.ip,
            "enabled": True,
        }

        async with self._client() as client:
            resp = await client.post(self._dns_path(), json=payload)
            resp.raise_for_status()
            result = resp.json()

            record_id = result.get("_id")
            logger.info(
                "Created UniFi record: %s -> %s (id: %s)",
                record.fqdn, record.ip, record_id,
            )
            return record_id

    async def update_record(self, record: DnsRecord, provider_id: str | None = None) -> None:
        """Update an existing static DNS record."""
        if not provider_id:
            provider_id = await self._find_record_id(record.fqdn)
            if not provider_id:
                logger.warning("Cannot update — record not found: %s", record.fqdn)
                return

        payload = {
            "key": record.fqdn,
            "record_type": "A",
            "value": record.ip,
            "enabled": True,
        }

        async with self._client() as client:
            resp = await client.put(
                f"{self._dns_path()}/{provider_id}",
                json=payload,
            )
            resp.raise_for_status()
            logger.info("Updated UniFi record: %s -> %s", record.fqdn, record.ip)

    async def delete_record(self, record: DnsRecord, provider_id: str | None = None) -> None:
        """Delete a static DNS record."""
        if not provider_id:
            provider_id = await self._find_record_id(record.fqdn)
            if not provider_id:
                logger.warning("Cannot delete — record not found: %s", record.fqdn)
                return

        async with self._client() as client:
            resp = await client.delete(f"{self._dns_path()}/{provider_id}")
            resp.raise_for_status()
            logger.info("Deleted UniFi record: %s", record.fqdn)

    async def create_txt_record(self, record: TxtRecord) -> str | None:
        """Create a TXT ownership record on UniFi Gateway."""
        # UniFi requires TXT values with commas to be quoted
        value = record.value
        if "," in value and not value.startswith('"'):
            value = f'"{value}"'

        payload = {
            "key": record.fqdn,
            "record_type": "TXT",
            "value": value,
            "enabled": True,
        }

        async with self._client() as client:
            resp = await client.post(self._dns_path(), json=payload)
            resp.raise_for_status()
            result = resp.json()

            record_id = result.get("_id")
            logger.info("Created UniFi TXT record: %s (id: %s)", record.fqdn, record_id)
            return record_id

    async def delete_txt_record(
        self, record: TxtRecord, provider_id: str | None = None,
    ) -> None:
        """Delete a TXT ownership record."""
        if not provider_id:
            provider_id = await self._find_record_id(record.fqdn, record_type="TXT")
            if not provider_id:
                logger.warning("Cannot delete TXT — record not found: %s", record.fqdn)
                return

        async with self._client() as client:
            resp = await client.delete(f"{self._dns_path()}/{provider_id}")
            resp.raise_for_status()
            logger.info("Deleted UniFi TXT record: %s", record.fqdn)

    async def get_txt_records(
        self, prefix: str, domain_filter: list[str] | None = None,
    ) -> list[TxtRecord]:
        """Fetch all TXT ownership records matching the prefix."""
        async with self._client() as client:
            resp = await client.get(self._dns_path())
            resp.raise_for_status()
            data = resp.json()

        records: list[TxtRecord] = []
        txt_prefix = f"{prefix}.a-"
        for entry in data:
            key = entry.get("key", "")
            value = entry.get("value", "").strip('"')  # UniFi stores quoted TXT values
            record_type = entry.get("record_type", "")
            if record_type != "TXT" or not key.startswith(txt_prefix):
                continue

            if domain_filter:
                a_fqdn = TxtRecord.extract_a_record_fqdn(key, prefix)
                if a_fqdn and not any(
                    a_fqdn.endswith(f".{d}") or a_fqdn == d for d in domain_filter
                ):
                    continue

            records.append(TxtRecord(fqdn=key, value=value))

        return records

    async def _find_record_id(
        self, fqdn: str, record_type: str = "A",
    ) -> str | None:
        """Find the UniFi record ID for a given FQDN and type."""
        async with self._client() as client:
            resp = await client.get(self._dns_path())
            resp.raise_for_status()
            data = resp.json()

        for entry in data:
            if entry.get("key") == fqdn and entry.get("record_type", "A") == record_type:
                return entry.get("_id")
        return None
