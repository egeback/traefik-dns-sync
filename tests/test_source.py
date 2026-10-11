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


class TestFileSource:
    def _source(self, directory):
        from traefik_dns_sync.config import TraefikConfig
        from traefik_dns_sync.source import FileSource

        return FileSource(TraefikConfig(file_dir=str(directory)))

    def test_yaml_and_toml_routers(self, tmp_path):
        (tmp_path / "homebridge.yml").write_text(
            "http:\n"
            "  routers:\n"
            "    homebridge:\n"
            "      rule: \"Host(`hb.example.se`) || Host(`hb.example.com`)\"\n"
            "      service: homebridge\n"
            "    homebridge-secure:\n"
            "      rule: \"Host(`hb.example.se`) || Host(`hb.example.com`)\"\n"
            "      service: homebridge\n"
        )
        (tmp_path / "matter.toml").write_text(
            "[http.routers.matter]\nrule = \"Host(`matter.example.se`)\"\nservice = \"matter\"\n"
        )
        routes = self._source(tmp_path).discover()
        assert [(r.router_name, r.hostnames) for r in routes] == [
            ("homebridge@file", ["hb.example.se", "hb.example.com"]),
            ("matter@file", ["matter.example.se"]),
        ]

    def test_ignores_middlewares_tcp_and_other_files(self, tmp_path):
        (tmp_path / "middlewares.yaml").write_text(
            "http:\n  middlewares:\n    redirect:\n      redirectScheme:\n        scheme: https\n"
        )
        (tmp_path / "mariadb.yaml").write_text(
            "tcp:\n  routers:\n    mariadb:\n      rule: \"HostSNI(`*`)\"\n"
        )
        (tmp_path / "empty.yaml").write_text("")
        (tmp_path / "notes.txt").write_text("Host(`nope.example.se`)")
        assert self._source(tmp_path).discover() == []

    def test_missing_directory_raises(self, tmp_path):
        import pytest

        with pytest.raises(FileNotFoundError):
            self._source(tmp_path / "nope").discover()

    def test_invalid_yaml_raises(self, tmp_path):
        import pytest
        import yaml

        (tmp_path / "broken.yml").write_text("http: [unclosed\n")
        with pytest.raises(yaml.YAMLError):
            self._source(tmp_path).discover()
