"""Configuration via environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class TraefikConfig(BaseSettings):
    """Traefik source configuration."""

    model_config = {"env_prefix": "TRAEFIK_"}

    url: str | None = Field(
        default=None, description="Traefik API URL",
    )
    use_docker: bool = Field(
        default=True, description="Read labels from Docker socket",
    )
    docker_host: str = Field(
        default="unix:///var/run/docker.sock",
        description="Docker socket path",
    )


class OpnsenseConfig(BaseSettings):
    """OPNsense Unbound provider configuration."""

    model_config = {"env_prefix": "OPNSENSE_"}

    enabled: bool = Field(default=False)
    host: str = Field(default="")
    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    skip_tls_verify: bool = Field(default=True)


class UnifiConfig(BaseSettings):
    """UniFi Gateway DNS provider configuration."""

    model_config = {"env_prefix": "UNIFI_"}

    enabled: bool = Field(default=False)
    host: str = Field(default="")
    api_key: str = Field(default="")
    skip_tls_verify: bool = Field(default=True)
    site: str = Field(default="default")


class Rfc2136Config(BaseSettings):
    """RFC 2136 Dynamic DNS Update provider configuration."""

    model_config = {"env_prefix": "RFC2136_"}

    enabled: bool = Field(default=False)
    host: str = Field(default="", description="DNS server address")
    port: int = Field(default=53, description="DNS server port")
    zone: str = Field(default="", description="DNS zone (e.g. internal.example.com)")
    tsig_key_name: str = Field(default="", description="TSIG key name")
    tsig_key_secret: str = Field(default="", description="TSIG key secret (base64)")
    tsig_key_algorithm: str = Field(
        default="hmac-sha256",
        description="TSIG algorithm (hmac-sha256, hmac-sha512, hmac-md5)",
    )


class SyncConfig(BaseSettings):
    """General sync configuration."""

    model_config = {"env_prefix": "SYNC_"}

    interval: int = Field(default=300, description="Sync interval in seconds")
    owner_id: str = Field(
        default="traefik-dns-sync",
        description="Owner ID for tracking managed records",
    )
    host_ip: str = Field(
        default="",
        description="IP address for A records",
    )
    domain_filters: list[str] = Field(
        default_factory=list,
        description="Only sync hostnames matching these domains",
    )
    state_file: str = Field(
        default="/data/state.json", description="Path to state file",
    )
    dry_run: bool = Field(
        default=False, description="Log changes without applying",
    )
    policy: str = Field(
        default="upsert-only",
        description="upsert-only or sync (deletes stale records)",
    )
    txt_prefix: str = Field(
        default="_tdns",
        description="Prefix for TXT ownership records (like _edns in external-dns)",
    )
    adopt_existing: bool = Field(
        default=False,
        description="Adopt existing DNS records by adding TXT ownership (default: skip them)",
    )
    health_port: int = Field(
        default=8080,
        description="Port for health endpoint (0 to disable)",
    )


class AppConfig:
    """Combined application configuration."""

    def __init__(self) -> None:
        self.traefik = TraefikConfig()
        self.opnsense = OpnsenseConfig()
        self.unifi = UnifiConfig()
        self.rfc2136 = Rfc2136Config()
        self.sync = SyncConfig()

    @property
    def enabled_providers(self) -> list[str]:
        providers = []
        if self.opnsense.enabled:
            providers.append("opnsense")
        if self.unifi.enabled:
            providers.append("unifi")
        if self.rfc2136.enabled:
            providers.append("rfc2136")
        return providers
