#!/usr/bin/env python3
"""
scripts/cron_auto_release.py
────────────────────────────
Runs the escrow auto-release sweep: any escrow_transaction whose
auto_release_at is in the past and status is 'delivered' gets released
automatically without requiring the buyer to manually confirm receipt.

Usage
-----
  python scripts/cron_auto_release.py                 # calls /admin/escrow/auto-release
  CRON_SECRET=mysecret BASE_URL=https://… python …   # production use with secret

Environment variables
---------------------
  BASE_URL     – full origin of the running Flask app, e.g. https://app.mercx.io
                 defaults to http://127.0.0.1:5000
  CRON_SECRET  – value sent in X-Cron-Secret header (must match server config)
                 if unset, script falls back to a plain POST (requires admin session,
                 which is not possible from a cron job — always set CRON_SECRET in prod)
  TIMEOUT_SECS – HTTP request timeout in seconds, default 30
  LOG_LEVEL    – DEBUG | INFO | WARNING | ERROR, default INFO

Exit codes
----------
  0  – success (≥0 escrows swept, even if released=0)
  1  – configuration / environment error
  2  – HTTP / network error reaching the Flask endpoint
  3  – application-level error returned by the endpoint
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

# ── Configuration from environment ───────────────────────────────────────────

BASE_URL     = os.environ.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
CRON_SECRET  = os.environ.get("CRON_SECRET", "")
TIMEOUT_SECS = int(os.environ.get("TIMEOUT_SECS", "30"))
LOG_LEVEL    = os.environ.get("LOG_LEVEL", "INFO").upper()

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] cron_auto_release: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
log = logging.getLogger(__name__)


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> int:
    """Execute the auto-release sweep. Returns an exit code (0 = success)."""
    endpoint = f"{BASE_URL}/admin/escrow/auto-release"
    log.info("Starting escrow auto-release sweep → %s", endpoint)

    if not CRON_SECRET:
        log.warning(
            "CRON_SECRET is not set. The endpoint will reject unauthenticated "
            "POST requests unless you are running with an active admin session. "
            "Set CRON_SECRET in your environment for production cron jobs."
        )

    # Build request
    headers = {"Content-Type": "application/json"}
    if CRON_SECRET:
        headers["X-Cron-Secret"] = CRON_SECRET

    req = urllib.request.Request(
        url=endpoint,
        data=b"{}",
        headers=headers,
        method="POST",
    )

    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            elapsed = time.monotonic() - start
            status  = resp.status
            body    = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - start
        status  = exc.code
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        log.error("HTTP %d from endpoint (%.2fs): %s", status, elapsed, body[:400])
        if status == 401 or status == 403:
            log.error("Authorization failed — check CRON_SECRET matches server config.")
        return 2
    except urllib.error.URLError as exc:
        elapsed = time.monotonic() - start
        log.error("Network error reaching %s after %.2fs: %s", endpoint, elapsed, exc.reason)
        return 2
    except Exception as exc:                         # broad catch for safety
        elapsed = time.monotonic() - start
        log.error("Unexpected error after %.2fs: %r", elapsed, exc)
        return 2

    log.debug("HTTP %d received in %.2fs, body: %s", status, elapsed, body[:800])

    # Parse JSON response
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.error("Non-JSON response (HTTP %d): %s", status, body[:400])
        return 3

    if status != 200:
        log.error("Endpoint returned HTTP %d: %s", status, data.get("error", body[:200]))
        return 3

    processed = data.get("processed", 0)
    released  = data.get("released", 0)
    results   = data.get("results", [])

    log.info("Sweep complete: processed=%d released=%d (%.2fs)", processed, released, elapsed)

    # Log per-item details at DEBUG level
    for r in results:
        eid = r.get("escrow_id", "?")
        ok  = r.get("success", False)
        msg = r.get("message", "")
        if ok:
            log.debug("  ✓ escrow %s released", eid)
        else:
            log.debug("  ✗ escrow %s skipped: %s", eid, msg)

    # Log any failures at WARNING level
    failures = [r for r in results if not r.get("success")]
    if failures:
        log.warning("%d escrow(s) could not be released:", len(failures))
        for r in failures:
            log.warning("  escrow %s — %s", r.get("escrow_id", "?"), r.get("message", "unknown"))

    return 0


if __name__ == "__main__":
    sys.exit(run())
