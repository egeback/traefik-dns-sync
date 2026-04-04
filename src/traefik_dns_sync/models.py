"""Data models for DNS records and sync state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# Default TXT record prefix (like external-dns uses _edns)
DEFAULT_TXT_PREFIX = "_tdns"


@dataclass(frozen=True)
class DnsRecord:
    """A DNS A-record to sync."""

    hostname: str
    ip: str
    domain: str | None = None

    @property
    def fqdn(self) -> str:
        if self.domain:
            return f"{self.hostname}.{self.domain}"
        return self.hostname

    @classmethod
    def from_fqdn(cls, fqdn: str, ip: str) -> DnsRecord:
        parts = fqdn.split(".", 1)
        if len(parts) == 2:
            return cls(hostname=parts[0], ip=ip, domain=parts[1])
        return cls(hostname=fqdn, ip=ip)


@dataclass(frozen=True)
class TxtRecord:
    """A TXT ownership record for tracking managed DNS entries."""

    fqdn: str
    value: str

    @staticmethod
    def make_fqdn(a_record_fqdn: str, prefix: str = DEFAULT_TXT_PREFIX) -> str:
        """Build TXT record FQDN from an A-record FQDN.

        Example: grafana.internal.example.com -> _tdns.a-grafana.internal.example.com
        """
        parts = a_record_fqdn.split(".", 1)
        if len(parts) == 2:
            return f"{prefix}.a-{parts[0]}.{parts[1]}"
        return f"{prefix}.a-{a_record_fqdn}"

    @staticmethod
    def make_value(owner_id: str, resource: str | None = None) -> str:
        """Build TXT record value.

        Example: "heritage=traefik-dns-sync,traefik-dns-sync/owner=docker-carl"
        """
        parts = [
            "heritage=traefik-dns-sync",
            f"traefik-dns-sync/owner={owner_id}",
        ]
        if resource:
            parts.append(f"traefik-dns-sync/resource={resource}")
        return ",".join(parts)

    @staticmethod
    def parse_value(value: str) -> dict[str, str]:
        """Parse a TXT record value into key-value pairs."""
        result: dict[str, str] = {}
        for part in value.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                result[k] = v
        return result

    @staticmethod
    def extract_a_record_fqdn(txt_fqdn: str, prefix: str = DEFAULT_TXT_PREFIX) -> str | None:
        """Extract the original A-record FQDN from a TXT record FQDN.

        Example: _tdns.a-grafana.internal.example.com -> grafana.internal.example.com
        """
        expected = f"{prefix}.a-"
        if not txt_fqdn.startswith(expected):
            return None
        return txt_fqdn[len(expected):]


@dataclass
class SyncState:
    """Tracks which DNS records are owned by this instance."""

    owner_id: str
    records: dict[str, ManagedRecord] = field(default_factory=dict)

    def key(self, record: DnsRecord) -> str:
        return record.fqdn


@dataclass
class ManagedRecord:
    """A DNS record managed by this instance."""

    fqdn: str
    ip: str
    provider: str
    provider_id: str | None = None
    txt_provider_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
