"""
Escrow lifecycle routes.

Buyer Pays -> Funds Held -> Seller Delivers -> Buyer Confirms -> Funds Released
                                                       (or Automatic Release)

Dispute at any point before release -> Freeze Funds -> Admin Decision
                                     -> Refund Buyer / Release Seller / Partial Refund

This blueprint owns the buyer-facing "confirm receipt" / "open dispute"
actions, the dispute message thread, the admin dispute-resolution
screen, and seller payout requests. It deliberately does NOT touch
`orders.status` bookkeeping beyond what's needed to reflect escrow
state in the UI — `escrow_transactions.status` is the source of truth
for where the money actually is.
"""
from datetime import datetime, timezone
from flask import Blueprint, render_template, redirect, url_for, request, session, flash, current_app, jsonify

from utils.supabase_client import (
    db_select, db_insert, db_update, db_delete,
    escrow_release, escrow_open_dispute, escrow_resolve_dispute,
    escrow_auto_release_due, payout_request_approve, payout_request_reject,
    WalletOperationError,
)
from utils.decorators import login_required, seller_required, admin_required
from utils.helpers import generate_reference, log_audit, notify_user
from utils.gateways import gateway_payout, gateway_refund, GatewayError
from utils.email import (
    send_escrow_released, send_dispute_opened, send_dispute_message_notification,
    send_dispute_resolved, send_payout_status,
)

escrow_bp = Blueprint("escrow", __name__)


def _user_email(user_id):
    u = db_select("users", "email,username", filters={"id": user_id}, single=True)
    return u or {}


def _safe_email(fn, *args, **kwargs):
    """Email delivery must never break a financial flow — SMTP may
    not even be configured (see utils/email.send_email's own
    MAIL_USERNAME check). Swallow and log any failure here."""
    try:
        fn(*args, **kwargs)
    except Exception:
        current_app.logger.error(f"escrow email {fn.__name__} failed", exc_info=True)


def _get_escrow_for_order(order_id):
    return db_select("escrow_transactions", "*", filters={"order_id": order_id}, single=True)


# ── Buyer: confirm receipt (manual release) ───────────────────────

@escrow_bp.route("/orders/<order_id>/confirm", methods=["POST"])
@login_required
def confirm_receipt(order_id):
    uid = session["user_id"]
    order = db_select("orders", "id,buyer_id,order_number", filters={"id": order_id}, single=True)
    if not order or order["buyer_id"] != uid:
        flash("Order not found.", "danger")
        return redirect(url_for("dashboard.purchases"))

    esc = _get_escrow_for_order(order_id)
    if not esc:
        flash("Nothing to confirm for this order.", "warning")
        return redirect(url_for("dashboard.purchases"))

    if esc["status"] not in ("held", "delivered"):
        flash(f"This order's payment is already {esc['status']}.", "info")
        return redirect(url_for("dashboard.purchases"))

    try:
        result = escrow_release(esc["id"], actor_id=uid, reason="buyer_confirmed")
    except WalletOperationError as e:
        current_app.logger.error(f"confirm_receipt release failed for {order_id}: {e}")
        flash("We couldn't release the funds right now. Please try again.", "danger")
        return redirect(url_for("dashboard.purchases"))

    if not result.get("success"):
        flash(result.get("message") or "Unable to confirm receipt.", "warning")
        return redirect(url_for("dashboard.purchases"))

    log_audit(uid, "escrow_confirm_receipt", resource_type="order", resource_id=order_id)

    seller = _user_email(esc["seller_id"])
    notify_user(esc["seller_id"], "escrow_released", "check-circle",
                f"Payment Released: ${float(esc['amount']):.2f}",
                f"The buyer confirmed receipt of order {order['order_number']}. Funds have been released to your wallet.",
                link="/seller/withdrawals")
    if seller.get("email"):
        _safe_email(send_escrow_released, seller["email"], seller.get("username", "there"),
                    order["order_number"], float(esc.get("seller_earnings") or esc["amount"]),
                    "buyer_confirmed", url_for("seller.withdrawals", _external=True))

    flash("Thanks! The seller has been paid out.", "success")
    return redirect(url_for("dashboard.purchases"))


# ── Buyer or seller: open a dispute ────────────────────────────────

DISPUTE_REASONS = {
    "not_as_described", "not_delivered", "quality_issue",
    "unauthorized_charge", "seller_unresponsive", "other",
}


@escrow_bp.route("/orders/<order_id>/dispute", methods=["POST"])
@login_required
def open_dispute(order_id):
    uid = session["user_id"]
    order = db_select("orders", "id,buyer_id,seller_id,order_number",
                      filters={"id": order_id}, single=True)
    if not order or uid not in (order["buyer_id"], order["seller_id"]):
        flash("Order not found.", "danger")
        return redirect(url_for("dashboard.index"))

    esc = _get_escrow_for_order(order_id)
    if not esc:
        flash("This order has no escrow to dispute.", "warning")
        return redirect(url_for("dashboard.index"))

    reason      = request.form.get("reason", "other").strip()
    description = request.form.get("description", "").strip()[:2000]
    if reason not in DISPUTE_REASONS:
        reason = "other"

    try:
        result = escrow_open_dispute(esc["id"], raised_by=uid, reason=reason, description=description)
    except WalletOperationError as e:
        current_app.logger.error(f"open_dispute failed for {order_id}: {e}")
        flash("We couldn't open a dispute right now. Please try again.", "danger")
        return redirect(url_for("dashboard.index"))

    if not result.get("success"):
        flash(result.get("message") or "Unable to open a dispute.", "warning")
        return redirect(url_for("dashboard.index"))

    db_update("orders", {"status": "disputed"}, {"id": order_id})
    log_audit(uid, "escrow_open_dispute", resource_type="order", resource_id=order_id,
              details={"reason": reason})

    against_id = order["seller_id"] if uid == order["buyer_id"] else order["buyer_id"]
    dispute_url = url_for("escrow.dispute_detail", dispute_id=result["dispute_id"], _external=True)
    against = _user_email(against_id)
    notify_user(against_id, "dispute_opened", "alert-triangle",
                f"Dispute Opened: Order {order['order_number']}",
                f"A dispute was opened on order {order['order_number']}. Funds are frozen pending admin review.",
                link=f"/disputes/{result['dispute_id']}")
    if against.get("email"):
        _safe_email(send_dispute_opened, against["email"], against.get("username", "there"),
                    order["order_number"], reason, True, dispute_url)

    flash("Dispute opened. Funds are frozen until an admin reviews it.", "success")
    return redirect(url_for("escrow.dispute_detail", dispute_id=result["dispute_id"]))


# ── Dispute thread (buyer, seller, admin) ─────────────────────────

@escrow_bp.route("/disputes/<dispute_id>")
@login_required
def dispute_detail(dispute_id):
    uid = session["user_id"]
    dispute = db_select("disputes", "*", filters={"id": dispute_id}, single=True)
    if not dispute:
        flash("Dispute not found.", "danger")
        return redirect(url_for("dashboard.index"))

    is_admin = session.get("role") in ("admin", "moderator")
    if not is_admin and uid not in (dispute["raised_by"], dispute["against_id"]):
        flash("You don't have access to this dispute.", "danger")
        return redirect(url_for("dashboard.index"))

    messages = db_select("dispute_messages", "*", filters={"dispute_id": dispute_id}, order="created_at")
    if not is_admin:
        messages = [m for m in messages if not m.get("is_admin_note")]

    escrow = db_select("escrow_transactions", "*", filters={"id": dispute["escrow_transaction_id"]}, single=True)
    return render_template("dashboard/dispute_detail.html",
                          dispute=dispute, messages=messages, escrow=escrow, is_admin=is_admin)


@escrow_bp.route("/disputes/<dispute_id>/message", methods=["POST"])
@login_required
def dispute_message(dispute_id):
    uid = session["user_id"]
    dispute = db_select("disputes", "*", filters={"id": dispute_id}, single=True)
    if not dispute:
        return jsonify({"error": "Dispute not found"}), 404

    is_admin = session.get("role") in ("admin", "moderator")
    if not is_admin and uid not in (dispute["raised_by"], dispute["against_id"]):
        return jsonify({"error": "Unauthorized"}), 403

    text = (request.json.get("message") if request.is_json else request.form.get("message", "")).strip()
    if not text:
        return jsonify({"error": "Message cannot be empty"}), 400

    is_note = is_admin and (request.json.get("internal_note") if request.is_json
                            else request.form.get("internal_note")) in ("1", "true", True)

    msg = db_insert("dispute_messages", {
        "dispute_id":    dispute_id,
        "sender_id":     uid,
        "message":       text[:2000],
        "is_admin_note": bool(is_note),
    })
    if dispute["status"] == "open" and is_admin:
        db_update("disputes", {"status": "under_review"}, {"id": dispute_id})

    if not is_note:
        recipients = {dispute["raised_by"], dispute["against_id"]} - {uid}
        order = db_select("orders", "order_number", filters={"id": dispute["order_id"]}, single=True)
        sender_label = "Support" if is_admin else "The other party"
        dispute_url = url_for("escrow.dispute_detail", dispute_id=dispute_id, _external=True)
        for rid in recipients:
            notify_user(rid, "dispute_message", "message-circle",
                        "New Dispute Message",
                        f"{sender_label} replied on the dispute for order {order['order_number'] if order else ''}.",
                        link=f"/disputes/{dispute_id}")
            r = _user_email(rid)
            if r.get("email"):
                _safe_email(send_dispute_message_notification, r["email"], r.get("username", "there"),
                            order["order_number"] if order else "", sender_label, dispute_url)

    return jsonify({"ok": True, "message": msg})


# ── Admin: resolve a dispute ───────────────────────────────────────

@escrow_bp.route("/admin/disputes/<dispute_id>/resolve", methods=["POST"])
@admin_required
def admin_resolve_dispute(dispute_id):
    admin_id   = session["user_id"]
    resolution = request.form.get("resolution", "")
    amount     = request.form.get("refund_amount", "")
    note       = request.form.get("note", "").strip()[:1000]

    refund_amount = None
    if resolution == "partial_refund":
        try:
            refund_amount = float(amount)
            assert refund_amount > 0
        except (ValueError, AssertionError, TypeError):
            flash("A valid partial refund amount is required.", "danger")
            return redirect(url_for("escrow.dispute_detail", dispute_id=dispute_id))

    dispute_row = db_select("disputes", "*", filters={"id": dispute_id}, single=True)
    if not dispute_row:
        flash("Dispute not found.", "danger")
        return redirect(url_for("admin.disputes"))

    esc = db_select("escrow_transactions", "*",
                    filters={"id": dispute_row["escrow_transaction_id"]}, single=True)

    # GATEWAY REFUND: if the buyer paid in through a real gateway
    # (not the wallet — see checkout(), which today only offers
    # 'wallet' as payment_method, but this stays generic for
    # non-wallet payment methods added later), send the money back to
    # their original card/account BEFORE touching the internal
    # ledger. If the gateway call fails outright (not just declines —
    # see GatewayError vs a {"success": False} result), we stop here
    # rather than resolve the dispute on the ledger while the buyer's
    # real money never moved.
    gateway_result = None
    if esc and resolution in ("refund_buyer", "partial_refund"):
        refund_amt = refund_amount if resolution == "partial_refund" else None
        try:
            gateway_result = gateway_refund(esc["payment_method"], esc["payment_reference"], refund_amt)
        except GatewayError as e:
            current_app.logger.error(f"admin_resolve_dispute gateway refund failed for {dispute_id}: {e}")
            flash("The gateway refund could not be sent. Nothing was changed — please try again or refund manually.", "danger")
            return redirect(url_for("escrow.dispute_detail", dispute_id=dispute_id))
        if not gateway_result.get("success"):
            flash(f"Gateway refund failed: {gateway_result.get('message')}. Nothing was changed.", "danger")
            return redirect(url_for("escrow.dispute_detail", dispute_id=dispute_id))

    try:
        result = escrow_resolve_dispute(dispute_id, admin_id, resolution, refund_amount, note)
    except WalletOperationError as e:
        current_app.logger.error(f"admin_resolve_dispute failed for {dispute_id}: {e}")
        flash("Resolution failed while moving funds. Please try again.", "danger")
        return redirect(url_for("escrow.dispute_detail", dispute_id=dispute_id))

    if not result.get("success"):
        flash(result.get("message") or "Unable to resolve dispute.", "warning")
        return redirect(url_for("escrow.dispute_detail", dispute_id=dispute_id))

    # NOTE: orders.status only allows ('pending','processing','completed',
    # 'cancelled','refunded','disputed') per schema.sql — there is no
    # 'partial_refunded' value, so a partial refund is recorded on the
    # order as 'refunded' (the authoritative partial-vs-full amount
    # lives on escrow_transactions.refunded_amount / disputes.resolution,
    # not on orders.status).
    order_status = {"refund_buyer": "refunded", "release_seller": "completed",
                    "partial_refund": "refunded"}.get(resolution, "completed")
    db_update("orders", {"status": order_status}, {"id": dispute_row["order_id"]})

    log_audit(admin_id, "dispute_resolve", resource_type="dispute", resource_id=dispute_id,
              details={"resolution": resolution, "amount": refund_amount,
                       "gateway_reference": (gateway_result or {}).get("gateway_reference")})

    order = db_select("orders", "order_number", filters={"id": dispute_row["order_id"]}, single=True)
    dispute_url = url_for("escrow.dispute_detail", dispute_id=dispute_id, _external=True)
    order_number = order["order_number"] if order else ""

    for rid in {dispute_row["raised_by"], dispute_row["against_id"]}:
        u = _user_email(rid)
        notify_user(rid, "dispute_resolved", "flag",
                    f"Dispute Resolved: Order {order_number}",
                    f"Resolution: {resolution.replace('_', ' ')}.",
                    link=f"/disputes/{dispute_id}")
        if u.get("email"):
            _safe_email(send_dispute_resolved, u["email"], u.get("username", "there"),
                        order_number, resolution, refund_amount or 0, note, dispute_url)

    flash(f"Dispute resolved: {resolution.replace('_', ' ')}.", "success")
    return redirect(url_for("admin.disputes"))


# ── Admin / scheduler: trigger auto-release sweep ─────────────────
#
# No background worker exists in this codebase yet, so auto-release
# is triggered by (a) an admin clicking a button, or (b) an external
# scheduler (cron, Render Cron Job, etc.) hitting this endpoint with
# a shared secret. It is always safe to call repeatedly.

@escrow_bp.route("/admin/escrow/auto-release", methods=["POST"])
def run_auto_release():
    is_admin_session = session.get("role") in ("admin", "moderator")
    secret_ok = False
    configured_secret = current_app.config.get("CRON_SECRET", "")
    if configured_secret and request.headers.get("X-Cron-Secret") == configured_secret:
        secret_ok = True

    if not (is_admin_session or secret_ok):
        return jsonify({"error": "Unauthorized"}), 403

    try:
        results = escrow_auto_release_due()
    except WalletOperationError as e:
        current_app.logger.error(f"auto-release sweep failed: {e}")
        return jsonify({"error": "Auto-release sweep failed"}), 500

    released = 0
    for r in results:
        if not r.get("success"):
            continue
        released += 1
        esc = db_select("escrow_transactions", "*", filters={"id": r.get("escrow_id")}, single=True)
        if not esc:
            continue
        order = db_select("orders", "order_number", filters={"id": esc["order_id"]}, single=True)
        seller = _user_email(esc["seller_id"])
        notify_user(esc["seller_id"], "escrow_released", "check-circle",
                    f"Payment Auto-Released: ${float(esc['amount']):.2f}",
                    f"The review window passed for order {order['order_number'] if order else ''}. "
                    f"Funds have been automatically released to your wallet.",
                    link="/seller/withdrawals")
        if seller.get("email"):
            _safe_email(send_escrow_released, seller["email"], seller.get("username", "there"),
                        order["order_number"] if order else "", float(esc.get("seller_earnings") or esc["amount"]),
                        "auto_release", url_for("seller.withdrawals", _external=True))

    return jsonify({"processed": len(results), "released": released, "results": results})


# ── Seller: request a payout of released earnings ─────────────────

@escrow_bp.route("/seller/payouts/request", methods=["POST"])
@seller_required
def request_payout():
    sid    = session["user_id"]
    amount = request.form.get("amount", "")

    # A saved payout account (see seller.payout_account) takes
    # priority over hand-typed fields — if the seller picked one,
    # reuse its method/details exactly so the same account can later
    # be dispatched through utils.gateways.gateway_payout() without
    # any reshaping.
    account_id = request.form.get("account_id", "").strip()
    account = db_select("seller_payout_accounts", "*",
                        filters={"id": account_id, "seller_id": sid}, single=True) if account_id else None

    if account:
        method      = account["method"]
        destination = account["details"]
    else:
        method = request.form.get("method", "bank_transfer").strip()
        destination = {
            "account_name":   request.form.get("account_name", "").strip()[:200],
            "account_number": request.form.get("account_number", "").strip()[:100],
            "bank_name":      request.form.get("bank_name", "").strip()[:200],
        }

    try:
        amount = float(amount)
        cfg = current_app.config
        assert cfg.get("MIN_WITHDRAWAL", 10) <= amount <= cfg.get("MAX_WITHDRAWAL", 10000)
    except (ValueError, AssertionError, TypeError):
        flash("Invalid payout amount.", "danger")
        return redirect(url_for("seller.withdrawals"))

    user = db_select("users", "balance", filters={"id": sid}, single=True)
    if not user or float(user.get("balance", 0)) < amount:
        flash("Insufficient available balance for this payout.", "danger")
        return redirect(url_for("seller.withdrawals"))

    db_insert("payout_requests", {
        "seller_id":   sid,
        "amount":      amount,
        "method":      method,
        "destination": destination,
        "status":      "pending",
        "reference":   generate_reference("PYO"),
    })
    log_audit(sid, "payout_request", details={"amount": amount, "method": method})
    flash("Payout request submitted. It will be reviewed within 1–2 business days.", "success")
    return redirect(url_for("seller.withdrawals"))


# ── Admin: approve / reject a payout request ──────────────────────

@escrow_bp.route("/admin/payouts/<payout_id>/approve", methods=["POST"])
@admin_required
def admin_approve_payout(payout_id):
    admin_id = session["user_id"]
    send_via_gateway = request.form.get("send_via_gateway") in ("1", "true", "on")
    gateway_reference = request.form.get("gateway_reference", "").strip() or None

    payout = db_select("payout_requests", "*", filters={"id": payout_id}, single=True)
    if not payout:
        flash("Payout request not found.", "danger")
        return redirect(url_for("admin.payouts"))

    # REAL GATEWAY DISPATCH: send the actual money first. Only once
    # the gateway confirms do we call payout_request_approve(), which
    # is what debits the seller's internal wallet balance and writes
    # the payout_history row — so a failed gateway call never leaves
    # the ledger out of sync with what actually moved.
    if send_via_gateway:
        try:
            gw = gateway_payout(payout["method"], payout.get("destination") or {},
                                float(payout["amount"]), description=f"MercX payout {payout['reference']}")
        except GatewayError as e:
            current_app.logger.error(f"admin_approve_payout gateway dispatch failed for {payout_id}: {e}")
            flash("Gateway payout could not be sent. The request is still pending — nothing was charged.", "danger")
            return redirect(url_for("admin.payouts"))

        if not gw.get("success"):
            flash(f"Gateway payout failed: {gw.get('message')}. The request is still pending.", "warning")
            return redirect(url_for("admin.payouts"))

        gateway_reference = gw.get("gateway_reference") or gateway_reference

    try:
        result = payout_request_approve(payout_id, admin_id, gateway_reference)
    except WalletOperationError as e:
        current_app.logger.error(f"payout approve failed for {payout_id}: {e}")
        flash("Approval failed. Please try again.", "danger")
        return redirect(url_for("admin.payouts"))

    flash(result.get("message") or "Processed.", "success" if result.get("success") else "warning")
    log_audit(admin_id, "payout_approve", resource_type="payout_request", resource_id=payout_id,
              details={"gateway_reference": gateway_reference, "via_gateway": send_via_gateway})

    if result.get("success"):
        seller = _user_email(payout["seller_id"])
        notify_user(payout["seller_id"], "payout_approved", "trending-down",
                    f"Payout Approved: ${float(payout['amount']):.2f}",
                    "Your seller payout has been processed.", link="/seller/withdrawals")
        if seller.get("email"):
            _safe_email(send_payout_status, seller["email"], seller.get("username", "there"),
                        float(payout["amount"]), "paid", gateway_reference)

    return redirect(url_for("admin.payouts"))


@escrow_bp.route("/admin/payouts/<payout_id>/reject", methods=["POST"])
@admin_required
def admin_reject_payout(payout_id):
    admin_id = session["user_id"]
    note = request.form.get("note", "").strip()[:500]

    payout = db_select("payout_requests", "*", filters={"id": payout_id}, single=True)

    try:
        result = payout_request_reject(payout_id, admin_id, note)
    except WalletOperationError as e:
        current_app.logger.error(f"payout reject failed for {payout_id}: {e}")
        flash("Rejection failed. Please try again.", "danger")
        return redirect(url_for("admin.payouts"))

    flash(result.get("message") or "Processed.", "success" if result.get("success") else "warning")
    log_audit(admin_id, "payout_reject", resource_type="payout_request", resource_id=payout_id,
              details={"note": note})

    if result.get("success") and payout:
        seller = _user_email(payout["seller_id"])
        notify_user(payout["seller_id"], "payout_rejected", "x-circle",
                    f"Payout Rejected: ${float(payout['amount']):.2f}",
                    note or "Your payout request was rejected.", link="/seller/withdrawals")
        if seller.get("email"):
            _safe_email(send_payout_status, seller["email"], seller.get("username", "there"),
                        float(payout["amount"]), "rejected", note=note)

    return redirect(url_for("admin.payouts"))


# ── Seller: manage saved payout accounts ───────────────────────────

PAYOUT_METHODS = {"bank_transfer", "paystack", "flutterwave", "stripe", "paypal", "crypto"}


@escrow_bp.route("/seller/payout-account")
@seller_required
def payout_account():
    sid = session["user_id"]
    accounts = db_select("seller_payout_accounts", "*", filters={"seller_id": sid}, order="-is_default")
    payouts = db_select("payout_requests", "*", filters={"seller_id": sid}, order="-requested_at")
    return render_template("seller/payout_account.html", accounts=accounts, payouts=payouts,
                          methods=sorted(PAYOUT_METHODS))


@escrow_bp.route("/seller/payout-account/save", methods=["POST"])
@seller_required
def payout_account_save():
    sid    = session["user_id"]
    method = request.form.get("method", "").strip()
    label  = request.form.get("label", "").strip()[:100]
    make_default = request.form.get("is_default") in ("1", "true", "on")

    if method not in PAYOUT_METHODS:
        flash("Invalid payout method.", "danger")
        return redirect(url_for("escrow.payout_account"))

    field_map = {
        "bank_transfer": ["account_name", "account_number", "bank_name"],
        "paystack":      ["account_name", "account_number", "bank_code", "bank_name"],
        "flutterwave":   ["account_name", "account_number", "account_bank", "bank_name"],
        "stripe":        ["stripe_account_id"],
        "paypal":        ["paypal_email"],
        "crypto":        ["network", "address"],
    }
    details = {f: request.form.get(f, "").strip()[:200] for f in field_map[method]}
    if not any(details.values()):
        flash("Please fill in the payout account details.", "danger")
        return redirect(url_for("escrow.payout_account"))

    if make_default:
        # Only one default account per seller (see the partial unique
        # index in migrations/003_seller_payout_accounts.sql) — clear
        # any existing default first so the insert doesn't violate it.
        existing_default = db_select("seller_payout_accounts", "id",
                                     filters={"seller_id": sid, "is_default": True})
        for row in existing_default:
            db_update("seller_payout_accounts", {"is_default": False}, {"id": row["id"]})

    db_insert("seller_payout_accounts", {
        "seller_id":  sid,
        "method":     method,
        "label":      label or method.replace("_", " ").title(),
        "details":    details,
        "is_default": make_default,
    })
    log_audit(sid, "payout_account_save", details={"method": method})
    flash("Payout account saved.", "success")
    return redirect(url_for("escrow.payout_account"))


@escrow_bp.route("/seller/payout-account/<account_id>/delete", methods=["POST"])
@seller_required
def payout_account_delete(account_id):
    sid = session["user_id"]
    account = db_select("seller_payout_accounts", "id",
                        filters={"id": account_id, "seller_id": sid}, single=True)
    if not account:
        flash("Payout account not found.", "danger")
        return redirect(url_for("escrow.payout_account"))
    db_delete("seller_payout_accounts", {"id": account_id})
    flash("Payout account removed.", "success")
    return redirect(url_for("escrow.payout_account"))


@escrow_bp.route("/seller/payout-account/<account_id>/make-default", methods=["POST"])
@seller_required
def payout_account_make_default(account_id):
    sid = session["user_id"]
    account = db_select("seller_payout_accounts", "id",
                        filters={"id": account_id, "seller_id": sid}, single=True)
    if not account:
        flash("Payout account not found.", "danger")
        return redirect(url_for("escrow.payout_account"))

    existing_default = db_select("seller_payout_accounts", "id",
                                 filters={"seller_id": sid, "is_default": True})
    for row in existing_default:
        db_update("seller_payout_accounts", {"is_default": False}, {"id": row["id"]})
    db_update("seller_payout_accounts", {"is_default": True}, {"id": account_id})
    flash("Default payout account updated.", "success")
    return redirect(url_for("escrow.payout_account"))
