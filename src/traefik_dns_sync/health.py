"""Lightweight health endpoint for container health checks."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

logger = logging.getLogger(__name__)


class HealthState:
    """Shared health state updated by the sync engine."""

    def __init__(self) -> None:
        self.last_sync: str | None = None
        self.last_result: str | None = None
        self.last_error: str | None = None
        self.sync_count: int = 0

    def record_sync(self, result: str) -> None:
        self.last_sync = datetime.now(UTC).isoformat()
        self.last_result = result
        self.last_error = None
        self.sync_count += 1

    def record_error(self, error: str) -> None:
        self.last_error = error

    def to_dict(self) -> dict:
        return {
            "status": "ok" if self.last_error is None else "error",
            "last_sync": self.last_sync,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "sync_count": self.sync_count,
        }


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/health", "/healthz", "/"):
                data = state.to_dict()
                status = 200 if data["status"] == "ok" else 503
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.write(body)
            else:
                self.send_error(404)

        def write(self, data: bytes) -> None:
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass  # Suppress per-request logging

    return Handler


def start_health_server(state: HealthState, port: int = 8080) -> HTTPServer:
    """Start the health HTTP server in a daemon thread."""
    server = HTTPServer(("0.0.0.0", port), _make_handler(state))
    thread = Thread(target=server.serve_forever, daemon=True, name="health-server")
    thread.start()
    logger.info("Health endpoint listening on :%d/health", port)
    return server
