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


# ── Convenience wrappers ──────────────────────────────────────

def db_select(table: str, columns: str = "*", filters: dict | None = None,
              order: str | None = None, limit: int | None = None,
              single: bool = False):
    """Generic SELECT helper. Returns data list (or dict if single=True)."""
    q = get_supabase().table(table).select(columns)
    for col, val in (filters or {}).items():
        q = q.eq(col, val)
    if order:
        desc = order.startswith("-")
        q = q.order(order.lstrip("-"), desc=desc)
    if limit:
        q = q.limit(limit)
    if single:
        try:
            return q.single().execute().data
        except Exception:
            return None
    return q.execute().data or []


def db_insert(table: str, data: dict):
    """INSERT a row and return the created record."""
    try:
        res = get_supabase().table(table).insert(data).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        current_app.logger.error(f"db_insert({table}): {e}")
        return None


def db_update(table: str, data: dict, filters: dict):
    """UPDATE rows matching filters. Returns list of updated rows."""
    try:
        q = get_supabase().table(table).update(data)
        for col, val in filters.items():
            q = q.eq(col, val)
        res = q.execute()
        return res.data or []
    except Exception as e:
        current_app.logger.error(f"db_update({table}): {e}")
        return []


def db_delete(table: str, filters: dict):
    """DELETE rows matching filters."""
    try:
        q = get_supabase().table(table).delete()
        for col, val in filters.items():
            q = q.eq(col, val)
        q.execute()
        return True
    except Exception as e:
        current_app.logger.error(f"db_delete({table}): {e}")
        return False


def db_upsert(table: str, data: dict, on_conflict: str):
    """UPSERT a row."""
    try:
        res = get_supabase().table(table).upsert(data, on_conflict=on_conflict).execute()
        return res.data[0] if res.data else None
    except Exception as e:
        current_app.logger.error(f"db_upsert({table}): {e}")
        return None


def db_rpc(fn: str, params: dict | None = None):
    """Call a Postgres RPC/function."""
    try:
        res = get_supabase().rpc(fn, params or {}).execute()
        return res.data
    except Exception as e:
        current_app.logger.error(f"db_rpc({fn}): {e}")
        return None


# ── Storage helpers ───────────────────────────────────────────

def storage_upload(bucket: str, path: str, file_bytes: bytes,
                   content_type: str = "application/octet-stream") -> str | None:
    """Upload bytes to Supabase Storage. Returns public URL or None."""
    try:
        sb = get_supabase()
        sb.storage.from_(bucket).upload(path, file_bytes, {"content-type": content_type})
        return sb.storage.from_(bucket).get_public_url(path)
    except Exception as e:
        current_app.logger.error(f"storage_upload({path}): {e}")
        return None


def storage_delete(bucket: str, path: str) -> bool:
    try:
        get_supabase().storage.from_(bucket).remove([path])
        return True
    except Exception as e:
        current_app.logger.error(f"storage_delete({path}): {e}")
        return False


def storage_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str | None:
    """Generate a signed URL for private file access."""
    try:
        res = get_supabase().storage.from_(bucket).create_signed_url(path, expires_in)
        return res.get("signedURL") or res.get("signedUrl")
    except Exception as e:
        current_app.logger.error(f"storage_signed_url({path}): {e}")
        return None
