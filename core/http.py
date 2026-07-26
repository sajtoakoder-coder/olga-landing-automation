"""Базовый HTTP-обработчик: превращает BaseHTTPRequestHandler-запрос в dispatch().

Используется и Vercel-функциями (api/*.py), и локальным dev-сервером.
"""
from __future__ import annotations

import logging
import urllib.parse
from http.server import BaseHTTPRequestHandler

from core.app import dispatch, error_response
from storage.store import get_store

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000  # 1 МБ достаточно для всех наших запросов


class DispatchHandler(BaseHTTPRequestHandler):
    """Обработчик, направляющий любой запрос в core.app.dispatch."""

    protocol_version = "HTTP/1.1"

    def _run(self, method: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY_BYTES:
                status, headers, body = error_response(413, "слишком большой запрос")
            else:
                body_bytes = self.rfile.read(length) if length > 0 else b""
                status, headers, body = dispatch(
                    get_store(),
                    method,
                    parsed.path,
                    query,
                    body_bytes,
                    self.headers.get("Content-Type", ""),
                )
        except Exception as exc:  # noqa: BLE001 - последний рубеж
            logger.exception("HTTP-обработчик упал: %s", exc)
            status, headers, body = error_response(500, "внутренняя ошибка")

        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - API BaseHTTPRequestHandler
        self._run("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._run("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._run("OPTIONS")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.info("%s %s", self.address_string(), format % args)
