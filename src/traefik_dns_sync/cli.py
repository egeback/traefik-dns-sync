"""CLI entrypoint for traefik-dns-sync."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading

import docker

from traefik_dns_sync.config import AppConfig
from traefik_dns_sync.health import HealthState, start_health_server
from traefik_dns_sync.providers.opnsense import OpnsenseProvider
from traefik_dns_sync.providers.rfc2136 import Rfc2136Provider
from traefik_dns_sync.providers.unifi import UnifiProvider
from traefik_dns_sync.state import StateTracker
from traefik_dns_sync.sync import SyncEngine

logger = logging.getLogger("traefik_dns_sync")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def build_providers(config: AppConfig) -> list:
    providers = []
    if config.opnsense.enabled:
        providers.append(OpnsenseProvider(config.opnsense, config.sync.owner_id))
        logger.info("OPNsense provider enabled (%s)", config.opnsense.host)
    if config.unifi.enabled:
        providers.append(UnifiProvider(config.unifi))
        logger.info("UniFi provider enabled (%s)", config.unifi.host)
    if config.rfc2136.enabled:
        providers.append(Rfc2136Provider(config.rfc2136))
        logger.info(
            "RFC2136 provider enabled (%s zone %s)", config.rfc2136.host, config.rfc2136.zone,
        )

    if not providers:
        logger.error("No providers enabled — set OPNSENSE_ENABLED=true and/or UNIFI_ENABLED=true")
        sys.exit(1)

    return providers


def watch_docker_events(
    docker_host: str,
    trigger: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
    stop: threading.Event,
) -> None:
    """Watch Docker container events and signal a sync on start/stop/die."""
    try:
        client = docker.DockerClient(base_url=docker_host)
    except Exception:
        logger.warning("Could not connect to Docker for event watching — polling only")
        return

    logger.info("Watching Docker events for container start/stop/die")
    try:
        for event in client.events(decode=True):
            if stop.is_set():
                break
            status = event.get("status", "")
            if event.get("Type") == "container" and status in ("start", "stop", "die"):
                name = event.get("Actor", {}).get("Attributes", {}).get("name", "?")
                logger.info("Docker event: container %s %s — triggering sync", name, status)
                loop.call_soon_threadsafe(trigger.set)
    except Exception:
        if not stop.is_set():
            logger.exception("Docker event watcher stopped unexpectedly")
    finally:
        client.close()


async def run_loop(engine: SyncEngine, config: AppConfig, health: HealthState) -> None:
    """Run the sync loop with interval polling + Docker event triggers."""
    interval = config.sync.interval
    stop = asyncio.Event()
    sync_trigger = asyncio.Event()
    thread_stop = threading.Event()

    def handle_signal() -> None:
        logger.info("Received shutdown signal")
        stop.set()
        sync_trigger.set()  # unblock any wait
        thread_stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Start Docker event watcher in background thread
    if config.traefik.use_docker:
        watcher = threading.Thread(
            target=watch_docker_events,
            args=(config.traefik.docker_host, sync_trigger, loop, thread_stop),
            daemon=True,
            name="docker-events",
        )
        watcher.start()

    logger.info(
        "Starting sync loop (interval: %ds, docker events: %s)",
        interval, config.traefik.use_docker,
    )

    while not stop.is_set():
        sync_trigger.clear()

        try:
            result = await engine.sync()
            logger.info("Sync complete: %s", result)
            health.record_sync(str(result))
        except Exception:
            logger.exception("Sync cycle failed")
            health.record_error("Sync cycle failed")

        # Wait for either: interval timeout OR Docker event trigger OR stop signal
        try:
            await asyncio.wait_for(
                _wait_any(stop, sync_trigger),
                timeout=interval,
            )
        except TimeoutError:
            pass  # Normal interval timeout — run next sync

    thread_stop.set()
    logger.info("Shutdown complete")


async def _wait_any(*events: asyncio.Event) -> None:
    """Wait until any of the given events is set."""
    done, pending = await asyncio.wait(
        [asyncio.create_task(e.wait()) for e in events],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()


async def run_once(engine: SyncEngine) -> None:
    """Run a single sync cycle."""
    result = await engine.sync()
    logger.info("Sync complete: %s", result)


def main() -> None:
    setup_logging()

    config = AppConfig()
    providers = build_providers(config)
    state = StateTracker(config.sync.state_file, config.sync.owner_id)

    engine = SyncEngine(config=config, providers=providers, state=state)

    logger.info(
        "traefik-dns-sync starting (owner: %s, providers: %s, policy: %s, dry_run: %s)",
        config.sync.owner_id,
        ", ".join(config.enabled_providers),
        config.sync.policy,
        config.sync.dry_run,
    )

    health = HealthState()

    # Start health server (unless disabled or --once mode)
    health_server = None
    if "--once" not in sys.argv and config.sync.health_port:
        health_server = start_health_server(health, config.sync.health_port)

    # Check for --once flag
    if "--once" in sys.argv:
        asyncio.run(run_once(engine))
    else:
        try:
            asyncio.run(run_loop(engine, config, health))
        finally:
            if health_server:
                health_server.shutdown()


if __name__ == "__main__":
    main()
