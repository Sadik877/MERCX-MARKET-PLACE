import time
from supabase import create_client, Client
from flask import current_app
import functools

_client: Client | None = None

def get_supabase() -> Client:
    """
    Return a singleton Supabase client.

    Reads exactly two environment variables:
        - SUPABASE_URL
        - SUPABASE_SECRET_KEY

    Raises a clear RuntimeError naming the specific missing variable,
    and surfaces a clean error if the SDK rejects the provided key.
    """
    global _client
    if _client is None:
        url = current_app.config.get("SUPABASE_URL", "")
        key = current_app.config.get("SUPABASE_SECRET_KEY", "")

        # Validate each variable independently so the error message is specific.
        if not url:
            raise RuntimeError(
                "Missing environment variable: SUPABASE_URL. "
                "Set it to your project's Supabase URL (Project Settings → API)."
            )
        if not key:
            raise RuntimeError(
                "Missing environment variable: SUPABASE_SECRET_KEY. "
                "Set it to your project's Supabase secret API key "
                "(Project Settings → API → Secret keys)."
            )

        try:
            _client = create_client(url, key)
        except Exception as e:
            hint = ""
            if "invalid api key" in str(e).lower():
                hint = (
                    " This specific error is almost always caused by an outdated "
                    "'supabase' Python package that pre-dates Supabase's newer "
                    "non-JWT key format (sb_secret_... / sb_publishable_...). "
                    "Older SDK versions try to validate the key as a JWT and "
                    "reject it locally before any network call is made. "
                    "Fix: ensure requirements.txt pins supabase>=2.20.0,<3.0.0 "
                    "and that the deployment actually reinstalled dependencies "
                    "(clear the build cache on Render if needed)."
                )
            raise RuntimeError(
                "Failed to initialize the Supabase client with the provided "
                "SUPABASE_URL / SUPABASE_SECRET_KEY. Verify that SUPABASE_URL "
                "is correct and that SUPABASE_SECRET_KEY is a valid, active "
                f"secret key for that project.{hint} Original error: {e}"
            ) from e

    return _client


def reset_client() -> None:
    """Force a reconnect on the next db_* call. Used automatically after a
    transient connection error, and can be called manually after rotating
    Supabase keys without restarting the process."""
    global _client
    _client = None


# ── Transient-error retry ─────────────────────────────────────
# Network blips (DNS resolution failing for a second right after a cold
# start, brief timeouts, connection resets) are common and self-resolving.
# Retrying these specific error classes — and only these — avoids masking
# real bugs (bad queries, permission errors, etc. fail immediately as before).
_TRANSIENT_HINTS = (
    "timeout", "timed out", "connection", "temporarily unavailable",
    "reset by peer", "broken pipe", "502", "503", "504",
    "name or service not known", "nodename nor servname",
    "temporary failure in name resolution", "getaddrinfo",
    "no address associated with hostname", "errno -2",
    "network is unreachable",
)


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(hint in msg for hint in _TRANSIENT_HINTS)


def _with_retry(fn, what: str, retries: int = 2, backoff: float = 0.4):
    """Run fn() with a couple of retries for transient errors only.
    Returns (ok: bool, result_or_exception)."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return True, fn()
        except Exception as e:
            last_exc = e
            if attempt < retries and _is_transient(e):
                time.sleep(backoff * (attempt + 1))
                reset_client()
                continue
            break
    current_app.logger.error(f"{what} error: {last_exc}")
    return False, last_exc


# ── Convenience wrappers ──────────────────────────────────────

def db_select(table: str, columns: str = "*", filters: dict | None = None,
              order: str | None = None, limit: int | None = None,
              single: bool = False):
    """Generic SELECT helper. Returns data list (or dict if single=True).
    On failure (including transient network errors, retried automatically):
    returns None if single=True, else an empty list — never raises."""
    def _run():
        q = get_supabase().table(table).select(columns)
        for col, val in (filters or {}).items():
            q = q.eq(col, val)
        if order:
            desc = order.startswith("-")
            q = q.order(order.lstrip("-"), desc=desc)
        if limit:
            q = q.limit(limit)
        if single:
            return q.single().execute().data
        return q.execute().data or []

    ok, result = _with_retry(_run, f"db_select({table})")
    if not ok:
        return None if single else []
    return result


def db_insert(table: str, data: dict):
    """INSERT a row and return the created record."""
    def _run():
        res = get_supabase().table(table).insert(data).execute()
        return res.data[0] if res.data else None
    ok, result = _with_retry(_run, f"db_insert({table})", retries=0)
    return result if ok else None


def db_update(table: str, data: dict, filters: dict):
    """UPDATE rows matching filters. Returns list of updated rows."""
    def _run():
        q = get_supabase().table(table).update(data)
        for col, val in filters.items():
            q = q.eq(col, val)
        res = q.execute()
        return res.data or []
    ok, result = _with_retry(_run, f"db_update({table})", retries=0)
    return result if ok else []


def db_delete(table: str, filters: dict):
    """DELETE rows matching filters."""
    def _run():
        q = get_supabase().table(table).delete()
        for col, val in filters.items():
            q = q.eq(col, val)
        q.execute()
        return True
    ok, _ = _with_retry(_run, f"db_delete({table})", retries=0)
    return ok


def db_upsert(table: str, data: dict, on_conflict: str):
    """UPSERT a row."""
    def _run():
        res = get_supabase().table(table).upsert(data, on_conflict=on_conflict).execute()
        return res.data[0] if res.data else None
    ok, result = _with_retry(_run, f"db_upsert({table})", retries=0)
    return result if ok else None


def db_rpc(fn: str, params: dict | None = None):
    """Call a Postgres RPC/function."""
    def _run():
        res = get_supabase().rpc(fn, params or {}).execute()
        return res.data
    ok, result = _with_retry(_run, f"db_rpc({fn})", retries=0)
    return result if ok else None


# ── Storage helpers ───────────────────────────────────────────

def storage_upload(bucket: str, path: str, file_bytes: bytes,
                   content_type: str = "application/octet-stream") -> str | None:
    """Upload bytes to Supabase Storage. Returns public URL or None."""
    def _run():
        sb = get_supabase()
        sb.storage.from_(bucket).upload(path, file_bytes, {"content-type": content_type})
        return sb.storage.from_(bucket).get_public_url(path)
    ok, result = _with_retry(_run, f"storage_upload({path})", retries=1)
    return result if ok else None


def storage_delete(bucket: str, path: str) -> bool:
    def _run():
        get_supabase().storage.from_(bucket).remove([path])
        return True
    ok, _ = _with_retry(_run, f"storage_delete({path})", retries=1)
    return ok


def storage_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str | None:
    """Generate a signed URL for private file access."""
    def _run():
        res = get_supabase().storage.from_(bucket).create_signed_url(path, expires_in)
        return res.get("signedURL") or res.get("signedUrl")
    ok, result = _with_retry(_run, f"storage_signed_url({path})", retries=1)
    return result if ok else None
