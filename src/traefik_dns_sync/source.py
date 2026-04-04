"""Traefik source — reads hostnames from Docker labels or Traefik API."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import docker
import httpx

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
