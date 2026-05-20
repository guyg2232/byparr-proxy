"""
byparr-proxy: tiny HTTP server that fronts an upstream site via Byparr.

Prowlarr/Jackett talk to us in plain HTTP; we ask Byparr to fetch from the
real upstream and return its solved body directly. Prowlarr never sees a
Cloudflare challenge, so it never triggers its cookie-replay path -- which
is the path that modern Cloudflare detects and blocks.

Configuration is via environment variables:
  UPSTREAM    base URL of the indexer, e.g. https://1337x.to (no trailing slash)
  BYPARR      Byparr /v1 endpoint, e.g. http://byparr:8191/v1
  TIMEOUT_MS  per-request timeout passed to Byparr (default 120000)
  PORT        local listen port (default 8888)
  LOG_LEVEL   DEBUG, INFO, WARNING, ERROR (default INFO)
"""
import json
import logging
import os
import re
import sys
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("UPSTREAM", "https://1337x.to").rstrip("/")
BYPARR = os.environ.get("BYPARR", "http://byparr:8191/v1")
TIMEOUT_MS = int(os.environ.get("TIMEOUT_MS", "120000"))
PORT = int(os.environ.get("PORT", "8888"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Skip static assets -- Prowlarr never needs them, and forwarding them to Byparr
# wastes a full Cloudflare-solve cycle per request.
SKIP_EXT = re.compile(
    r"\.(css|js|mjs|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|mp4|webm)(\?|$)",
    re.IGNORECASE,
)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("byparr-proxy")


class Handler(BaseHTTPRequestHandler):
    def _proxy(self):
        rid = uuid.uuid4().hex[:6]
        client = self.address_string()
        method = self.command
        path = self.path
        start = time.monotonic()

        if SKIP_EXT.search(path):
            log.info("[%s] %s %s from %s -> 404 skipped (static asset)",
                     rid, method, path, client)
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        target = UPSTREAM + path
        log.info("[%s] %s %s from %s -> forwarding to byparr (%s)",
                 rid, method, path, client, target)

        payload = json.dumps({
            "cmd": "request.get",
            "url": target,
            "maxTimeout": TIMEOUT_MS,
        }).encode()
        req = urllib.request.Request(
            BYPARR,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        byparr_start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_MS / 1000 + 15) as resp:
                raw = resp.read()
                data = json.loads(raw)
        except Exception as e:
            elapsed = time.monotonic() - start
            log.error("[%s] %s %s <- byparr request failed after %.2fs: %s",
                      rid, method, path, elapsed, e)
            self.send_error(502, f"byparr request failed: {e}")
            return

        byparr_elapsed = time.monotonic() - byparr_start

        if data.get("status") != "ok":
            elapsed = time.monotonic() - start
            msg = data.get("message", "unknown")
            log.warning("[%s] %s %s <- byparr returned non-ok in %.2fs: %s",
                        rid, method, path, byparr_elapsed, msg)
            self.send_error(502, f"byparr returned: {msg}")
            return

        sol = data["solution"]
        body = sol["response"].encode("utf-8", errors="replace")
        upstream_status = sol.get("status", 200)
        upstream_url = sol.get("url", target)

        log.debug("[%s] byparr solved in %.2fs: %d, %d bytes, final url=%s",
                  rid, byparr_elapsed, upstream_status, len(body), upstream_url)

        self.send_response(upstream_status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)

        elapsed = time.monotonic() - start
        log.info("[%s] %s %s <- %d in %.2fs (byparr %.2fs, %d bytes)",
                 rid, method, path, upstream_status, elapsed,
                 byparr_elapsed, len(body))

    def do_GET(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()

    # Silence BaseHTTPRequestHandler's default per-request logging --
    # we emit our own structured lines from _proxy().
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        pass


def main():
    log.info("byparr-proxy starting")
    log.info("  upstream:   %s", UPSTREAM)
    log.info("  byparr:     %s", BYPARR)
    log.info("  timeout:    %d ms", TIMEOUT_MS)
    log.info("  port:       %d", PORT)
    log.info("  log level:  %s", LOG_LEVEL)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("ready, listening on 0.0.0.0:%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown signal received, stopping")


if __name__ == "__main__":
    main()
