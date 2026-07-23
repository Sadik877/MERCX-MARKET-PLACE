"""
Real payment-gateway integrations: refunds (money back to a buyer's
original payment source) and transfers/payouts (money out to a
seller's bank/mobile-money/PayPal-style destination).

This module is intentionally separate from `utils/supabase_client.py`:
the Postgres RPCs in migrations/002_escrow_system.sql move money
between *internal* ledger rows (wallet balance, escrow holds,
payout_requests) — they have no way to call out to Stripe/Paystack/
Flutterwave over HTTP. This module makes those outbound HTTP calls;
the blueprint layer (blueprints/escrow.py) is responsible for calling
a gateway function FIRST and only mutating the internal ledger once
the gateway confirms (or, for refunds where the ledger is
wallet-only today, alongside it — see the call sites for the exact
ordering and why).

Every public function returns a plain dict:
    {"success": bool, "gateway_reference": str|None,
     "status": str, "raw": dict, "message": str}
and NEVER raises for an ordinary gateway-side decline/failure — it
only raises GatewayError for configuration problems (missing API
key) or transport failures (network error, malformed response),
which the caller should treat the same way as WalletOperationError:
log it, tell the user, and leave the internal ledger untouched.
"""
import hashlib
import time
import requests
from flask import current_app


class GatewayError(Exception):
    """Raised for configuration or transport failures — never for an
    ordinary gateway-side decline (that comes back as success=False
    in the returned dict instead)."""
    pass


def _cfg(key, required=True):
    val = current_app.config.get(key, "")
    if required and not val:
        raise GatewayError(f"{key} is not configured")
    return val


# ── Stripe ──────────────────────────────────────────────────────

def stripe_create_refund(payment_reference: str, amount: float = None,
                          reason: str = None) -> dict:
    """Refund a Stripe PaymentIntent (or Charge id) back to the
    buyer's original card/payment method. `amount` in dollars; omit
    to refund the full original amount."""
    import stripe
    stripe.api_key = _cfg("STRIPE_SECRET_KEY")
    try:
        params = {"payment_intent": payment_reference}
        if amount is not None:
            params["amount"] = int(round(amount * 100))
        if reason in ("duplicate", "fraudulent", "requested_by_customer"):
            params["reason"] = reason
        refund = stripe.Refund.create(**params)
        return {
            "success": refund.status in ("succeeded", "pending"),
            "gateway_reference": refund.id,
            "status": refund.status,
            "raw": refund.to_dict() if hasattr(refund, "to_dict") else dict(refund),
            "message": f"Stripe refund {refund.status}",
        }
    except stripe.error.StripeError as e:
        current_app.logger.error(f"stripe_create_refund failed: {e}")
        return {"success": False, "gateway_reference": None, "status": "failed",
                "raw": {}, "message": str(e)}
    except Exception as e:
        raise GatewayError(f"Stripe refund transport error: {e}") from e


def stripe_create_transfer(destination_account_id: str, amount: float,
                            currency: str = "usd", description: str = None) -> dict:
    """Pay out a seller's released escrow earnings via Stripe Connect
    (destination is a connected account id, e.g. 'acct_...'). Requires
    the seller to have a connected Stripe account on file — sellers
    without one should use a different `method` on their payout
    request (bank_transfer via Paystack/Flutterwave, etc.)."""
    import stripe
    stripe.api_key = _cfg("STRIPE_SECRET_KEY")
    try:
        transfer = stripe.Transfer.create(
            amount=int(round(amount * 100)),
            currency=currency,
            destination=destination_account_id,
            description=description or "MercX seller payout",
        )
        return {
            "success": True,
            "gateway_reference": transfer.id,
            "status": "paid",
            "raw": transfer.to_dict() if hasattr(transfer, "to_dict") else dict(transfer),
            "message": "Stripe transfer completed",
        }
    except stripe.error.StripeError as e:
        current_app.logger.error(f"stripe_create_transfer failed: {e}")
        return {"success": False, "gateway_reference": None, "status": "failed",
                "raw": {}, "message": str(e)}
    except Exception as e:
        raise GatewayError(f"Stripe transfer transport error: {e}") from e


# ── Paystack ────────────────────────────────────────────────────

_PAYSTACK_BASE = "https://api.paystack.co"


def _paystack_headers():
    return {"Authorization": f"Bearer {_cfg('PAYSTACK_SECRET_KEY')}",
            "Content-Type": "application/json"}


def paystack_create_refund(payment_reference: str, amount: float = None) -> dict:
    """Refund a Paystack transaction back to the buyer's original
    payment source."""
    try:
        payload = {"transaction": payment_reference}
        if amount is not None:
            payload["amount"] = int(round(amount * 100))
        resp = requests.post(f"{_PAYSTACK_BASE}/refund", json=payload,
                              headers=_paystack_headers(), timeout=20)
        data = resp.json()
        ok = resp.status_code in (200, 201) and data.get("status") is True
        ref = (data.get("data") or {}).get("id")
        return {
            "success": ok,
            "gateway_reference": str(ref) if ref else None,
            "status": (data.get("data") or {}).get("status", "failed" if not ok else "pending"),
            "raw": data,
            "message": data.get("message", "Paystack refund processed" if ok else "Paystack refund failed"),
        }
    except requests.RequestException as e:
        raise GatewayError(f"Paystack refund transport error: {e}") from e


def paystack_create_transfer_recipient(name: str, account_number: str,
                                        bank_code: str, currency: str = "NGN") -> dict:
    """Register a seller's bank account as a Paystack transfer
    recipient. Returns {"success", "recipient_code", "raw"}."""
    try:
        payload = {"type": "nuban", "name": name, "account_number": account_number,
                   "bank_code": bank_code, "currency": currency}
        resp = requests.post(f"{_PAYSTACK_BASE}/transferrecipient", json=payload,
                              headers=_paystack_headers(), timeout=20)
        data = resp.json()
        ok = resp.status_code in (200, 201) and data.get("status") is True
        return {
            "success": ok,
            "recipient_code": (data.get("data") or {}).get("recipient_code"),
            "raw": data,
            "message": data.get("message", ""),
        }
    except requests.RequestException as e:
        raise GatewayError(f"Paystack recipient transport error: {e}") from e


def paystack_create_transfer(recipient_code: str, amount: float,
                              reason: str = None, reference: str = None) -> dict:
    """Send a payout to a previously-registered Paystack transfer
    recipient."""
    try:
        payload = {
            "source": "balance",
            "amount": int(round(amount * 100)),
            "recipient": recipient_code,
            "reason": reason or "MercX seller payout",
            "reference": reference or f"MXPYO{int(time.time())}",
        }
        resp = requests.post(f"{_PAYSTACK_BASE}/transfer", json=payload,
                              headers=_paystack_headers(), timeout=20)
        data = resp.json()
        ok = resp.status_code in (200, 201) and data.get("status") is True
        tdata = data.get("data") or {}
        return {
            "success": ok,
            "gateway_reference": str(tdata.get("transfer_code") or tdata.get("reference") or ""),
            "status": tdata.get("status", "failed" if not ok else "pending"),
            "raw": data,
            "message": data.get("message", "Paystack transfer initiated" if ok else "Paystack transfer failed"),
        }
    except requests.RequestException as e:
        raise GatewayError(f"Paystack transfer transport error: {e}") from e


# ── Flutterwave ─────────────────────────────────────────────────

_FLW_BASE = "https://api.flutterwave.com/v3"


def _flw_headers():
    return {"Authorization": f"Bearer {_cfg('FLUTTERWAVE_SECRET_KEY')}",
            "Content-Type": "application/json"}


def flutterwave_create_refund(transaction_id: str, amount: float = None) -> dict:
    """Refund a Flutterwave transaction (transaction_id is FLW's
    numeric transaction id, returned at charge time) back to the
    buyer's original payment source."""
    try:
        payload = {}
        if amount is not None:
            payload["amount"] = amount
        resp = requests.post(f"{_FLW_BASE}/transactions/{transaction_id}/refund",
                              json=payload, headers=_flw_headers(), timeout=20)
        data = resp.json()
        ok = resp.status_code in (200, 201) and data.get("status") == "success"
        rdata = data.get("data") or {}
        return {
            "success": ok,
            "gateway_reference": str(rdata.get("id")) if rdata.get("id") else None,
            "status": rdata.get("status", "failed" if not ok else "pending"),
            "raw": data,
            "message": data.get("message", "Flutterwave refund processed" if ok else "Flutterwave refund failed"),
        }
    except requests.RequestException as e:
        raise GatewayError(f"Flutterwave refund transport error: {e}") from e


def flutterwave_create_transfer(account_bank: str, account_number: str,
                                 amount: float, currency: str = "NGN",
                                 narration: str = None, reference: str = None) -> dict:
    """Send a payout directly to a seller's bank account via
    Flutterwave Transfers (no separate recipient-registration step
    required, unlike Paystack)."""
    try:
        payload = {
            "account_bank": account_bank,
            "account_number": account_number,
            "amount": amount,
            "currency": currency,
            "narration": narration or "MercX seller payout",
            "reference": reference or f"MXPYO{int(time.time())}",
        }
        resp = requests.post(f"{_FLW_BASE}/transfers", json=payload,
                              headers=_flw_headers(), timeout=20)
        data = resp.json()
        ok = resp.status_code in (200, 201) and data.get("status") == "success"
        tdata = data.get("data") or {}
        return {
            "success": ok,
            "gateway_reference": str(tdata.get("id")) if tdata.get("id") else None,
            "status": tdata.get("status", "failed" if not ok else "pending"),
            "raw": data,
            "message": data.get("message", "Flutterwave transfer initiated" if ok else "Flutterwave transfer failed"),
        }
    except requests.RequestException as e:
        raise GatewayError(f"Flutterwave transfer transport error: {e}") from e


# ── Unified dispatch helpers ────────────────────────────────────
#
# These are what blueprints/escrow.py actually calls. They map a
# payout_request's `method` / a dispute's original `payment_method`
# to the right gateway function, keeping gateway-specific parameter
# shapes (recipient_code vs account_bank vs destination account id)
# out of the blueprint layer entirely.

def gateway_refund(payment_method: str, payment_reference: str,
                    amount: float = None) -> dict:
    """Refund the buyer's ORIGINAL payment source. `payment_method`
    is whatever was stored on the order/escrow row at checkout
    ('stripe', 'paystack', 'flutterwave', or 'wallet'). For 'wallet'
    there is nothing to call out to — the refund is already fully
    handled by the internal ledger (escrow_resolve_dispute /
    wallet_credit) — so this returns a no-op success so callers can
    treat every payment method uniformly."""
    if payment_method == "stripe":
        return stripe_create_refund(payment_reference, amount)
    if payment_method == "paystack":
        return paystack_create_refund(payment_reference, amount)
    if payment_method == "flutterwave":
        return flutterwave_create_refund(payment_reference, amount)
    return {"success": True, "gateway_reference": None, "status": "internal",
            "raw": {}, "message": "Wallet-funded order — refunded on the internal ledger only, no gateway call needed."}


def gateway_payout(method: str, destination: dict, amount: float,
                    currency: str = "usd", description: str = None) -> dict:
    """Send money out to a seller for an approved payout_request.
    `method` and `destination` come straight from the payout_requests
    row (see migrations/002_escrow_system.sql / seller_payout_accounts
    in migrations/003). Supported methods:
      - 'stripe'      destination = {"stripe_account_id": "acct_..."}
      - 'paystack'    destination = {"account_name","account_number","bank_code"}
                      (currency defaults to NGN if not overridden)
      - 'flutterwave' destination = {"account_bank","account_number"}
                      (account_bank is FLW's numeric bank code)
      - anything else (e.g. 'bank_transfer', 'paypal', 'crypto') is
        treated as a manual/offline payout: no gateway is configured
        for it yet, so this returns success=False with a clear
        message telling the admin to process it manually and record
        the reference by hand.
    """
    method = (method or "").lower()

    if method == "stripe":
        acct = destination.get("stripe_account_id")
        if not acct:
            return {"success": False, "gateway_reference": None, "status": "failed",
                    "raw": {}, "message": "No Stripe connected account on file for this seller."}
        return stripe_create_transfer(acct, amount, currency=currency if currency != "usd" else "usd",
                                       description=description)

    if method == "paystack":
        recipient = destination.get("recipient_code")
        if not recipient:
            reg = paystack_create_transfer_recipient(
                name=destination.get("account_name", "Seller"),
                account_number=destination.get("account_number", ""),
                bank_code=destination.get("bank_code", ""),
                currency=destination.get("currency", "NGN"),
            )
            if not reg.get("success"):
                return {"success": False, "gateway_reference": None, "status": "failed",
                        "raw": reg.get("raw", {}),
                        "message": reg.get("message") or "Could not register Paystack transfer recipient."}
            recipient = reg["recipient_code"]
        return paystack_create_transfer(recipient, amount, reason=description)

    if method == "flutterwave":
        return flutterwave_create_transfer(
            account_bank=destination.get("account_bank", ""),
            account_number=destination.get("account_number", ""),
            amount=amount,
            currency=destination.get("currency", "NGN"),
            narration=description,
        )

    return {"success": False, "gateway_reference": None, "status": "manual",
            "raw": {}, "message": f"No gateway configured for method '{method}'. "
                                   f"Process this payout manually, then record the "
                                   f"gateway/bank reference on the approval form."}
