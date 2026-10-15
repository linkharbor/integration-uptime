#!/usr/bin/env python3
"""LinkHarbor uptime agent.

Checks one URL on an interval and pushes the result to a LinkHarbor
"Custom (API)" integration: a status card on your bio page with Up/Down and
the response time, plus events for push notifications when the site goes
down or comes back.

    export LH_INGEST_URL="https://dock.at-harbor.com/v1/ingest/123"
    export LH_INGEST_SECRET="…"
    python3 agent.py --url-check https://example.com --title "My website"

A check passes when the HTTP status is within --expect-status (default
200-399, redirects are followed) and, if given, the body contains
--expect-text. The site counts as down after --fails-before-down
consecutive failed checks (default 2). No third-party packages needed
(Python 3.10+, standard library only).

Events sent (each can drive a push rule in the dashboard):
    site.down        data.error, data.status
    site.up          data.downtime_minutes (0 on the first check)
    response.slow    data.ms — response time above --slow-ms (0 = off)
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

log = logging.getLogger("linkharbor")

UA = "LinkHarbor-Uptime/1.0"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url-check", required=True, help="URL to check, e.g. https://example.com/health")
    p.add_argument("--method", default="GET", choices=["GET", "HEAD"], help="HTTP method (default GET)")
    p.add_argument("--expect-status", default="200-399", help="accepted HTTP status codes, e.g. 200 or 200-299,304 (default 200-399)")
    p.add_argument("--expect-text", default="", help="the body must contain this text (GET only)")
    p.add_argument("--timeout", type=float, default=10, help="seconds to wait for a response (default 10)")
    p.add_argument("--fails-before-down", type=int, default=2, help="consecutive failed checks before the site counts as down (default 2)")
    p.add_argument("--slow-ms", type=int, default=0, help="send response.slow when the response takes longer (0 = off)")
    p.add_argument("--title", default="", help="card title (default: the host)")
    p.add_argument("--name", default="", help="same as --title")
    p.add_argument("--address", default="", help="text shown on the card while up (default: the HTTP status)")
    p.add_argument("--url", default="", help="link behind the card's button (default: the checked URL)")
    p.add_argument("--cta", default="Open", help="button label")
    p.add_argument("--image-url", default="", help="banner image for the large layout")
    p.add_argument("--interval", type=int, default=60, help="seconds between checks (default 60)")
    p.add_argument("--heartbeat", type=int, default=10, help="minutes between state-only updates (0 = only on change)")
    p.add_argument("--ingest-url", default=os.environ.get("LH_INGEST_URL", ""))
    p.add_argument("--ingest-secret", default=os.environ.get("LH_INGEST_SECRET", ""))
    p.add_argument("--once", action="store_true", help="check once, send, exit")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    if not a.ingest_url or not a.ingest_secret:
        p.error("set LH_INGEST_URL and LH_INGEST_SECRET (or --ingest-url / --ingest-secret)")
    if not a.url_check.startswith(("http://", "https://")):
        p.error("--url-check must start with http:// or https://")
    try:
        a.expect_status = parse_status_ranges(a.expect_status)
    except ValueError:
        p.error("--expect-status must look like 200, 200-299 or 200-299,304")
    a.title = a.title or a.name or urllib.parse.urlsplit(a.url_check).hostname or a.url_check
    return a


def parse_status_ranges(spec: str) -> list[tuple[int, int]]:
    ranges = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        ranges.append((int(lo), int(hi or lo)))
    if not ranges:
        raise ValueError(spec)
    return ranges


class Ingest:
    def __init__(self, url: str, secret: str) -> None:
        self.url, self.secret = url, secret

    def send(self, event: str | None, data: dict | None, state: dict) -> bool:
        body: dict = {"state": state}
        if event:
            body["event"] = event
            body["event_id"] = f"{event}-{uuid.uuid4()}"
        if data:
            body["data"] = data
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json",
                     "User-Agent": UA},
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=10) as res:
                    log.debug("← %s %s", res.status, res.read(200).decode(errors="replace"))
                    return True
            except urllib.error.HTTPError as e:
                text = e.read(300).decode(errors="replace")
                if e.code == 401:
                    log.error("401 from LinkHarbor — wrong secret or integration disconnected")
                    return False
                if e.code < 500:
                    log.error("%s from LinkHarbor: %s", e.code, text)
                    return False
                log.warning("%s from LinkHarbor (attempt %d)", e.code, attempt)
            except Exception as e:  # network
                log.warning("request failed (attempt %d): %s", attempt, e)
            time.sleep(2)
        return False


def probe(url: str, method: str, timeout: float, expect_status: list[tuple[int, int]], expect_text: str) -> dict:
    """One check. Returns {"ok": bool, "ms": int, "status": int | None, "error": str}."""
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, "Accept": "*/*"})
    started = time.monotonic()
    status: int | None = None
    body = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            status = res.status
            if method == "GET" and expect_text:
                body = res.read(2_000_000)
    except urllib.error.HTTPError as e:
        status = e.code
    except urllib.error.URLError as e:
        return {"ok": False, "ms": elapsed_ms(started), "status": None, "error": str(e.reason)[:200]}
    except (TimeoutError, http.client.HTTPException, OSError, ValueError) as e:
        return {"ok": False, "ms": elapsed_ms(started), "status": None, "error": (str(e) or type(e).__name__)[:200]}
    ms = elapsed_ms(started)
    if not any(lo <= status <= hi for lo, hi in expect_status):
        return {"ok": False, "ms": ms, "status": status, "error": f"HTTP {status}"}
    if method == "GET" and expect_text and expect_text not in body.decode(errors="replace"):
        return {"ok": False, "ms": ms, "status": status, "error": f"HTTP {status}, text not found: {expect_text[:60]}"}
    return {"ok": True, "ms": ms, "status": status, "error": ""}


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def build_state(a: argparse.Namespace, up: bool, result: dict) -> dict:
    s: dict = {
        "type": "status",
        "title": a.title[:80],
        "state": "online" if up else "offline",
        "label": "Up" if up else "Down",
        "url": a.url or a.url_check,
        "cta": a.cta,
    }
    if up:
        s["value"] = f"{result['ms']} ms"
        s["text"] = (a.address or f"HTTP {result['status']}")[:200]
    else:
        s["text"] = (result["error"] or "no response")[:200]
    if a.image_url:
        s["image_url"] = a.image_url
    return s


def main() -> int:
    a = parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingest = Ingest(a.ingest_url, a.ingest_secret)

    up: bool | None = None
    last_good: dict = {"ms": 0, "status": 0, "error": ""}
    fails = 0
    down_since = 0.0
    last_sent = 0.0
    heartbeat_s = a.heartbeat * 60

    while True:
        r = probe(a.url_check, a.method, a.timeout, a.expect_status, a.expect_text)
        log.debug("%s %s → %s %s ms %s", a.method, a.url_check, r["status"], r["ms"], r["error"])
        fails = 0 if r["ok"] else fails + 1
        if r["ok"]:
            now_up = True
            last_good = r
        elif fails >= max(1, a.fails_before_down):
            now_up = False
        else:
            now_up = up  # not down yet; keep what the card shows (None on the first checks)
            log.info("check failed (%d/%d): %s", fails, a.fails_before_down, r["error"])

        events: list[tuple[str, dict | None]] = []
        if now_up is not None and now_up != up:
            if now_up:
                minutes = int((time.time() - down_since) / 60) if down_since else 0
                events.append(("site.up", {"downtime_minutes": minutes, "ms": r["ms"]}))
                down_since = 0.0
            else:
                down_since = time.time()
                events.append(("site.down", {"error": r["error"], "status": r["status"] or 0}))
        if r["ok"] and a.slow_ms > 0 and r["ms"] > a.slow_ms:
            events.append(("response.slow", {"ms": r["ms"], "status": r["status"]}))

        if now_up is not None:
            state = build_state(a, now_up, last_good if now_up else r)
            if events:
                for ev, data in events:
                    log.info("%s %s", ev, json.dumps(data) if data else "")
                    ingest.send(ev, data, state)
                last_sent = time.time()
            elif heartbeat_s and time.time() - last_sent >= heartbeat_s:
                log.info("heartbeat: %s, %s ms", "up" if now_up else "down", r["ms"])
                ingest.send(None, None, state)
                last_sent = time.time()
            up = now_up

        if a.once:
            return 0 if r["ok"] else 1
        time.sleep(max(5, a.interval))


if __name__ == "__main__":
    sys.exit(main())
