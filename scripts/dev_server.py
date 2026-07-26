"""Локальный dev-сервер: статика site/ + все API через core.app.dispatch.

Запуск:  python scripts/dev_server.py [порт]
По умолчанию порт 3000, хранилище data/store.json.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import sys
import urllib.parse
from http.server import ThreadingHTTPServer

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.http import DispatchHandler  # noqa: E402

SITE_DIR = os.path.join(_ROOT, "site")

STATIC_ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/success": "success.html",
    "/success.html": "success.html",
    "/privacy": "privacy.html",
    "/consent": "consent.html",
    "/offer": "offer.html",
    "/payment-info": "payment-info.html",
}


class DevHandler(DispatchHandler):
    """API — в dispatch, всё остальное — статика из site/."""

    def _serve_static(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in STATIC_ROUTES:
            relative = STATIC_ROUTES[path]
        elif path.startswith("/site/"):
            relative = path[len("/site/"):]
        elif path.startswith("/photos/"):
            relative = path.lstrip("/")
        else:
            relative = path.lstrip("/")

        full = os.path.abspath(os.path.join(SITE_DIR, relative.replace("/", os.sep)))
        if not full.startswith(SITE_DIR) or not os.path.isfile(full):
            self.send_response(404)
            body = "not found".encode("utf-8")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/") or path == "/pay":
            self._run("GET")
        else:
            self._serve_static()

    def do_POST(self) -> None:  # noqa: N802
        self._run("POST")


def make_server(port: int = 0) -> ThreadingHTTPServer:
    """Сервер для тестов/локалки. port=0 — свободный порт."""
    return ThreadingHTTPServer(("127.0.0.1", port), DevHandler)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    os.environ.setdefault("APP_URL", f"http://localhost:{port}")
    os.environ.setdefault("STORAGE_PATH", os.path.join(_ROOT, "data", "store.json"))
    server = make_server(port)
    print(f"Dev server: http://localhost:{port}  (Ctrl+C для остановки)")
    print(f"Хранилище: {os.environ['STORAGE_PATH']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
