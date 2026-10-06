"""A small local web dashboard: ``python -m arbitrage_bot --ui``.

Standard library only. It listens on 127.0.0.1, serves one self-contained page
(dashboard.html) and the bot's latest snapshot as JSON, and offers three safe
controls: pause trading, resume, stop. Nothing on it can place an order or
change a setting.

Thread safety: the server thread never touches live bot state. It reads
``bot.latest_snapshot`` (a plain dict the bot's thread replaces after every
scan) and calls the bot's flag setters.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("dashboard.html")
# Browsers can't add a custom header to a cross-site request without a CORS
# preflight, which this server never approves - so requiring it blocks CSRF.
CONTROL_HEADER = "X-Arb-Dashboard"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LogBuffer(logging.Handler):
    """Keeps the most recent log lines for the dashboard's activity feed."""

    def __init__(self, capacity: int = 300):
        super().__init__(level=logging.INFO)
        self._lines: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._lock_lines = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # a bad log call must never break the bot
            message = str(record.msg)
        with self._lock_lines:
            self._lines.append({"t": record.created, "level": record.levelname, "message": message})

    def lines(self) -> List[Dict[str, Any]]:
        with self._lock_lines:
            return list(self._lines)


class Dashboard:
    def __init__(self, bot, log_buffer: LogBuffer, host: str = "127.0.0.1", port: int = 8765):
        self.bot = bot
        self.log_buffer = log_buffer
        self.page = PAGE.read_bytes()
        self.server = ThreadingHTTPServer((host, port), _make_handler(self))
        self.server.daemon_threads = True
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"

    def start(self) -> str:
        self._thread = threading.Thread(target=self.server.serve_forever, name="dashboard", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def state(self) -> Dict[str, Any]:
        state = dict(self.bot.latest_snapshot)
        state["status"] = self.bot.status  # flags can change between snapshots (e.g. Pause just clicked)
        state["events"] = self.log_buffer.lines()
        return state

    def control(self, action: str) -> Dict[str, Any]:
        if action == "pause":
            self.bot.pause()
        elif action == "resume":
            self.bot.resume()
        elif action == "stop":
            self.bot.request_stop()
        else:
            raise KeyError(action)
        return {"ok": True, "status": self.bot.status}


def _json_default(value):
    try:
        return float(value)  # Decimal
    except (TypeError, ValueError):
        return str(value)


def _make_handler(dashboard: Dashboard):
    class Handler(BaseHTTPRequestHandler):
        server_version = "arbitrage-bot-dashboard"

        def log_message(self, fmt, *args):  # keep the bot's console clean
            log.debug("dashboard %s - %s", self.address_string(), fmt % args)

        def _host_ok(self) -> bool:
            # Rejecting foreign Host headers stops DNS-rebinding pages from reading the state.
            host = self.headers.get("Host", "")
            return (urlsplit(f"//{host}").hostname or "") in LOCAL_HOSTS

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Dict[str, Any]) -> None:
            self._send(status, json.dumps(payload, default=_json_default).encode(), "application/json")

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"ok": False, "error": "forbidden host"})
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, dashboard.page, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(200, dashboard.state())
            return self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if not self._host_ok() or self.headers.get(CONTROL_HEADER) != "1":
                return self._json(403, {"ok": False, "error": "forbidden"})
            path = urlsplit(self.path).path
            action = path.rsplit("/", 1)[-1] if path.startswith("/api/") else ""
            try:
                return self._json(200, dashboard.control(action))
            except KeyError:
                return self._json(404, {"ok": False, "error": "unknown action"})

    return Handler
