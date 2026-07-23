import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import current_app, render_template_string


# ── HTML e-mail templates ─────────────────────────────────────

_BASE = """
<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body{font-family:'Inter',Arial,sans-serif;background:#080B14;margin:0;padding:0;color:#F8FAFC}
  .wrap{max-width:600px;margin:40px auto;background:#0F1724;border-radius:16px;overflow:hidden;border:1px solid rgba(124,58,237,.3)}
  .header{background:linear-gradient(135deg,#7C3AED,#06B6D4);padding:36px 40px;text-align:center}
  .header h1{margin:0;font-size:24px;color:#fff;font-weight:700;letter-spacing:-.5px}
  .header p{margin:8px 0 0;color:rgba(255,255,255,.8);font-size:14px}
  .body{padding:36px 40px}
  .body p{color:#94A3B8;line-height:1.7;margin:0 0 16px}
  .body h2{color:#F8FAFC;font-size:20px;margin:0 0 16px}
  .btn{display:inline-block;background:linear-gradient(135deg,#7C3AED,#8B5CF6);color:#fff !important;
    text-decoration:none;padding:14px 32px;border-radius:10px;font-weight:600;font-size:15px;margin:8px 0}
  .info-box{background:#1A2438;border:1px solid rgba(255,255,255,.06);border-radius:10px;
    padding:20px 24px;margin:20px 0}
  .info-box .label{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#7C3AED;font-weight:600}
  .info-box .value{font-size:18px;color:#F8FAFC;font-weight:700;margin-top:4px}
  .footer{background:#080B14;padding:24px 40px;text-align:center;border-top:1px solid rgba(255,255,255,.06)}
  .footer p{color:#4B5563;font-size:12px;margin:0}
  .divider{border:none;border-top:1px solid rgba(255,255,255,.06);margin:24px 0}
</style></head><body>
<div class="wrap">
  <div class="header">
    <h1>⚡ MercX Digital</h1>
    <p>The Premium Digital Marketplace</p>
  </div>
  <div class="body">{{ body|safe }}</div>
  <div class="footer"><p>© 2024 MercX Digital Marketplace · <a href="#" style="color:#7C3AED">Unsubscribe</a></p></div>
</div>
</body></html>
"""


def _render(body_html: str) -> str:
    return render_template_string(_BASE, body=body_html)


# ── SMTP send ─────────────────────────────────────────────────

def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send a single HTML email. Returns True on success."""
    cfg = current_app.config
    if not cfg.get("MAIL_USERNAME"):
        current_app.logger.warning("Email not configured — skipping send.")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["MAIL_DEFAULT_SENDER"]
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html"))

        ctx = ssl.create_default_context()
        with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"]) as srv:
            if cfg["MAIL_USE_TLS"]:
                srv.starttls(context=ctx)
            srv.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
            srv.sendmail(cfg["MAIL_DEFAULT_SENDER"], to, msg.as_string())
        return True
    except Exception as e:
        current_app.logger.error(f"send_email to {to}: {e}")
        return False


# ── Transactional email helpers ───────────────────────────────

def send_verification_email(to: str, username: str, verify_url: str) -> bool:
    body = f"""
    <h2>Verify Your Email</h2>
    <p>Hi <strong>{username}</strong>, welcome to MercX Digital Marketplace!</p>
    <p>Click the button below to verify your email address and activate your account.</p>
    <p style="text-align:center;margin:32px 0">
      <a href="{verify_url}" class="btn">Verify Email Address</a>
    </p>
    <p>This link expires in <strong>24 hours</strong>. If you didn't create an account, you can safely ignore this email.</p>
    <hr class="divider">
    <p style="font-size:13px;color:#4B5563">Or copy this URL: {verify_url}</p>
    """
    return send_email(to, "Verify your MercX Digital account", _render(body))


def send_password_reset_email(to: str, username: str, reset_url: str) -> bool:
    body = f"""
    <h2>Reset Your Password</h2>
    <p>Hi <strong>{username}</strong>, we received a request to reset your password.</p>
    <p style="text-align:center;margin:32px 0">
      <a href="{reset_url}" class="btn">Reset Password</a>
    </p>
    <p>This link expires in <strong>1 hour</strong>. If you didn't request this, ignore this email — your password won't change.</p>
    <hr class="divider">
    <p style="font-size:13px;color:#4B5563">Or copy this URL: {reset_url}</p>
    """
    return send_email(to, "Reset your MercX Digital password", _render(body))


def send_order_confirmation(to: str, username: str, order_number: str,
                             items: list, total: float, dashboard_url: str) -> bool:
    items_html = "".join(
        f'<div class="info-box"><div class="label">Product</div>'
        f'<div class="value">{i["title"]}</div>'
        f'<p style="margin:8px 0 0;color:#94A3B8">${i["price"]:.2f} × {i["qty"]}</p></div>'
        for i in items
    )
    body = f"""
    <h2>Order Confirmed! 🎉</h2>
    <p>Hi <strong>{username}</strong>, your order has been placed successfully.</p>
    <div class="info-box">
      <div class="label">Order Number</div>
      <div class="value">{order_number}</div>
    </div>
    {items_html}
    <div class="info-box">
      <div class="label">Total Paid</div>
      <div class="value">${total:.2f}</div>
    </div>
    <p>Your digital products are ready to download from your dashboard.</p>
    <p style="text-align:center;margin:32px 0">
      <a href="{dashboard_url}" class="btn">Download Your Products</a>
    </p>
    """
    return send_email(to, f"Order {order_number} Confirmed — MercX Digital", _render(body))


def send_sale_notification(to: str, seller_name: str, product_title: str,
                            amount: float, earnings: float, order_number: str) -> bool:
    body = f"""
    <h2>You Made a Sale! 💰</h2>
    <p>Hi <strong>{seller_name}</strong>, your product just sold on MercX Digital.</p>
    <div class="info-box">
      <div class="label">Product Sold</div>
      <div class="value">{product_title}</div>
    </div>
    <div class="info-box">
      <div class="label">Sale Amount</div>
      <div class="value">${amount:.2f}</div>
    </div>
    <div class="info-box">
      <div class="label">Your Earnings (after fee)</div>
      <div class="value" style="color:#10B981">${earnings:.2f}</div>
    </div>
    <p>Order <strong>{order_number}</strong> has been credited to your wallet.</p>
    """
    return send_email(to, f"Sale! {product_title} — MercX Digital", _render(body))


def send_deposit_confirmation(to: str, username: str, amount: float, reference: str) -> bool:
    body = f"""
    <h2>Deposit Successful ✅</h2>
    <p>Hi <strong>{username}</strong>, your wallet has been funded.</p>
    <div class="info-box">
      <div class="label">Amount Deposited</div>
      <div class="value">${amount:.2f}</div>
    </div>
    <div class="info-box">
      <div class="label">Reference</div>
      <div class="value" style="font-size:14px;font-family:monospace">{reference}</div>
    </div>
    <p>Your MercX wallet balance has been updated and is ready to use.</p>
    """
    return send_email(to, "Wallet Funded — MercX Digital", _render(body))


def send_withdrawal_processed(to: str, username: str, amount: float,
                               status: str, note: str = "") -> bool:
    status_color = "#10B981" if status == "approved" else "#EF4444"
    status_label = "Approved ✅" if status == "approved" else "Rejected ❌"
    body = f"""
    <h2>Withdrawal {status_label}</h2>
    <p>Hi <strong>{username}</strong>, your withdrawal request has been processed.</p>
    <div class="info-box">
      <div class="label">Amount</div>
      <div class="value">${amount:.2f}</div>
    </div>
    <div class="info-box">
      <div class="label">Status</div>
      <div class="value" style="color:{status_color}">{status_label}</div>
    </div>
    {"<div class='info-box'><div class='label'>Note from Admin</div><div class='value' style='font-size:15px'>"+note+"</div></div>" if note else ""}
    <p>If you have questions, please contact our support team.</p>
    """
    return send_email(to, f"Withdrawal {status_label} — MercX Digital", _render(body))


# ── Escrow lifecycle ────────────────────────────────────────────

def send_escrow_delivered(to: str, username: str, order_number: str,
                           auto_release_hours: int, confirm_url: str) -> bool:
    """Sent to the buyer the moment a seller marks an order delivered
    — this is what starts the auto-release countdown."""
    body = f"""
    <h2>Your Order Was Delivered 📦</h2>
    <p>Hi <strong>{username}</strong>, the seller has delivered order <strong>{order_number}</strong>.</p>
    <div class="info-box">
      <div class="label">Please Review &amp; Confirm</div>
      <div class="value" style="font-size:15px">You have {auto_release_hours} hours to confirm receipt or open a dispute.</div>
    </div>
    <p>If you don't take any action, funds will be automatically released to the seller after {auto_release_hours} hours.</p>
    <p style="text-align:center;margin:32px 0">
      <a href="{confirm_url}" class="btn">Review Your Order</a>
    </p>
    """
    return send_email(to, f"Order {order_number} Delivered — Please Confirm", _render(body))


def send_escrow_released(to: str, username: str, order_number: str,
                          amount: float, reason: str, wallet_url: str) -> bool:
    """Sent to the SELLER when escrow funds are released to them
    (buyer confirmation, admin dispute resolution, or auto-release)."""
    reason_label = {
        "buyer_confirmed": "the buyer confirmed receipt",
        "auto_release":    "the review window passed automatically",
        "dispute_resolved": "a dispute was resolved in your favor",
    }.get(reason, reason.replace("_", " "))
    body = f"""
    <h2>Funds Released to You 💰</h2>
    <p>Hi <strong>{username}</strong>, escrow funds for order <strong>{order_number}</strong> have been released to your wallet because {reason_label}.</p>
    <div class="info-box">
      <div class="label">Amount Released</div>
      <div class="value" style="color:#10B981">${amount:.2f}</div>
    </div>
    <p style="text-align:center;margin:32px 0">
      <a href="{wallet_url}" class="btn">View Wallet</a>
    </p>
    """
    return send_email(to, f"Funds Released — Order {order_number}", _render(body))


def send_dispute_opened(to: str, username: str, order_number: str,
                         reason: str, is_against_you: bool, dispute_url: str) -> bool:
    """Sent to both the counterparty (whoever didn't open it) and,
    separately, as a confirmation to whoever opened it."""
    heading = "A Dispute Was Opened Against This Order ⚠️" if is_against_you else "Dispute Opened ⚠️"
    intro = (f"A dispute has been opened on order <strong>{order_number}</strong>. Funds are frozen until an admin reviews it."
              if is_against_you else
              f"Your dispute on order <strong>{order_number}</strong> has been submitted and funds are now frozen.")
    body = f"""
    <h2>{heading}</h2>
    <p>Hi <strong>{username}</strong>, {intro}</p>
    <div class="info-box">
      <div class="label">Reason</div>
      <div class="value" style="font-size:15px">{reason.replace('_',' ').title()}</div>
    </div>
    <p style="text-align:center;margin:32px 0">
      <a href="{dispute_url}" class="btn">View Dispute</a>
    </p>
    """
    return send_email(to, f"Dispute Opened — Order {order_number}", _render(body))


def send_dispute_message_notification(to: str, username: str, order_number: str,
                                       sender_label: str, dispute_url: str) -> bool:
    body = f"""
    <h2>New Message in Your Dispute 💬</h2>
    <p>Hi <strong>{username}</strong>, {sender_label} replied on the dispute for order <strong>{order_number}</strong>.</p>
    <p style="text-align:center;margin:32px 0">
      <a href="{dispute_url}" class="btn">View Message</a>
    </p>
    """
    return send_email(to, f"New Dispute Message — Order {order_number}", _render(body))


def send_dispute_resolved(to: str, username: str, order_number: str,
                           resolution: str, amount: float, note: str, dispute_url: str) -> bool:
    label = {"refund_buyer": "Refunded to Buyer", "release_seller": "Released to Seller",
              "partial_refund": "Partially Refunded"}.get(resolution, resolution.replace("_", " ").title())
    body = f"""
    <h2>Dispute Resolved</h2>
    <p>Hi <strong>{username}</strong>, the dispute on order <strong>{order_number}</strong> has been resolved by our team.</p>
    <div class="info-box">
      <div class="label">Resolution</div>
      <div class="value">{label}</div>
    </div>
    {f'<div class="info-box"><div class="label">Amount</div><div class="value">${amount:.2f}</div></div>' if amount else ''}
    {f'<div class="info-box"><div class="label">Note from our team</div><div class="value" style="font-size:15px">{note}</div></div>' if note else ''}
    <p style="text-align:center;margin:32px 0">
      <a href="{dispute_url}" class="btn">View Details</a>
    </p>
    """
    return send_email(to, f"Dispute Resolved — Order {order_number}", _render(body))


def send_payout_status(to: str, username: str, amount: float, status: str,
                        gateway_reference: str = None, note: str = "") -> bool:
    """Sent to a SELLER for payout_requests (escrow payouts) — distinct
    from send_withdrawal_processed(), which covers the older generic
    wallet withdrawal flow."""
    is_paid = status == "paid"
    status_color = "#10B981" if is_paid else ("#EF4444" if status in ("rejected", "failed") else "#F59E0B")
    status_label = {"paid": "Paid ✅", "rejected": "Rejected ❌",
                    "failed": "Failed ❌", "processing": "Processing ⏳"}.get(status, status.title())
    body = f"""
    <h2>Payout {status_label}</h2>
    <p>Hi <strong>{username}</strong>, your seller payout request has been updated.</p>
    <div class="info-box">
      <div class="label">Amount</div>
      <div class="value">${amount:.2f}</div>
    </div>
    <div class="info-box">
      <div class="label">Status</div>
      <div class="value" style="color:{status_color}">{status_label}</div>
    </div>
    {f'<div class="info-box"><div class="label">Gateway Reference</div><div class="value" style="font-size:14px;font-family:monospace">{gateway_reference}</div></div>' if gateway_reference else ''}
    {f'<div class="info-box"><div class="label">Note</div><div class="value" style="font-size:15px">{note}</div></div>' if note else ''}
    """
    return send_email(to, f"Payout {status_label} — MercX Digital", _render(body))


def send_listing_status(to: str, seller_name: str, title: str,
                         status: str, reason: str = "") -> bool:
    approved = status == "approved"
    status_label = "Approved ✅" if approved else "Rejected ❌"
    body = f"""
    <h2>Listing {status_label}</h2>
    <p>Hi <strong>{seller_name}</strong>, we've reviewed your listing.</p>
    <div class="info-box">
      <div class="label">Product</div>
      <div class="value">{title}</div>
    </div>
    <div class="info-box">
      <div class="label">Decision</div>
      <div class="value" style="color:{'#10B981' if approved else '#EF4444'}">{status_label}</div>
    </div>
    {"<div class='info-box'><div class='label'>Reason</div><div class='value' style='font-size:15px'>"+reason+"</div></div>" if reason else ""}
    {"<p>Your product is now live on the marketplace!</p>" if approved else "<p>Please update your listing and resubmit for review.</p>"}
    """
    return send_email(to, f"Listing {status_label} — MercX Digital", _render(body))
