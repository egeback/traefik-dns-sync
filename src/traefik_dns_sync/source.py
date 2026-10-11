"""Traefik source — reads hostnames from Docker labels or Traefik API."""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import docker
import httpx
import yaml

from traefik_dns_sync.config import TraefikConfig

logger = logging.getLogger(__name__)

HOST_PATTERN = re.compile(r"Host\(`([^`]+)`\)")


@dataclass(frozen=True)
class TraefikRoute:
    """A discovered Traefik route with its hostnames."""

    router_name: str
    hostnames: list[str]
    service_name: str | None = None


def extract_hosts_from_rule(rule: str) -> list[str]:
    """Extract hostnames from a Traefik routing rule."""
    return HOST_PATTERN.findall(rule)


class DockerSource:
    """Read Traefik routes from Docker container labels."""

    def __init__(self, config: TraefikConfig) -> None:
        self._config = config

    def discover(self) -> list[TraefikRoute]:
        """Discover all Traefik routes from running containers."""
        client = docker.DockerClient(base_url=self._config.docker_host)
        routes: list[TraefikRoute] = []

        try:
            containers = client.containers.list()
            for container in containers:
                routes.extend(self._parse_container(container))
        finally:
            client.close()

        logger.info("Discovered %d Traefik routes from Docker labels", len(routes))
        return routes

    def _parse_container(self, container: docker.models.containers.Container) -> list[TraefikRoute]:
        """Parse Traefik labels from a single container."""
        labels = container.labels or {}

        if labels.get("traefik.enable", "false").lower() != "true":
            return []

        routers: dict[str, dict[str, str]] = {}
        for key, value in labels.items():
            # Match: traefik.http.routers.<name>.<property>
            match = re.match(r"traefik\.http\.routers\.([^.]+)\.(.+)", key)
            if match:
                router_name = match.group(1)
                prop = match.group(2)
                routers.setdefault(router_name, {})[prop] = value

        routes: list[TraefikRoute] = []
        seen_hosts: set[str] = set()

        for name, props in routers.items():
            rule = props.get("rule", "")
            hostnames = extract_hosts_from_rule(rule)
            if not hostnames:
                continue

            # Deduplicate — HTTP and HTTPS routers often share the same hosts
            hosts_key = tuple(sorted(hostnames))
            if hosts_key in seen_hosts:
                continue
            seen_hosts.add(hosts_key)

            service = props.get("service")
            routes.append(TraefikRoute(router_name=name, hostnames=hostnames, service_name=service))
            logger.debug(
                "Container %s router %s: %s", container.name, name, hostnames
            )

        return routes


class TraefikApiSource:
    """Read Traefik routes from the Traefik API."""

    def __init__(self, config: TraefikConfig) -> None:
        self._config = config

    async def discover(self) -> list[TraefikRoute]:
        """Discover all Traefik routes from the API."""
        async with httpx.AsyncClient(base_url=self._config.url) as client:
            resp = await client.get("/api/http/routers")
            resp.raise_for_status()
            data = resp.json()

        routes: list[TraefikRoute] = []
        seen_hosts: set[tuple[str, ...]] = set()

        for router in data:
            rule = router.get("rule", "")
            hostnames = extract_hosts_from_rule(rule)
            if not hostnames:
                continue

            hosts_key = tuple(sorted(hostnames))
            if hosts_key in seen_hosts:
                continue
            seen_hosts.add(hosts_key)

            name = router.get("name", "unknown")
            service = router.get("service")
            routes.append(TraefikRoute(router_name=name, hostnames=hostnames, service_name=service))

        logger.info("Discovered %d Traefik routes from API", len(routes))
        return routes


class FileSource:
    """Read Traefik routes from the file provider's directory (YAML or TOML).

    Covers routes that are not on a container — e.g. services outside Docker. Any error
    (missing directory, unparseable file) is raised so the sync cycle fails instead of
    treating the routes as gone and deleting their records.
    """

    SUFFIXES = (".yml", ".yaml", ".toml")

    def __init__(self, config: TraefikConfig) -> None:
        self._config = config

    def discover(self) -> list[TraefikRoute]:
        directory = Path(self._config.file_dir or "")
        if not directory.is_dir():
            raise FileNotFoundError(f"Traefik file provider directory not found: {directory}")

        routes: list[TraefikRoute] = []
        seen_hosts: set[tuple[str, ...]] = set()

        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix not in self.SUFFIXES:
                continue
            data = self._load(path)
            routers = ((data or {}).get("http") or {}).get("routers") or {}
            for name, router in routers.items():
                hostnames = extract_hosts_from_rule((router or {}).get("rule", ""))
                if not hostnames:
                    continue

                hosts_key = tuple(sorted(hostnames))
                if hosts_key in seen_hosts:
                    continue
                seen_hosts.add(hosts_key)

                routes.append(
                    TraefikRoute(
                        router_name=f"{name}@file",
                        hostnames=hostnames,
                        service_name=router.get("service"),
                    )
                )
                logger.debug("File %s router %s: %s", path.name, name, hostnames)

        logger.info("Discovered %d Traefik routes from %s", len(routes), directory)
        return routes

    @staticmethod
    def _load(path: Path) -> dict:
        if path.suffix == ".toml":
            with path.open("rb") as f:
                return tomllib.load(f)
        with path.open() as f:
            return yaml.safe_load(f) or {}
