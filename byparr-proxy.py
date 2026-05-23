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
  CACHE_TTL_S TTL for successful response cache, in seconds (default 3600)
  STUB_CAT_PATHS  if truthy, /cat/... paths (indexer test endpoints) skip
                  Byparr and return a synthetic empty result page.
                  Useful as a temporary mitigation when Byparr can't solve
                  the Cloudflare challenge on the category pages but real
                  searches still work. (default off)
"""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("UPSTREAM", "https://1337x.to").rstrip("/")
BYPARR = os.environ.get("BYPARR", "http://byparr:8191/v1")
TIMEOUT_MS = int(os.environ.get("TIMEOUT_MS", "120000"))
PORT = int(os.environ.get("PORT", "8888"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
CACHE_TTL_S = int(os.environ.get("CACHE_TTL_S", "3600"))
STUB_CAT_PATHS = os.environ.get("STUB_CAT_PATHS", "").lower() in ("1", "true", "yes", "on")

# Skip static assets -- Prowlarr never needs them, and forwarding them to Byparr
# wastes a full Cloudflare-solve cycle per request.
SKIP_EXT = re.compile(
    r"\.(css|js|mjs|map|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|eot|mp4|webm)(\?|$)",
    re.IGNORECASE,
)

# Indexer "test" and "browse" endpoints in the cardigann definition all live
# under /cat/<Category>/<page>/. Real keyword searches go to /search/ or
# /sort-search/, so this regex isolates the test traffic.
CAT_PATH = re.compile(r"^/cat/", re.IGNORECASE)

# Single fake row so Prowlarr's "0 results = failure" gate is satisfied during
# indexer tests. 0 seeders ensures nothing ever picks it for download, and the
# title is obviously synthetic if it leaks into a Prowlarr browse view.
STUB_BODY = (
    b"<!DOCTYPE html><html><head><title>byparr-proxy stub</title></head><body>"
    b"<table class=\"table-list\"><tbody>"
    b"<tr>"
    b"<td class=\"coll-1 name\">"
    b"<a href=\"/sub/40/0/\">Other</a>"
    b"<a href=\"/torrent/0/byparr-proxy-stub-indexer-healthy/\">"
    b"byparr-proxy stub - indexer healthy</a>"
    b"</td>"
    b"<td class=\"coll-2 seeds\">0</td>"
    b"<td class=\"coll-3 leeches\">0</td>"
    b"<td class=\"coll-date\">now</td>"
    b"<td class=\"coll-4 size\">1 KB</td>"
    b"<td class=\"coll-5 user\">byparr-proxy</td>"
    b"</tr>"
    b"</tbody></table>"
    b"</body></html>"
)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("byparr-proxy")


class _Pending:
    __slots__ = ("event", "status", "body", "error")

    def __init__(self):
        self.event = threading.Event()
        self.status = None
        self.body = None
        self.error = None


# Cache successful upstream responses for CACHE_TTL_S. Sonarr/Radarr/Prowlarr
# poll the same indexer-test endpoints (e.g. /cat/TV/1/) on independent
# schedules; without this, each poll triggers a full Cloudflare solve and
# the bursts cause Byparr to 408 on queued requests.
_cache = {}          # path -> (expires_at_monotonic, status, body)
_inflight = {}       # path -> _Pending
_state_lock = threading.Lock()
# Byparr drives a real browser, so it effectively serializes. Holding this
# while calling Byparr keeps a burst from sharing a single maxTimeout window
# across N concurrent requests (which is what caused the 408s).
_byparr_lock = threading.Lock()


def _fetch_from_byparr(rid, target):
    """Call Byparr and return (status, body). Raises on failure."""
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
    with urllib.request.urlopen(req, timeout=TIMEOUT_MS / 1000 + 15) as resp:
        data = json.loads(resp.read())
    if data.get("status") != "ok":
        raise RuntimeError(f"byparr returned: {data.get('message', 'unknown')}")
    sol = data["solution"]
    return sol.get("status", 200), sol["response"].encode("utf-8", errors="replace")


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

        if STUB_CAT_PATHS and CAT_PATH.search(path):
            log.info("[%s] %s %s from %s -> 200 stubbed (cat path, bypassing byparr)",
                     rid, method, path, client)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(STUB_BODY)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(STUB_BODY)
            return

        status, body, source, byparr_elapsed = self._get(rid, method, path, client)
        if status is None:
            # error already sent
            return

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(body)

        elapsed = time.monotonic() - start
        if source == "cache":
            log.info("[%s] %s %s <- %d in %.2fs (cache hit, %d bytes)",
                     rid, method, path, status, elapsed, len(body))
        elif source == "coalesced":
            log.info("[%s] %s %s <- %d in %.2fs (coalesced with in-flight, %d bytes)",
                     rid, method, path, status, elapsed, len(body))
        else:
            log.info("[%s] %s %s <- %d in %.2fs (byparr %.2fs, %d bytes)",
                     rid, method, path, status, elapsed,
                     byparr_elapsed, len(body))

    def _get(self, rid, method, path, client):
        """Return (status, body, source, byparr_elapsed) or (None, ...) on error."""
        now = time.monotonic()

        with _state_lock:
            entry = _cache.get(path)
            if entry and entry[0] > now:
                age = CACHE_TTL_S - (entry[0] - now)
                log.info("[%s] %s %s from %s -> cache hit (age %.0fs, ttl %ds)",
                         rid, method, path, client, age, CACHE_TTL_S)
                return entry[1], entry[2], "cache", 0.0

            pending = _inflight.get(path)
            if pending is not None:
                log.info("[%s] %s %s from %s -> coalescing with in-flight request",
                         rid, method, path, client)
                owner = False
            else:
                pending = _Pending()
                _inflight[path] = pending
                owner = True

        if not owner:
            pending.event.wait(timeout=TIMEOUT_MS / 1000 + 30)
            if pending.error is not None:
                log.error("[%s] %s %s <- coalesced request failed: %s",
                          rid, method, path, pending.error)
                self.send_error(502, f"byparr request failed: {pending.error}")
                return None, None, None, 0.0
            return pending.status, pending.body, "coalesced", 0.0

        target = UPSTREAM + path
        log.info("[%s] %s %s from %s -> forwarding to byparr (%s)",
                 rid, method, path, client, target)
        byparr_start = time.monotonic()
        try:
            with _byparr_lock:
                status, body = _fetch_from_byparr(rid, target)
        except Exception as e:
            byparr_elapsed = time.monotonic() - byparr_start
            with _state_lock:
                _inflight.pop(path, None)
            pending.error = e
            pending.event.set()
            log.error("[%s] %s %s <- byparr request failed after %.2fs: %s",
                      rid, method, path, byparr_elapsed, e)
            self.send_error(502, f"byparr request failed: {e}")
            return None, None, None, byparr_elapsed

        byparr_elapsed = time.monotonic() - byparr_start
        with _state_lock:
            _inflight.pop(path, None)
            if 200 <= status < 300:
                _cache[path] = (time.monotonic() + CACHE_TTL_S, status, body)
                log.info("[%s] %s %s -> cached for %ds (status %d, %d bytes)",
                         rid, method, path, CACHE_TTL_S, status, len(body))
        pending.status = status
        pending.body = body
        pending.event.set()
        return status, body, "fresh", byparr_elapsed

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
    log.info("  cache ttl:  %d s", CACHE_TTL_S)
    log.info("  stub /cat/: %s", "on" if STUB_CAT_PATHS else "off")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("ready, listening on 0.0.0.0:%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutdown signal received, stopping")


if __name__ == "__main__":
    main()
