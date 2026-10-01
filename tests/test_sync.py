"""Tests for sync engine."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from traefik_dns_sync.config import AppConfig
from traefik_dns_sync.models import DnsRecord, TxtRecord
from traefik_dns_sync.source import TraefikRoute
from traefik_dns_sync.state import StateTracker
from traefik_dns_sync.sync import SyncEngine


class FakeProvider:
    def __init__(self, name: str = "fake", supports_txt: bool = True):
        self._name = name
        self.supports_txt = supports_txt
        self.created: list[DnsRecord] = []
        self.records: list[DnsRecord] = []  # current state on the "provider"
        self.updated: list[DnsRecord] = []
        self.deleted: list[DnsRecord] = []
        self.txt_created: list[TxtRecord] = []
        self.txt_deleted: list[TxtRecord] = []
        self.txt_records: list[TxtRecord] = []

    @property
    def name(self) -> str:
        return self._name

    async def get_records(self, domain_filter=None):
        # Like a real provider: what was created (and not deleted) is listed
        return list(self.records)

    async def create_record(self, record):
        self.created.append(record)
        self.records.append(record)
        return f"id-{record.fqdn}"

    async def update_record(self, record, provider_id=None):
        self.updated.append(record)

    async def delete_record(self, record, provider_id=None):
        self.deleted.append(record)
        self.records = [r for r in self.records if r.fqdn != record.fqdn]

    async def create_txt_record(self, record):
        self.txt_created.append(record)
        return f"txt-id-{record.fqdn}"

    async def delete_txt_record(self, record, provider_id=None):
        self.txt_deleted.append(record)

    async def get_txt_records(self, prefix, domain_filter=None):
        return self.txt_records


def make_engine(provider: FakeProvider, tmpdir: str, **env_overrides) -> SyncEngine:
    import os

    env = {
        "SYNC_HOST_IP": "192.168.1.10",
        "SYNC_STATE_FILE": str(Path(tmpdir) / "state.json"),
        "SYNC_DRY_RUN": "false",
        "TRAEFIK_USE_DOCKER": "true",
        **env_overrides,
    }
    with patch.dict(os.environ, env, clear=False):
        config = AppConfig()
    state = StateTracker(config.sync.state_file, config.sync.owner_id)
    return SyncEngine(config=config, providers=[provider], state=state)


@pytest.fixture
def routes():
    return [
        TraefikRoute(
            router_name="grafana",
            hostnames=["grafana.internal.example.se", "grafana.internal.example.com"],
        ),
        TraefikRoute(
            router_name="evcc",
            hostnames=["evcc.internal.example.se"],
        ),
    ]


class TestSyncEngine:
    @pytest.mark.asyncio
    async def test_creates_new_records(self, routes):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("opnsense", supports_txt=False)
            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 3
            assert result.unchanged == 0
            fqdns = {r.fqdn for r in provider.created}
            assert "grafana.internal.example.se" in fqdns
            assert "grafana.internal.example.com" in fqdns
            assert "evcc.internal.example.se" in fqdns

    @pytest.mark.asyncio
    async def test_unchanged_on_second_sync(self, routes):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("opnsense", supports_txt=False)
            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                await engine.sync()
                provider.created.clear()
                result = await engine.sync()

            assert result.created == 0
            assert result.unchanged == 3

    @pytest.mark.asyncio
    async def test_dry_run(self, routes):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("opnsense", supports_txt=False)
            engine = make_engine(provider, tmpdir, SYNC_DRY_RUN="true")

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 3
            assert len(provider.created) == 0  # nothing actually created

    @pytest.mark.asyncio
    async def test_filters_domains(self):
        routes = [
            TraefikRoute(router_name="ext", hostnames=["app.example.org"]),
            TraefikRoute(router_name="int", hostnames=["app.internal.example.se"]),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("opnsense", supports_txt=False)
            engine = make_engine(
                provider, tmpdir,
                SYNC_DOMAIN_FILTERS='["internal.example.se", "internal.example.com"]',
            )

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 1
            assert provider.created[0].fqdn == "app.internal.example.se"

    @pytest.mark.asyncio
    async def test_deletes_stale_with_sync_policy(self, routes):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("opnsense", supports_txt=False)
            engine = make_engine(provider, tmpdir, SYNC_POLICY="sync")

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                await engine.sync()

            # Remove evcc from routes
            reduced = [r for r in routes if r.router_name != "evcc"]
            with patch.object(engine, "_discover_routes_async", return_value=reduced):
                result = await engine.sync()

            assert result.deleted == 1
            assert provider.deleted[0].fqdn == "evcc.internal.example.se"


class TestSyncEngineTxt:
    """Tests for TXT ownership record integration."""

    @pytest.mark.asyncio
    async def test_creates_txt_alongside_a_record(self, routes):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")
            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 3
            assert len(provider.txt_created) == 3
            txt_fqdns = {t.fqdn for t in provider.txt_created}
            assert "_tdns.a-grafana.internal.example.se" in txt_fqdns
            assert "_tdns.a-grafana.internal.example.com" in txt_fqdns
            assert "_tdns.a-evcc.internal.example.se" in txt_fqdns

            # Verify TXT value format
            for txt in provider.txt_created:
                assert "heritage=traefik-dns-sync" in txt.value
                assert "traefik-dns-sync/owner=traefik-dns-sync" in txt.value

    @pytest.mark.asyncio
    async def test_deletes_txt_alongside_a_record(self, routes):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")
            engine = make_engine(provider, tmpdir, SYNC_POLICY="sync")

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                await engine.sync()

            reduced = [r for r in routes if r.router_name != "evcc"]
            with patch.object(engine, "_discover_routes_async", return_value=reduced):
                result = await engine.sync()

            assert result.deleted == 1
            assert len(provider.txt_deleted) == 1
            assert provider.txt_deleted[0].fqdn == "_tdns.a-evcc.internal.example.se"

    @pytest.mark.asyncio
    async def test_no_txt_for_provider_without_support(self, routes):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("opnsense", supports_txt=False)
            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 3
            assert len(provider.txt_created) == 0

    @pytest.mark.asyncio
    async def test_dry_run_logs_txt_operations(self, routes):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")
            engine = make_engine(provider, tmpdir, SYNC_DRY_RUN="true")

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 3
            assert len(provider.created) == 0
            assert len(provider.txt_created) == 0

    @pytest.mark.asyncio
    async def test_rebuilds_state_from_txt_on_startup(self):
        """Simulate a fresh container with no state file but TXT records in DNS."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")
            # Pre-populate TXT records as if they exist in DNS
            provider.txt_records = [
                TxtRecord(
                    fqdn="_tdns.a-grafana.internal.example.se",
                    value="heritage=traefik-dns-sync,traefik-dns-sync/owner=traefik-dns-sync",
                ),
            ]
            # Simulate A record also existing
            async def get_records_with_existing(domain_filter=None):
                return [DnsRecord.from_fqdn("grafana.internal.example.se", "192.168.1.10")]

            provider.get_records = get_records_with_existing

            routes = [
                TraefikRoute(
                    router_name="grafana",
                    hostnames=["grafana.internal.example.se"],
                ),
            ]

            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            # Should recognize the record as managed (from TXT) and not re-create it
            assert result.unchanged == 1
            assert result.created == 0

    @pytest.mark.asyncio
    async def test_skips_existing_record_without_ownership(self):
        """Record exists in DNS but has no TXT — should not be touched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")

            # A record exists in provider but no TXT ownership
            async def get_records_with_existing(domain_filter=None):
                return [DnsRecord.from_fqdn("amp.internal.example.se", "192.168.1.10")]

            provider.get_records = get_records_with_existing

            routes = [
                TraefikRoute(
                    router_name="amp",
                    hostnames=["amp.internal.example.se"],
                ),
            ]

            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            # Should skip — record exists but not ours
            assert result.unchanged == 1
            assert result.created == 0
            assert len(provider.created) == 0
            assert len(provider.txt_created) == 0

    @pytest.mark.asyncio
    async def test_adopts_existing_record_when_enabled(self):
        """With SYNC_ADOPT_EXISTING=true, adopt record by adding TXT ownership."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")

            async def get_records_with_existing(domain_filter=None):
                return [DnsRecord.from_fqdn("amp.internal.example.se", "192.168.1.10")]

            provider.get_records = get_records_with_existing

            routes = [
                TraefikRoute(
                    router_name="amp",
                    hostnames=["amp.internal.example.se"],
                ),
            ]

            engine = make_engine(provider, tmpdir, SYNC_ADOPT_EXISTING="true")

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            # Should adopt — create TXT but NOT re-create A record
            assert result.created == 1
            assert len(provider.created) == 0  # A record already exists
            assert len(provider.txt_created) == 1
            assert provider.txt_created[0].fqdn == "_tdns.a-amp.internal.example.se"
            assert len(provider.updated) == 0  # already points at this host

    @pytest.mark.asyncio
    async def test_adopt_updates_ip_when_record_points_elsewhere(self):
        """An adopted record pointing at another host (app moved here) gets this host's IP."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")

            async def get_records_elsewhere(domain_filter=None):
                return [DnsRecord.from_fqdn("amp.internal.example.se", "192.168.1.99")]

            provider.get_records = get_records_elsewhere
            routes = [TraefikRoute(router_name="amp", hostnames=["amp.internal.example.se"])]
            engine = make_engine(provider, tmpdir, SYNC_ADOPT_EXISTING="true")

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 1
            assert len(provider.created) == 0
            assert len(provider.txt_created) == 1
            assert [r.ip for r in provider.updated] == ["192.168.1.10"]

    @pytest.mark.asyncio
    async def test_managed_record_drift_is_corrected(self):
        """A managed record whose provider value drifted (e.g. adopted by v1.0.2) is fixed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")
            routes = [TraefikRoute(router_name="amp", hostnames=["amp.internal.example.se"])]
            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                await engine.sync()  # creates and manages the record

            async def get_records_drifted(domain_filter=None):
                return [DnsRecord.from_fqdn("amp.internal.example.se", "192.168.1.99")]

            provider.get_records = get_records_drifted
            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.updated == 1
            assert [r.ip for r in provider.updated] == ["192.168.1.10"]

    @pytest.mark.asyncio
    async def test_missing_managed_record_is_recreated(self):
        """A managed record deleted from the provider (e.g. by the instance that owned it before
        the app moved) is recreated; TXT only if it is gone too."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")
            routes = [TraefikRoute(router_name="amp", hostnames=["amp.internal.example.se"])]
            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                await engine.sync()  # creates A + TXT and manages them
            assert len(provider.created) == 1

            async def get_records_gone(domain_filter=None):
                return []

            provider.get_records = get_records_gone
            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 1
            assert len(provider.created) == 2
            assert provider.created[-1].ip == "192.168.1.10"

    @pytest.mark.asyncio
    async def test_missing_record_not_recreated_when_fetch_fails(self):
        """If listing the provider fails, nothing is assumed missing (no duplicate creates)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")
            routes = [TraefikRoute(router_name="amp", hostnames=["amp.internal.example.se"])]
            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                await engine.sync()

            async def get_records_fail(domain_filter=None):
                raise RuntimeError("gateway down")

            provider.get_records = get_records_fail
            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert len(provider.created) == 1
            assert result.created == 0

    @pytest.mark.asyncio
    async def test_duplicate_records_are_reconciled(self):
        """A managed name with an extra A record pointing elsewhere is treated as drift."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")
            routes = [TraefikRoute(router_name="amp", hostnames=["amp.internal.example.se"])]
            engine = make_engine(provider, tmpdir)

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                await engine.sync()

            async def get_records_duplicated(domain_filter=None):
                return [
                    DnsRecord.from_fqdn("amp.internal.example.se", "192.168.1.99"),
                    DnsRecord.from_fqdn("amp.internal.example.se", "192.168.1.10"),
                ]

            provider.get_records = get_records_duplicated
            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.updated == 1

    @pytest.mark.asyncio
    async def test_adopt_dry_run(self):
        """Dry-run with adopt should log but not create."""
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider("unifi")

            async def get_records_with_existing(domain_filter=None):
                return [DnsRecord.from_fqdn("amp.internal.example.se", "192.168.1.10")]

            provider.get_records = get_records_with_existing

            routes = [
                TraefikRoute(
                    router_name="amp",
                    hostnames=["amp.internal.example.se"],
                ),
            ]

            engine = make_engine(
                provider, tmpdir, SYNC_ADOPT_EXISTING="true", SYNC_DRY_RUN="true",
            )

            with patch.object(engine, "_discover_routes_async", return_value=routes):
                result = await engine.sync()

            assert result.created == 1
            assert len(provider.created) == 0
            assert len(provider.txt_created) == 0
