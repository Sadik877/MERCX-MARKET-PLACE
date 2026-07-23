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
              single: bool = False, in_filters: dict | None = None,
              count_only: bool = False):
    """Generic SELECT helper. Returns data list (or dict if single=True).

    New (additive, backward-compatible) params — Phase 3 / BUG_INVENTORY
    "N+1 / fetch-entire-table" observation:
    - in_filters: dict of {column: [values...]} applied as `.in_(col, values)`,
      for batching lookups that previously required N per-row queries
      (e.g. fetching buyer/seller/user records for a page of orders in one
      round trip instead of one query per row inside a for-loop).
    - count_only: if True, skip fetching rows entirely and return just the
      matching row count (int) via Supabase's exact-count mode. Use this
      instead of `len(db_select(...))` for dashboard/admin stat tiles —
      avoids transferring full rows just to discard them for a number.
    """
    q = get_supabase().table(table).select(
        columns, count="exact" if count_only else None
    )
    for col, val in (filters or {}).items():
        q = q.eq(col, val)
    for col, vals in (in_filters or {}).items():
        q = q.in_(col, list(vals))
    if order:
        desc = order.startswith("-")
        q = q.order(order.lstrip("-"), desc=desc)
    if limit:
        q = q.limit(limit)
    if count_only:
        try:
            res = q.execute()
            return res.count or 0
        except Exception as e:
            current_app.logger.error(f"db_select count({table}): {e}")
            return 0
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


class WalletOperationError(Exception):
    """Raised when an atomic wallet RPC call fails unexpectedly."""
    pass


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


# ── Atomic wallet operations ──────────────────────────────────
#
# These wrap the Postgres functions defined in
# migrations/001_atomic_wallet_functions.sql. Unlike db_rpc() above,
# these do NOT swallow exceptions into a generic None/[] return —
# callers need to know unambiguously whether the atomic operation
# succeeded, so failures are raised as WalletOperationError for the
# blueprint to handle explicitly (instead of silently proceeding as
# if a balance mutation happened when it didn't).

def _call_rpc_single(fn: str, params: dict) -> dict:
    """Call an RPC that returns a single-row table result and return
    that row as a dict. Raises WalletOperationError on any failure
    (network, DB exception, or empty/malformed result) so financial
    code paths never silently continue on an ambiguous result."""
    try:
        res = get_supabase().rpc(fn, params).execute()
    except Exception as e:
        current_app.logger.error(f"wallet rpc {fn} failed: {e}")
        raise WalletOperationError(f"{fn} failed: {e}") from e

    data = res.data
    if not data:
        raise WalletOperationError(f"{fn} returned no data")
    # Supabase RPCs returning TABLE(...) come back as a list of rows.
    row = data[0] if isinstance(data, list) else data
    return row


def wallet_credit_idempotent(user_id, amount, reference, payment_method,
                              description, tx_type: str = "deposit") -> dict:
    """Atomically credit a user's wallet, or no-op if `reference` was
    already processed. Returns dict with keys: tx_id,
    already_processed, balance_before, balance_after."""
    return _call_rpc_single("wallet_credit_idempotent", {
        "p_user_id": str(user_id),
        "p_amount": float(amount),
        "p_reference": reference,
        "p_payment_method": payment_method,
        "p_description": description,
        "p_type": tx_type,
    })


def wallet_debit_atomic(user_id, amount, reference, description,
                         order_id=None, tx_type: str = "purchase") -> dict:
    """Atomically debit a user's wallet if sufficient balance exists.
    Returns dict with keys: tx_id, success, balance_before,
    balance_after. `success=False` means insufficient balance (or a
    prior identical reference already applied) — not an exception."""
    return _call_rpc_single("wallet_debit_atomic", {
        "p_user_id": str(user_id),
        "p_amount": float(amount),
        "p_reference": reference,
        "p_description": description,
        "p_order_id": str(order_id) if order_id else None,
        "p_type": tx_type,
    })


def checkout_wallet_debit_atomic(user_id, amount, reference, description) -> dict:
    """Atomically debit a buyer's wallet for a checkout, idempotent on
    `reference`. Returns dict with keys: tx_id, success, already_done,
    balance_before, balance_after."""
    return _call_rpc_single("checkout_wallet_debit_atomic", {
        "p_user_id": str(user_id),
        "p_amount": float(amount),
        "p_reference": reference,
        "p_description": description,
    })


def wallet_tx_approve_atomic(tx_id, admin_id) -> dict:
    """Atomically approve a pending deposit/withdrawal transaction,
    with row locking so it cannot be double-approved concurrently.
    Returns dict with keys: success, message, tx_type, user_id, amount."""
    return _call_rpc_single("wallet_tx_approve_atomic", {
        "p_tx_id": str(tx_id),
        "p_admin_id": str(admin_id),
    })


def wallet_tx_reject_atomic(tx_id, admin_id, note: str = "") -> dict:
    """Atomically reject a pending transaction, with the same row
    locking guarantee as wallet_tx_approve_atomic."""
    return _call_rpc_single("wallet_tx_reject_atomic", {
        "p_tx_id": str(tx_id),
        "p_admin_id": str(admin_id),
        "p_note": note,
    })


def increment_listing_views(listing_id) -> None:
    """Atomically increment a listing's view counter (no lost updates
    under concurrent page views)."""
    try:
        get_supabase().rpc("increment_listing_views",
                            {"p_listing_id": str(listing_id)}).execute()
    except Exception as e:
        current_app.logger.error(f"increment_listing_views({listing_id}): {e}")


def increment_download_count_atomic(order_item_id) -> dict:
    """Atomically check-and-increment an order item's download count.
    Returns dict with keys: allowed, new_count, max_downloads."""
    return _call_rpc_single("increment_download_count_atomic", {
        "p_order_item_id": str(order_item_id),
    })


# ── Escrow operations ──────────────────────────────────────────
#
# These wrap the Postgres functions defined in
# migrations/002_escrow_system.sql. Same contract as the wallet
# helpers above: failures raise WalletOperationError rather than
# returning an ambiguous None, since these all move money.

def escrow_hold_create(order_id, buyer_id, seller_id, amount, platform_fee,
                        seller_earnings, payment_method, payment_reference,
                        instant_delivery: bool = False,
                        auto_release_hours: int = 72) -> dict:
    """Create the escrow hold for a newly-paid order. Idempotent on
    order_id/payment_reference. Returns dict with keys: escrow_id,
    already_processed, status."""
    return _call_rpc_single("escrow_hold_create", {
        "p_order_id": str(order_id),
        "p_buyer_id": str(buyer_id),
        "p_seller_id": str(seller_id),
        "p_amount": float(amount),
        "p_platform_fee": float(platform_fee),
        "p_seller_earnings": float(seller_earnings),
        "p_payment_method": payment_method,
        "p_payment_reference": payment_reference,
        "p_instant_delivery": bool(instant_delivery),
        "p_auto_release_hours": int(auto_release_hours),
    })


def escrow_mark_delivered(escrow_id, seller_id, auto_release_hours: int = 72) -> dict:
    """Seller marks the order delivered; starts the auto-release
    countdown. Returns dict with keys: success, message."""
    return _call_rpc_single("escrow_mark_delivered", {
        "p_escrow_id": str(escrow_id),
        "p_seller_id": str(seller_id),
        "p_auto_release_hours": int(auto_release_hours),
    })


def escrow_release(escrow_id, actor_id, reason: str = "buyer_confirmed") -> dict:
    """Release held funds to the seller (buyer confirmation or
    system auto-release). Returns dict with keys: success,
    already_processed, seller_id, amount, message."""
    return _call_rpc_single("escrow_release", {
        "p_escrow_id": str(escrow_id),
        "p_actor_id": str(actor_id) if actor_id else None,
        "p_reason": reason,
    })


def escrow_auto_release_due() -> list:
    """Release every escrow past its auto-release deadline. Returns a
    list of dicts with keys: escrow_id, success, message. Safe to
    call repeatedly (e.g. from a scheduler) — already-released rows
    are simply not selected."""
    try:
        res = get_supabase().rpc("escrow_auto_release_due", {}).execute()
        return res.data or []
    except Exception as e:
        current_app.logger.error(f"escrow_auto_release_due failed: {e}")
        raise WalletOperationError(f"escrow_auto_release_due failed: {e}") from e


def escrow_open_dispute(escrow_id, raised_by, reason, description: str = None) -> dict:
    """Open a dispute against an escrow transaction, freezing its
    funds. Returns dict with keys: dispute_id, success, message."""
    return _call_rpc_single("escrow_open_dispute", {
        "p_escrow_id": str(escrow_id),
        "p_raised_by": str(raised_by),
        "p_reason": reason,
        "p_description": description,
    })


def escrow_resolve_dispute(dispute_id, admin_id, resolution: str,
                            refund_amount: float = None, note: str = None) -> dict:
    """Admin resolves a dispute. `resolution` is one of
    'refund_buyer', 'release_seller', 'partial_refund'
    (partial_refund requires refund_amount). Returns dict with keys:
    success, message."""
    return _call_rpc_single("escrow_resolve_dispute", {
        "p_dispute_id": str(dispute_id),
        "p_admin_id": str(admin_id),
        "p_resolution": resolution,
        "p_refund_amount": float(refund_amount) if refund_amount is not None else None,
        "p_note": note,
    })


# ── Seller payout operations ───────────────────────────────────

def payout_request_approve(payout_id, admin_id, gateway_reference: str = None) -> dict:
    """Approve and pay out a pending seller payout request. Returns
    dict with keys: success, message."""
    return _call_rpc_single("payout_request_approve_atomic", {
        "p_payout_id": str(payout_id),
        "p_admin_id": str(admin_id),
        "p_gateway_reference": gateway_reference,
    })


def payout_request_reject(payout_id, admin_id, note: str = "") -> dict:
    """Reject a pending seller payout request (no funds move).
    Returns dict with keys: success, message."""
    return _call_rpc_single("payout_request_reject_atomic", {
        "p_payout_id": str(payout_id),
        "p_admin_id": str(admin_id),
        "p_note": note,
    })


# ── Webhook replay protection ──────────────────────────────────

def webhook_event_record(gateway: str, event_id: str, event_type: str,
                          payload: dict, signature_valid: bool) -> dict:
    """Log an inbound webhook delivery before processing it. Returns
    dict with keys: is_new, id, was_processed.
    - is_new=True: never seen before, process it.
    - is_new=False, was_processed=True: true replay of a delivery that
      already completed successfully — skip reprocessing entirely.
    - is_new=False, was_processed=False: seen before but the previous
      attempt errored out — safe to retry (downstream idempotency
      references still prevent any double-credit)."""
    return _call_rpc_single("webhook_event_record", {
        "p_gateway": gateway,
        "p_event_id": event_id,
        "p_event_type": event_type,
        "p_payload": payload,
        "p_signature_valid": signature_valid,
    })


def webhook_event_mark_processed(webhook_event_id, error: str = None) -> None:
    """Mark a logged webhook delivery as processed (or failed with
    `error` set). Best-effort — failures here are logged but never
    raised, since the business-logic outcome already happened."""
    try:
        get_supabase().rpc("webhook_event_mark_processed", {
            "p_id": str(webhook_event_id),
            "p_error": error,
        }).execute()
    except Exception as e:
        current_app.logger.error(f"webhook_event_mark_processed({webhook_event_id}): {e}")
