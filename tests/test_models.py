"""Tests for data models."""

from traefik_dns_sync.models import DnsRecord, TxtRecord


class TestDnsRecord:
    def test_fqdn_with_domain(self):
        r = DnsRecord(hostname="grafana", ip="192.168.1.10", domain="internal.example.se")
        assert r.fqdn == "grafana.internal.example.se"

    def test_fqdn_without_domain(self):
        r = DnsRecord(hostname="grafana.internal.example.se", ip="192.168.1.10")
        assert r.fqdn == "grafana.internal.example.se"

    def test_from_fqdn(self):
        r = DnsRecord.from_fqdn("grafana.internal.example.se", "192.168.1.10")
        assert r.hostname == "grafana"
        assert r.domain == "internal.example.se"
        assert r.ip == "192.168.1.10"

    def test_from_fqdn_no_domain(self):
        r = DnsRecord.from_fqdn("localhost", "127.0.0.1")
        assert r.hostname == "localhost"
        assert r.domain is None

    def test_frozen(self):
        r = DnsRecord(hostname="test", ip="1.2.3.4")
        try:
            r.hostname = "other"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass


class TestTxtRecord:
    def test_make_fqdn(self):
        result = TxtRecord.make_fqdn("grafana.internal.example.se")
        assert result == "_tdns.a-grafana.internal.example.se"

    def test_make_fqdn_custom_prefix(self):
        result = TxtRecord.make_fqdn("app.example.com", prefix="_edns")
        assert result == "_edns.a-app.example.com"

    def test_make_fqdn_no_domain(self):
        result = TxtRecord.make_fqdn("localhost")
        assert result == "_tdns.a-localhost"

    def test_make_value(self):
        value = TxtRecord.make_value("docker-carl")
        assert value == "heritage=traefik-dns-sync,traefik-dns-sync/owner=docker-carl"

    def test_make_value_with_resource(self):
        value = TxtRecord.make_value("docker-carl", resource="container/grafana")
        assert "traefik-dns-sync/resource=container/grafana" in value

    def test_parse_value(self):
        value = "heritage=traefik-dns-sync,traefik-dns-sync/owner=docker-carl"
        parsed = TxtRecord.parse_value(value)
        assert parsed["heritage"] == "traefik-dns-sync"
        assert parsed["traefik-dns-sync/owner"] == "docker-carl"

    def test_extract_a_record_fqdn(self):
        result = TxtRecord.extract_a_record_fqdn("_tdns.a-grafana.internal.example.se")
        assert result == "grafana.internal.example.se"

    def test_extract_a_record_fqdn_no_match(self):
        result = TxtRecord.extract_a_record_fqdn("grafana.internal.example.se")
        assert result is None

    def test_extract_a_record_fqdn_custom_prefix(self):
        result = TxtRecord.extract_a_record_fqdn("_edns.a-app.example.com", prefix="_edns")
        assert result == "app.example.com"

    def test_roundtrip_fqdn(self):
        """make_fqdn -> extract_a_record_fqdn should return original."""
        original = "prometheus.internal.example.com"
        txt_fqdn = TxtRecord.make_fqdn(original)
        extracted = TxtRecord.extract_a_record_fqdn(txt_fqdn)
        assert extracted == original
