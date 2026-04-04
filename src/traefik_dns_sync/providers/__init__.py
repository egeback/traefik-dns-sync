"""DNS providers."""

from __future__ import annotations

from typing import Protocol

from traefik_dns_sync.models import DnsRecord, TxtRecord


class DnsProvider(Protocol):
    """Protocol for DNS providers."""

    @property
    def name(self) -> str: ...

    async def get_records(self, domain_filter: list[str] | None = None) -> list[DnsRecord]: ...

    async def create_record(self, record: DnsRecord) -> str | None:
        """Create a DNS record. Returns provider-specific ID if available."""
        ...

    async def update_record(self, record: DnsRecord, provider_id: str | None = None) -> None: ...

    async def delete_record(self, record: DnsRecord, provider_id: str | None = None) -> None: ...

    async def create_txt_record(self, record: TxtRecord) -> str | None:
        """Create a TXT ownership record. Returns provider-specific ID if available."""
        ...

    async def delete_txt_record(self, record: TxtRecord, provider_id: str | None = None) -> None:
        """Delete a TXT ownership record."""
        ...

    async def get_txt_records(
        self, prefix: str, domain_filter: list[str] | None = None,
    ) -> list[TxtRecord]:
        """Fetch all TXT ownership records matching the prefix."""
        ...
