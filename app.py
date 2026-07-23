from flask import Flask, render_template, session, g, request
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from config import get_config
from utils.helpers import fmt_price, time_ago, fmt_date, truncate, calc_discount_pct

csrf    = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=["500 per hour"])


def create_app():
    app = Flask(__name__)
    cfg_obj = get_config()
    app.config.from_object(cfg_obj)

    # Surface the active config class + DEBUG in the deploy logs so a
    # forgotten/misspelled FLASK_ENV is immediately visible, instead of
    # silently running in the wrong mode (see config.py for why this
    # matters — DEBUG=True in production bypasses the custom error pages).
    import os as _os
    app.logger.info(
        f"Starting with config={cfg_obj.__name__} DEBUG={app.config.get('DEBUG')} "
        f"FLASK_ENV={_os.environ.get('FLASK_ENV', 'unset (defaults to production)')}"
    )

    # ── Extensions ────────────────────────────────────────────
    csrf.init_app(app)
    limiter.init_app(app)

    # Exempt webhook routes from CSRF
    csrf.exempt("blueprints.api.stripe_webhook")
    csrf.exempt("blueprints.api.paystack_webhook")
    csrf.exempt("blueprints.api.flutterwave_webhook")
    # Exempt the auto-release sweep: it's invoked either by an
    # authenticated admin click (session-based, CSRF token present)
    # OR by an external scheduler with no browser session at all
    # (authenticated instead via the X-Cron-Secret header inside the
    # route itself) — CSRF tokens don't apply to that second caller.
    csrf.exempt("blueprints.escrow.run_auto_release")

    # ── Blueprints ────────────────────────────────────────────
    from blueprints.main        import main_bp
    from blueprints.auth        import auth_bp
    from blueprints.dashboard   import dashboard_bp
    from blueprints.seller      import seller_bp
    from blueprints.marketplace import marketplace_bp
    from blueprints.admin       import admin_bp
    from blueprints.api         import api_bp
    from blueprints.escrow      import escrow_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp,        url_prefix="/auth")
    app.register_blueprint(dashboard_bp,   url_prefix="/dashboard")
    app.register_blueprint(seller_bp,      url_prefix="/seller")
    app.register_blueprint(marketplace_bp, url_prefix="/marketplace")
    app.register_blueprint(admin_bp,       url_prefix="/admin")
    app.register_blueprint(api_bp,         url_prefix="/api")
    app.register_blueprint(escrow_bp)

    # ── Rate-limit sensitive routes ───────────────────────────
    limiter.limit("10 per minute")(auth_bp)

    # ── Jinja2 Filters ────────────────────────────────────────
    app.jinja_env.filters["fmt_price"]       = fmt_price
    app.jinja_env.filters["time_ago"]        = time_ago
    app.jinja_env.filters["fmt_date"]        = fmt_date
    app.jinja_env.filters["truncate_text"]   = truncate
    app.jinja_env.filters["discount_pct"]    = calc_discount_pct

    # ── Template Context Processor ────────────────────────────
    @app.context_processor
    def inject_globals():
        from utils.supabase_client import db_select
        uid           = session.get("user_id")
        cart_count    = 0
        notif_count   = 0
        message_count = 0

        if uid:
            try:
                cart_count  = len(db_select("cart_items", "id", filters={"user_id": uid}))
                notif_count = len(db_select("notifications", "id",
                                            filters={"user_id": uid, "is_read": False}))
                convs1 = db_select("conversations", "unread_count_1",
                                   filters={"participant_1": uid})
                convs2 = db_select("conversations", "unread_count_2",
                                   filters={"participant_2": uid})
                message_count = (sum(c.get("unread_count_1", 0) for c in convs1) +
                                 sum(c.get("unread_count_2", 0) for c in convs2))
            except Exception:
                pass

        return dict(
            cart_count=cart_count,
            notif_count=notif_count,
            message_count=message_count,
            current_user_id=uid,
            current_role=session.get("role"),
            current_username=session.get("username"),
            current_balance=session.get("balance", 0),
            is_verified=session.get("is_verified", False),
        )

    # ── Error Handlers ────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(401)
    def unauthorized(e):
        # BUG-009 fix: add 401 handler for parity with 403/404/429/500.
        # Most auth gates in this app redirect rather than abort(401), so
        # this is rarely hit today, but API routes and future abort(401)
        # calls now get the themed 403 page (which is the most appropriate
        # existing template for "you are not permitted here") rather than
        # Flask's bare default.
        return render_template("errors/403.html"), 401

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(500)
    def server_error(e):
        app.logger.error(f"500 error: {e}")
        return render_template("errors/500.html"), 500

    @app.errorhandler(429)
    def rate_limited(e):
        from flask import jsonify
        return jsonify({"error": "Too many requests. Slow down."}), 429

    # ── Security Headers ──────────────────────────────────────
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"]        = "SAMEORIGIN"
        response.headers["X-XSS-Protection"]       = "1; mode=block"
        response.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]      = "geolocation=(), microphone=(), camera=()"
        return response

    return app


# ── Entry Point ───────────────────────────────────────────────

app = create_app()

if __name__ == "__main__":
    app.run(debug=app.config.get("DEBUG", False), host="0.0.0.0", port=5000)
