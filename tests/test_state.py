"""Tests for state tracker."""

import tempfile
from pathlib import Path

from traefik_dns_sync.models import DnsRecord, TxtRecord
from traefik_dns_sync.state import StateTracker


class TestStateTracker:
    def test_add_and_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "test-owner")

            record = DnsRecord.from_fqdn("grafana.internal.example.se", "192.168.1.10")
            assert not tracker.is_managed(record, "opnsense")

            tracker.add(record, "opnsense", "uuid-123")
            assert tracker.is_managed(record, "opnsense")
            assert not tracker.is_managed(record, "unifi")

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")

            tracker = StateTracker(path, "test-owner")
            record = DnsRecord.from_fqdn("test.example.com", "1.2.3.4")
            tracker.add(record, "opnsense", "uuid-456")
            tracker.save()

            # Reload
            tracker2 = StateTracker(path, "test-owner")
            assert tracker2.is_managed(record, "opnsense")
            managed = tracker2.get_managed(record, "opnsense")
            assert managed is not None
            assert managed.provider_id == "uuid-456"

    def test_persistence_with_txt_provider_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")

            tracker = StateTracker(path, "test-owner")
            record = DnsRecord.from_fqdn("test.example.com", "1.2.3.4")
            tracker.add(record, "unifi", "id-123", txt_provider_id="txt-id-456")
            tracker.save()

            # Reload
            tracker2 = StateTracker(path, "test-owner")
            managed = tracker2.get_managed(record, "unifi")
            assert managed is not None
            assert managed.provider_id == "id-123"
            assert managed.txt_provider_id == "txt-id-456"

    def test_stale_detection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "test-owner")

            r1 = DnsRecord.from_fqdn("a.example.com", "1.1.1.1")
            r2 = DnsRecord.from_fqdn("b.example.com", "2.2.2.2")
            tracker.add(r1, "opnsense")
            tracker.add(r2, "opnsense")

            # b.example.com is no longer in Traefik
            stale = tracker.get_stale({"a.example.com"}, "opnsense")
            assert len(stale) == 1
            assert stale[0].fqdn == "b.example.com"

    def test_remove(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "test-owner")

            record = DnsRecord.from_fqdn("test.example.com", "1.2.3.4")
            tracker.add(record, "unifi", "id-789")
            assert tracker.is_managed(record, "unifi")

            tracker.remove(record, "unifi")
            assert not tracker.is_managed(record, "unifi")

    def test_update_ip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "test-owner")

            record = DnsRecord.from_fqdn("test.example.com", "1.2.3.4")
            tracker.add(record, "opnsense", "uuid-1")

            new_record = DnsRecord.from_fqdn("test.example.com", "5.6.7.8")
            tracker.update(new_record, "opnsense")

            managed = tracker.get_managed(new_record, "opnsense")
            assert managed is not None
            assert managed.ip == "5.6.7.8"
            assert managed.provider_id == "uuid-1"  # preserved

    def test_corrupt_state_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("not valid json{{{")

            tracker = StateTracker(str(path), "test-owner")
            assert tracker.record_count == 0  # starts fresh


class TestStateRebuildFromTxt:
    """Tests for rebuilding state from TXT ownership records."""

    def test_rebuild_recovers_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "my-owner")

            txt_records = [
                TxtRecord(
                    fqdn="_tdns.a-grafana.internal.example.se",
                    value="heritage=traefik-dns-sync,traefik-dns-sync/owner=my-owner",
                ),
            ]
            a_records = [
                DnsRecord.from_fqdn("grafana.internal.example.se", "192.168.1.10"),
            ]

            recovered = tracker.rebuild_from_txt(txt_records, a_records, "unifi", "_tdns")
            assert recovered == 1

            record = DnsRecord.from_fqdn("grafana.internal.example.se", "192.168.1.10")
            assert tracker.is_managed(record, "unifi")

    def test_rebuild_ignores_other_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "my-owner")

            txt_records = [
                TxtRecord(
                    fqdn="_tdns.a-grafana.internal.example.se",
                    value="heritage=traefik-dns-sync,traefik-dns-sync/owner=other-owner",
                ),
            ]
            a_records = [
                DnsRecord.from_fqdn("grafana.internal.example.se", "192.168.1.10"),
            ]

            recovered = tracker.rebuild_from_txt(txt_records, a_records, "unifi", "_tdns")
            assert recovered == 0

    def test_rebuild_ignores_non_heritage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "my-owner")

            txt_records = [
                TxtRecord(
                    fqdn="_tdns.a-grafana.internal.example.se",
                    value="heritage=external-dns,external-dns/owner=k8s",
                ),
            ]
            a_records = [
                DnsRecord.from_fqdn("grafana.internal.example.se", "192.168.1.10"),
            ]

            recovered = tracker.rebuild_from_txt(txt_records, a_records, "unifi", "_tdns")
            assert recovered == 0

    def test_rebuild_skips_already_in_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "my-owner")

            # Pre-populate state
            record = DnsRecord.from_fqdn("grafana.internal.example.se", "192.168.1.10")
            tracker.add(record, "unifi", "existing-id")

            txt_records = [
                TxtRecord(
                    fqdn="_tdns.a-grafana.internal.example.se",
                    value="heritage=traefik-dns-sync,traefik-dns-sync/owner=my-owner",
                ),
            ]
            a_records = [record]

            recovered = tracker.rebuild_from_txt(txt_records, a_records, "unifi", "_tdns")
            assert recovered == 0  # already known

    def test_rebuild_skips_txt_without_a_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            tracker = StateTracker(path, "my-owner")

            txt_records = [
                TxtRecord(
                    fqdn="_tdns.a-orphan.internal.example.se",
                    value="heritage=traefik-dns-sync,traefik-dns-sync/owner=my-owner",
                ),
            ]
            a_records = []  # no matching A record

            recovered = tracker.rebuild_from_txt(txt_records, a_records, "unifi", "_tdns")
            assert recovered == 0
