"""OPNsense Unbound DNS provider.

Supports native TXT records on OPNsense >= 25.7 via the host override API
(rr=TXT, txtdata field). On older versions, falls back to storing ownership
in the host override description field.
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

    def __init__(self, config: OpnsenseConfig, owner_id: str = "") -> None:
        self._config = config
        self._owner_id = owner_id
        self._base_url = f"https://{config.host}"
        self._auth = (config.api_key, config.api_secret)
        self._native_txt: bool | None = None  # detected on first use

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

    def _make_description(self, owner_id: str) -> str:
        return TxtRecord.make_value(owner_id)

    async def _get_all_overrides(self) -> list[dict[str, Any]]:
        async with self._client() as client:
            resp = await client.get(
                "/api/unbound/settings/searchHostOverride"
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("rows", [])

    async def get_records(
        self, domain_filter: list[str] | None = None,
    ) -> list[DnsRecord]:
        """Fetch A-record host overrides from OPNsense Unbound."""
        records: list[DnsRecord] = []
        for row in await self._get_all_overrides():
            hostname = row.get("hostname", "")
            domain = row.get("domain", "")
            ip = row.get("server", "")
            rr = row.get("rr", "A")
            if not hostname or not ip or rr not in ("A", ""):
                continue

            record = DnsRecord(
                hostname=hostname, ip=ip, domain=domain or None,
            )
            if domain_filter and not any(
                record.fqdn.endswith(f".{d}") or record.fqdn == d
                for d in domain_filter
            ):
                continue
            records.append(record)

        return records

    async def _find_override(
        self, record: DnsRecord,
    ) -> dict[str, Any] | None:
        for row in await self._get_all_overrides():
            rr = row.get("rr", "A")
            if rr not in ("A", ""):
                continue
            if (
                row.get("hostname") == record.hostname
                and row.get("domain") == (record.domain or "")
            ):
                return row
        return None

    async def create_record(self, record: DnsRecord) -> str | None:
        """Create an A-record host override."""
        payload = {
            "host": {
                "enabled": "1",
                "hostname": record.hostname,
                "domain": record.domain or "",
                "server": record.ip,
                "description": self._make_description(
                    self._owner_id,
                ),
            }
        }

        async with self._client() as client:
            resp = await client.post(
                "/api/unbound/settings/addHostOverride",
                json=payload,
            )
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
                logger.warning(
                    "OPNsense create returned no UUID: %s", result,
                )

            return uuid

    async def update_record(
        self, record: DnsRecord, provider_id: str | None = None,
    ) -> None:
        uuid = provider_id
        if not uuid:
            existing = await self._find_override(record)
            if not existing:
                logger.warning(
                    "Cannot update — record not found: %s", record.fqdn,
                )
                return
            uuid = existing.get("uuid")

        payload = {
            "host": {
                "enabled": "1",
                "hostname": record.hostname,
                "domain": record.domain or "",
                "server": record.ip,
                "description": self._make_description(
                    self._owner_id,
                ),
            }
        }

        async with self._client() as client:
            resp = await client.post(
                f"/api/unbound/settings/setHostOverride/{uuid}",
                json=payload,
            )
            resp.raise_for_status()
            await self._reconfigure(client)
            logger.info(
                "Updated OPNsense record: %s -> %s",
                record.fqdn, record.ip,
            )

    async def delete_record(
        self, record: DnsRecord, provider_id: str | None = None,
    ) -> None:
        uuid = provider_id
        if not uuid:
            existing = await self._find_override(record)
            if not existing:
                logger.warning(
                    "Cannot delete — record not found: %s", record.fqdn,
                )
                return
            uuid = existing.get("uuid")

        async with self._client() as client:
            resp = await client.post(
                f"/api/unbound/settings/delHostOverride/{uuid}",
            )
            resp.raise_for_status()
            await self._reconfigure(client)
            logger.info("Deleted OPNsense record: %s", record.fqdn)

    # -- TXT ownership records --

    async def _try_native_txt(
        self, client: httpx.AsyncClient, txt: TxtRecord,
    ) -> str | None:
        """Try creating a native TXT host override (OPNsense >= 25.7)."""
        parts = txt.fqdn.split(".", 1)
        hostname = parts[0] if parts else txt.fqdn
        domain = parts[1] if len(parts) == 2 else ""

        payload = {
            "host": {
                "enabled": "1",
                "hostname": hostname,
                "domain": domain,
                "rr": "TXT",
                "server": "",
                "txtdata": txt.value,
                "description": "traefik-dns-sync TXT",
            }
        }

        resp = await client.post(
            "/api/unbound/settings/addHostOverride",
            json=payload,
        )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("result") == "saved":
                uuid = result.get("uuid")
                await self._reconfigure(client)
                return uuid
            # Validation error — txtdata not supported
            if result.get("result") == "failed":
                return None
        return None

    async def create_txt_record(
        self, record: TxtRecord,
    ) -> str | None:
        """Create TXT ownership record.

        Tries native TXT (>= 25.7) first, falls back to no-op
        (ownership stored in A-record description).
        """
        if self._native_txt is False:
            return None

        async with self._client() as client:
            uuid = await self._try_native_txt(client, record)

        if uuid:
            if self._native_txt is None:
                logger.info(
                    "OPNsense native TXT support detected",
                )
                self._native_txt = True
            logger.info(
                "Created OPNsense TXT: %s (uuid: %s)",
                record.fqdn, uuid,
            )
            return uuid

        if self._native_txt is None:
            logger.info(
                "OPNsense native TXT not available — "
                "using description field for ownership",
            )
            self._native_txt = False
        return None

    async def delete_txt_record(
        self, record: TxtRecord, provider_id: str | None = None,
    ) -> None:
        """Delete TXT ownership record."""
        if not provider_id:
            provider_id = await self._find_txt_override(record.fqdn)
            if not provider_id:
                return

        async with self._client() as client:
            resp = await client.post(
                f"/api/unbound/settings/delHostOverride/"
                f"{provider_id}",
            )
            resp.raise_for_status()
            await self._reconfigure(client)
            logger.info("Deleted OPNsense TXT: %s", record.fqdn)

    async def get_txt_records(
        self, prefix: str, domain_filter: list[str] | None = None,
    ) -> list[TxtRecord]:
        """Read ownership from native TXT records AND description fields.

        Checks both sources so recovery works regardless of which
        mechanism was used to create the records.
        """
        records: list[TxtRecord] = []
        seen: set[str] = set()
        txt_prefix = f"{prefix}.a-"

        for row in await self._get_all_overrides():
            rr = row.get("rr", "A")
            hostname = row.get("hostname", "")
            domain = row.get("domain", "")
            if not hostname:
                continue

            fqdn = f"{hostname}.{domain}" if domain else hostname

            # Source 1: Native TXT records
            if rr == "TXT" and fqdn.startswith(txt_prefix):
                txtdata = row.get("txtdata", "")
                if "heritage=traefik-dns-sync" not in txtdata:
                    continue

                if domain_filter:
                    a_fqdn = TxtRecord.extract_a_record_fqdn(
                        fqdn, prefix,
                    )
                    if a_fqdn and not any(
                        a_fqdn.endswith(f".{d}") or a_fqdn == d
                        for d in domain_filter
                    ):
                        continue

                records.append(TxtRecord(fqdn=fqdn, value=txtdata))
                seen.add(fqdn)
                continue

            # Source 2: Description field fallback
            if rr in ("A", ""):
                description = row.get("description", "")
                if "heritage=traefik-dns-sync" not in description:
                    continue

                txt_fqdn = TxtRecord.make_fqdn(fqdn, prefix)
                if txt_fqdn in seen:
                    continue

                if domain_filter and not any(
                    fqdn.endswith(f".{d}") or fqdn == d
                    for d in domain_filter
                ):
                    continue

                records.append(
                    TxtRecord(fqdn=txt_fqdn, value=description),
                )

        return records

    async def _find_txt_override(self, fqdn: str) -> str | None:
        """Find UUID of a TXT host override by FQDN."""
        parts = fqdn.split(".", 1)
        hostname = parts[0] if parts else fqdn
        domain = parts[1] if len(parts) == 2 else ""

        for row in await self._get_all_overrides():
            if (
                row.get("rr") == "TXT"
                and row.get("hostname") == hostname
                and row.get("domain") == domain
            ):
                return row.get("uuid")
        return None

    async def _reconfigure(self, client: httpx.AsyncClient) -> None:
        resp = await client.post("/api/unbound/service/reconfigure")
        resp.raise_for_status()
