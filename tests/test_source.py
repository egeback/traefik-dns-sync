"""Tests for Traefik source — hostname extraction from labels."""

from traefik_dns_sync.source import extract_hosts_from_rule


class TestExtractHostsFromRule:
    def test_single_host(self):
        rule = "Host(`grafana.internal.example.se`)"
        assert extract_hosts_from_rule(rule) == ["grafana.internal.example.se"]

    def test_multiple_hosts_or(self):
        rule = "Host(`evcc.internal.example.se`) || Host(`evcc.internal.example.com`)"
        assert extract_hosts_from_rule(rule) == [
            "evcc.internal.example.se",
            "evcc.internal.example.com",
        ]

    def test_host_with_path(self):
        rule = "Host(`api.example.com`) && PathPrefix(`/v1`)"
        assert extract_hosts_from_rule(rule) == ["api.example.com"]

    def test_no_host(self):
        rule = "PathPrefix(`/api`)"
        assert extract_hosts_from_rule(rule) == []

    def test_empty_rule(self):
        assert extract_hosts_from_rule("") == []
