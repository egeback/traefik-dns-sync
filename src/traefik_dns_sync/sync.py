"""Core sync engine — reconciles Traefik routes with DNS providers."""

from __future__ import annotations

import logging

from traefik_dns_sync.config import AppConfig
from traefik_dns_sync.models import DnsRecord, TxtRecord
from traefik_dns_sync.providers import DnsProvider
from traefik_dns_sync.source import DockerSource, TraefikApiSource, TraefikRoute
from traefik_dns_sync.state import StateTracker

logger = logging.getLogger(__name__)


class SyncEngine:
    """Reconcile Traefik routes with DNS providers."""

    def __init__(
        self,
        config: AppConfig,
        providers: list[DnsProvider],
        state: StateTracker,
    ) -> None:
        self._config = config
        self._providers = providers
        self._state = state
        self._initialized = False

    def _discover_routes(self) -> list[TraefikRoute]:
        """Discover Traefik routes from configured source."""
        if self._config.traefik.use_docker:
            source = DockerSource(self._config.traefik)
            return source.discover()
        else:
            raise ValueError(
                "Traefik API source requires async — use discover_routes_async instead"
            )

    async def _discover_routes_async(self) -> list[TraefikRoute]:
        """Discover Traefik routes from Traefik API."""
        if not self._config.traefik.use_docker and self._config.traefik.url:
            source = TraefikApiSource(self._config.traefik)
            return await source.discover()
        return self._discover_routes()

    def _routes_to_records(self, routes: list[TraefikRoute]) -> list[DnsRecord]:
        """Convert Traefik routes to DNS records, applying domain filters."""
        ip = self._config.sync.host_ip
        if not ip:
            raise ValueError("SYNC_HOST_IP must be set — cannot auto-detect IP in container")

        records: list[DnsRecord] = []
        seen: set[str] = set()

        for route in routes:
            for hostname in route.hostnames:
                if hostname in seen:
                    continue

                # Apply domain filter
                if self._config.sync.domain_filters:
                    if not any(
                        hostname.endswith(f".{d}") or hostname == d
                        for d in self._config.sync.domain_filters
                    ):
                        logger.debug("Skipping %s — not in domain filter", hostname)
                        continue

                seen.add(hostname)
                records.append(DnsRecord.from_fqdn(hostname, ip))

        logger.info("Resolved %d DNS records from %d routes", len(records), len(routes))
        return records

    async def _initialize_state(self) -> None:
        """Rebuild state from TXT ownership records on first sync.

        This ensures we recover ownership even if the state file is missing.
        """
        if self._initialized:
            return
        self._initialized = True

        prefix = self._config.sync.txt_prefix
        domain_filter = self._config.sync.domain_filters or None

        for provider in self._providers:
            if not self._provider_supports_txt(provider):
                logger.info(
                    "Provider %s does not support TXT — skipping state rebuild",
                    provider.name,
                )
                continue

            try:
                txt_records = await provider.get_txt_records(prefix, domain_filter)
                a_records = await provider.get_records(domain_filter)
                recovered = self._state.rebuild_from_txt(
                    txt_records, a_records, provider.name, prefix,
                )
                if recovered:
                    self._state.save()
                logger.info(
                    "State init for %s: %d TXT records found, %d recovered",
                    provider.name, len(txt_records), recovered,
                )
            except Exception:
                logger.exception(
                    "Failed to rebuild state from TXT for %s — continuing with file state",
                    provider.name,
                )

    @staticmethod
    def _provider_supports_txt(provider: DnsProvider) -> bool:
        return getattr(provider, "supports_txt", True)

    async def sync(self) -> SyncResult:
        """Run a single sync cycle."""
        await self._initialize_state()

        routes = await self._discover_routes_async()
        desired_records = self._routes_to_records(routes)
        current_fqdns = {r.fqdn for r in desired_records}

        result = SyncResult()

        for provider in self._providers:
            await self._sync_provider(provider, desired_records, current_fqdns, result)

        self._state.save()
        return result

    async def _sync_provider(
        self,
        provider: DnsProvider,
        desired: list[DnsRecord],
        current_fqdns: set[str],
        result: SyncResult,
    ) -> None:
        """Sync records to a single provider."""
        provider_name = provider.name
        supports_txt = self._provider_supports_txt(provider)
        prefix = self._config.sync.txt_prefix
        owner_id = self._state.owner_id
        logger.info("Syncing %d records to %s", len(desired), provider_name)

        # Fetch existing records from provider to detect conflicts
        try:
            existing = await provider.get_records(self._config.sync.domain_filters or None)
        except Exception:
            logger.exception("Failed to fetch existing records from %s", provider_name)
            existing = []
        existing_fqdns = {r.fqdn for r in existing}
        # Actual IPs on the provider (a name can have several A records), to catch adopted
        # records pointing elsewhere, manual drift and duplicates
        existing_ips: dict[str, set[str]] = {}
        for r in existing:
            existing_ips.setdefault(r.fqdn, set()).add(r.ip)

        for record in desired:
            managed = self._state.get_managed(record, provider_name)

            if managed is None:
                # Check if record already exists in provider but not in our state
                if record.fqdn in existing_fqdns:
                    if not self._config.sync.adopt_existing:
                        logger.info(
                            "Skipping %s — already exists on %s without ownership"
                            " (set SYNC_ADOPT_EXISTING=true to take over)",
                            record.fqdn, provider_name,
                        )
                        result.unchanged += 1
                        continue

                    # Adopt: add TXT ownership to existing record
                    if self._config.sync.dry_run:
                        logger.info(
                            "[DRY RUN] Would adopt %s on %s (add TXT ownership)",
                            record.fqdn, provider_name,
                        )
                        if supports_txt:
                            txt_fqdn = TxtRecord.make_fqdn(record.fqdn, prefix)
                            logger.info(
                                "[DRY RUN] Would create TXT %s on %s",
                                txt_fqdn, provider_name,
                            )
                        result.created += 1
                        continue

                    try:
                        txt_provider_id = None
                        if supports_txt:
                            txt = TxtRecord(
                                fqdn=TxtRecord.make_fqdn(record.fqdn, prefix),
                                value=TxtRecord.make_value(owner_id),
                            )
                            txt_provider_id = await provider.create_txt_record(txt)
                        # The adopted record may point at another host (e.g. the app moved here)
                        found = existing_ips.get(record.fqdn, {record.ip})
                        if found != {record.ip}:
                            await provider.update_record(record, None)
                            logger.info(
                                "Adopted %s on %s: %s -> %s",
                                record.fqdn, provider_name,
                                ", ".join(sorted(found)), record.ip,
                            )
                        self._state.add(record, provider_name, None, txt_provider_id)
                        result.created += 1
                        logger.info(
                            "Adopted %s on %s (TXT ownership added)",
                            record.fqdn, provider_name,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to adopt %s on %s", record.fqdn, provider_name,
                        )
                        result.errors += 1
                    continue

                # New record — create it
                if self._config.sync.dry_run:
                    logger.info(
                        "[DRY RUN] Would create %s -> %s on %s",
                        record.fqdn, record.ip, provider_name,
                    )
                    if supports_txt:
                        txt_fqdn = TxtRecord.make_fqdn(record.fqdn, prefix)
                        logger.info(
                            "[DRY RUN] Would create TXT %s on %s", txt_fqdn, provider_name,
                        )
                    result.created += 1
                    continue

                try:
                    provider_id = await provider.create_record(record)
                except Exception:
                    logger.exception("Failed to create %s on %s", record.fqdn, provider_name)
                    result.errors += 1
                    continue

                # Create TXT ownership record (separate try — A record already exists)
                txt_provider_id = None
                if supports_txt:
                    try:
                        txt = TxtRecord(
                            fqdn=TxtRecord.make_fqdn(record.fqdn, prefix),
                            value=TxtRecord.make_value(owner_id),
                        )
                        txt_provider_id = await provider.create_txt_record(txt)
                    except Exception:
                        logger.exception(
                            "Failed to create TXT for %s on %s — A record was created",
                            record.fqdn, provider_name,
                        )

                self._state.add(record, provider_name, provider_id, txt_provider_id)
                result.created += 1

            elif managed.ip != record.ip or existing_ips.get(record.fqdn, {record.ip}) != {
                record.ip
            }:
                # IP changed here, or the record on the provider drifted (or was duplicated)
                current_ip = ", ".join(sorted(existing_ips.get(record.fqdn, {managed.ip})))
                if self._config.sync.dry_run:
                    logger.info(
                        "[DRY RUN] Would update %s: %s -> %s on %s",
                        record.fqdn, current_ip, record.ip, provider_name,
                    )
                    result.updated += 1
                    continue

                try:
                    await provider.update_record(record, managed.provider_id)
                    self._state.update(record, provider_name, managed.provider_id)
                    result.updated += 1
                except Exception:
                    logger.exception("Failed to update %s on %s", record.fqdn, provider_name)
                    result.errors += 1
            else:
                result.unchanged += 1

        # Handle stale records (only if policy is "sync")
        if self._config.sync.policy == "sync":
            stale = self._state.get_stale(current_fqdns, provider_name)
            for managed in stale:
                if self._config.sync.dry_run:
                    logger.info(
                        "[DRY RUN] Would delete stale %s from %s",
                        managed.fqdn, provider_name,
                    )
                    if supports_txt:
                        txt_fqdn = TxtRecord.make_fqdn(managed.fqdn, prefix)
                        logger.info(
                            "[DRY RUN] Would delete TXT %s from %s", txt_fqdn, provider_name,
                        )
                    result.deleted += 1
                    continue

                try:
                    stale_record = DnsRecord.from_fqdn(managed.fqdn, managed.ip)
                    await provider.delete_record(stale_record, managed.provider_id)

                    # Delete TXT ownership record
                    if supports_txt:
                        txt = TxtRecord(
                            fqdn=TxtRecord.make_fqdn(managed.fqdn, prefix),
                            value="",
                        )
                        await provider.delete_txt_record(txt, managed.txt_provider_id)

                    self._state.remove(stale_record, provider_name)
                    result.deleted += 1
                except Exception:
                    logger.exception(
                        "Failed to delete stale %s from %s",
                        managed.fqdn, provider_name,
                    )
                    result.errors += 1


class SyncResult:
    """Result of a sync cycle."""

    def __init__(self) -> None:
        self.created: int = 0
        self.updated: int = 0
        self.deleted: int = 0
        self.unchanged: int = 0
        self.errors: int = 0

    def __str__(self) -> str:
        return (
            f"created={self.created} updated={self.updated} deleted={self.deleted} "
            f"unchanged={self.unchanged} errors={self.errors}"
        )
