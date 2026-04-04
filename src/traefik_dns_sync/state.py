"""State tracker — persists which DNS records are owned by this instance.

State is rebuilt from TXT ownership records in DNS on startup (source of truth).
The local state file is a cache that speeds up subsequent sync cycles.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from traefik_dns_sync.models import DnsRecord, ManagedRecord, SyncState, TxtRecord

logger = logging.getLogger(__name__)


class StateTracker:
    """Persist and query sync state."""

    def __init__(self, state_file: str, owner_id: str) -> None:
        self._path = Path(state_file)
        self._state = SyncState(owner_id=owner_id)
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.info("No state file found at %s — starting fresh", self._path)
            return

        try:
            data = json.loads(self._path.read_text())
            self._state.owner_id = data.get("owner_id", self._state.owner_id)
            for fqdn, rec_data in data.get("records", {}).items():
                # Handle state files without txt_provider_id
                rec_data.setdefault("txt_provider_id", None)
                self._state.records[fqdn] = ManagedRecord(**rec_data)
            logger.info("Loaded state: %d managed records", len(self._state.records))
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to load state file: %s — starting fresh", e)

    def rebuild_from_txt(
        self,
        txt_records: list[TxtRecord],
        a_records: list[DnsRecord],
        provider_name: str,
        prefix: str,
    ) -> int:
        """Rebuild state for a provider from TXT ownership records in DNS.

        This is the source of truth — called on startup to recover state
        even if the state file is missing or stale.

        Returns the number of records recovered.
        """
        # Build a lookup of A-records by FQDN for IP info
        a_by_fqdn: dict[str, str] = {r.fqdn: r.ip for r in a_records}
        recovered = 0

        for txt in txt_records:
            parsed = TxtRecord.parse_value(txt.value)
            if parsed.get("heritage") != "traefik-dns-sync":
                continue
            owner = parsed.get("traefik-dns-sync/owner", "")
            if owner != self._state.owner_id:
                logger.debug(
                    "Skipping TXT %s — owner %s != %s",
                    txt.fqdn, owner, self._state.owner_id,
                )
                continue

            a_fqdn = TxtRecord.extract_a_record_fqdn(txt.fqdn, prefix)
            if not a_fqdn:
                continue

            key = f"{a_fqdn}@{provider_name}"
            if key in self._state.records:
                # Already in state (from file cache), skip
                continue

            ip = a_by_fqdn.get(a_fqdn, "")
            if not ip:
                logger.warning(
                    "TXT record %s has no matching A record — skipping", txt.fqdn,
                )
                continue

            now = datetime.now(UTC).isoformat()
            self._state.records[key] = ManagedRecord(
                fqdn=a_fqdn,
                ip=ip,
                provider=provider_name,
                provider_id=None,  # will be resolved on next sync if needed
                txt_provider_id=None,
                created_at=now,
                updated_at=now,
            )
            recovered += 1

        if recovered:
            logger.info(
                "Recovered %d records from TXT ownership records for %s",
                recovered, provider_name,
            )
        return recovered

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "owner_id": self._state.owner_id,
            "records": {},
        }
        for fqdn, rec in self._state.records.items():
            data["records"][fqdn] = {
                "fqdn": rec.fqdn,
                "ip": rec.ip,
                "provider": rec.provider,
                "provider_id": rec.provider_id,
                "txt_provider_id": rec.txt_provider_id,
                "created_at": rec.created_at,
                "updated_at": rec.updated_at,
            }

        self._path.write_text(json.dumps(data, indent=2) + "\n")
        logger.debug("Saved state: %d records", len(self._state.records))

    def is_managed(self, record: DnsRecord, provider: str) -> bool:
        key = f"{record.fqdn}@{provider}"
        return key in self._state.records

    def get_managed(self, record: DnsRecord, provider: str) -> ManagedRecord | None:
        key = f"{record.fqdn}@{provider}"
        return self._state.records.get(key)

    def add(
        self,
        record: DnsRecord,
        provider: str,
        provider_id: str | None = None,
        txt_provider_id: str | None = None,
    ) -> None:
        key = f"{record.fqdn}@{provider}"
        now = datetime.now(UTC).isoformat()
        self._state.records[key] = ManagedRecord(
            fqdn=record.fqdn,
            ip=record.ip,
            provider=provider,
            provider_id=provider_id,
            txt_provider_id=txt_provider_id,
            created_at=now,
            updated_at=now,
        )

    def update(
        self,
        record: DnsRecord,
        provider: str,
        provider_id: str | None = None,
        txt_provider_id: str | None = None,
    ) -> None:
        key = f"{record.fqdn}@{provider}"
        existing = self._state.records.get(key)
        now = datetime.now(UTC).isoformat()
        self._state.records[key] = ManagedRecord(
            fqdn=record.fqdn,
            ip=record.ip,
            provider=provider,
            provider_id=provider_id or (existing.provider_id if existing else None),
            txt_provider_id=txt_provider_id or (existing.txt_provider_id if existing else None),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )

    def remove(self, record: DnsRecord, provider: str) -> None:
        key = f"{record.fqdn}@{provider}"
        self._state.records.pop(key, None)

    def get_stale(self, current_fqdns: set[str], provider: str) -> list[ManagedRecord]:
        """Find records in state that are no longer in the current set."""
        stale = []
        for key, rec in self._state.records.items():
            if rec.provider == provider and rec.fqdn not in current_fqdns:
                stale.append(rec)
        return stale

    @property
    def owner_id(self) -> str:
        return self._state.owner_id

    @property
    def record_count(self) -> int:
        return len(self._state.records)
