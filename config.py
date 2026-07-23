import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

class Config:
    # ── Core ──────────────────────────────────────────────────
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    WTF_CSRF_SECRET_KEY = os.environ.get("WTF_CSRF_SECRET_KEY", SECRET_KEY)
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

    # ── Session ───────────────────────────────────────────────
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=int(os.environ.get("PERMANENT_SESSION_LIFETIME", 1440))
    )
    SESSION_COOKIE_SECURE   = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # ── Supabase ──────────────────────────────────────────────
    SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
    SUPABASE_SECRET_KEY  = os.environ.get("SUPABASE_SECRET_KEY", "")
    SUPABASE_ANON_KEY    = os.environ.get("SUPABASE_ANON_KEY", "")
    SUPABASE_BUCKET      = os.environ.get("SUPABASE_STORAGE_BUCKET", "mercx-assets")

    # ── Email (Brevo transactional HTTP API) ─────────────────
    # Switched from raw SMTP because Render's free tier blocks all
    # outbound traffic on SMTP ports (25/465/587). Brevo's API is
    # plain HTTPS on port 443, which is never blocked, and it's a
    # single HTTP call instead of a stateful socket connection —
    # no handshake to hang, no worker-killing timeout risk.
    BREVO_API_KEY        = os.environ.get("BREVO_API_KEY", "")
    MAIL_DEFAULT_SENDER  = os.environ.get("MAIL_DEFAULT_SENDER", "MercX Digital <noreply@mercxdigital.com>")
    # Hard ceiling on the Brevo API call. This is what stops a slow
    # or unreachable API endpoint from hanging a Gunicorn worker
    # until it gets SIGKILLed — never remove or set to None.
    BREVO_TIMEOUT        = int(os.environ.get("BREVO_TIMEOUT", 10))

    # ── Payment Gateways ──────────────────────────────────────
    STRIPE_SECRET_KEY        = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY   = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET    = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    FLUTTERWAVE_SECRET_KEY   = os.environ.get("FLUTTERWAVE_SECRET_KEY", "")
    FLUTTERWAVE_PUBLIC_KEY   = os.environ.get("FLUTTERWAVE_PUBLIC_KEY", "")
    FLUTTERWAVE_WEBHOOK_SECRET = os.environ.get("FLUTTERWAVE_WEBHOOK_SECRET", "")
    PAYSTACK_SECRET_KEY      = os.environ.get("PAYSTACK_SECRET_KEY", "")
    PAYSTACK_PUBLIC_KEY      = os.environ.get("PAYSTACK_PUBLIC_KEY", "")

    # ── Redis ─────────────────────────────────────────────────
    REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

    # ── Escrow ────────────────────────────────────────────────
    ESCROW_AUTO_RELEASE_HOURS = int(os.environ.get("ESCROW_AUTO_RELEASE_HOURS", 72))
    CRON_SECRET               = os.environ.get("CRON_SECRET", "")

    # ── Upload / File Limits ──────────────────────────────────
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 52428800))  # 50 MB
    # SECURITY (BUG-016): "svg" intentionally excluded. Uploaded listing/store
    # images are served back to visitors as-is via Supabase Storage public
    # URLs embedded directly in <img src="..."> — there is no app-layer proxy
    # in front of them to add Content-Disposition/CSP headers or to sanitize
    # the file on the way out. An SVG can embed <script>/event-handler
    # payloads that execute in the visitor's browser when rendered inline,
    # so allowing SVG upload here was a stored-XSS vector. Raster formats
    # (png/jpg/jpeg/gif/webp) don't execute script content and remain safe
    # to serve inline as-is. If SVG support is needed later, it must go
    # through a sanitizer (e.g. a dedicated SVG-scrubbing library) on
    # upload, not be re-added here as-is.
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    ALLOWED_FILE_EXTENSIONS  = {"zip", "rar", "tar", "gz", "7z", "pdf", "docx",
                                 "figma", "sketch", "xd", "psd", "ai", "mp3", "mp4",
                                 "ttf", "otf", "woff", "woff2", "csv", "json", "xml"}

    # ── Business Rules ────────────────────────────────────────
    SITE_URL              = os.environ.get("SITE_URL", "http://localhost:5000")
    COMMISSION_RATE       = float(os.environ.get("COMMISSION_RATE", 10)) / 100
    MAX_CART_ITEMS        = 20
    MAX_DOWNLOADS         = 5
    DOWNLOAD_EXPIRY_DAYS  = 7
    MIN_WITHDRAWAL        = 10.00
    MAX_WITHDRAWAL        = 10_000.00
    REFERRAL_BONUS        = 5.00

class DevelopmentConfig(Config):
    # Respect FLASK_DEBUG (read in the base Config class) rather than
    # hardcoding True here. Hardcoding it previously meant any deployment
    # that fell through to this class (e.g. a missing/misspelled FLASK_ENV
    # on Render) silently ran with DEBUG=True, which makes Flask bypass the
    # custom @app.errorhandler(500) page entirely — propagate_exceptions
    # defaults to True whenever app.debug is True — and dump a bare
    # "Internal Server Error" instead of errors/500.html.
    pass

class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True

config_map = {
    "development": DevelopmentConfig,
    "production":  ProductionConfig,
    "default":     ProductionConfig,
}

def get_config():
    # Fail safe, not fail open: default to production if FLASK_ENV isn't
    # explicitly set, so a forgotten env var on a real deployment never
    # silently turns on DEBUG (see DevelopmentConfig note above).
    env = os.environ.get("FLASK_ENV", "production")
    return config_map.get(env, ProductionConfig)
