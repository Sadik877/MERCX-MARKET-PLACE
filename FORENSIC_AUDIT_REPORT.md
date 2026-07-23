# FORENSIC AUDIT REPORT — MercX Digital Marketplace

**Scope:** Full codebase audit (no fixes applied — diagnosis only, per instructions).
**Build audited:** `MERCX-MARKET-PLACE-main__3_.zip` — includes the escrow/payout subsystem
(`blueprints/escrow.py`, `migrations/002_escrow_system.sql`, `migrations/003_seller_payout_accounts.sql`,
`utils/gateways.py`, Brevo-based `utils/email.py`).
**Method:** Static reading of every blueprint, every template's `extends`/`block` chain, the
full `schema.sql` + all migrations, `utils/*`, `static/js/main.js`, and a mechanical
cross-reference script comparing every `url_for('endpoint.name')` call found in templates and
Python against the full set of 122 endpoints actually registered by Flask. No live server was
run against a real Supabase instance, so purely data-dependent runtime behavior (e.g. exact
Supabase error strings, actual empty-state rendering) is flagged as inferred where relevant
rather than asserted as directly observed.

Companion files: **BUG_INVENTORY.md** (structured bug-by-bug detail, BUG-001…011) and
**FIX_ROADMAP.md** (phased plan). This document is the narrative summary organized by the
10 categories requested.

---

## 1. Broken Links / Routes

A full mechanical cross-reference (every `url_for()` in every template + every blueprint,
against every registered endpoint) found **two dangling endpoint references**, both of which
are 100%-reproducible crashes, not edge cases:

| Referenced endpoint | Where | Actually exists? | Impact |
|---|---|---|---|
| `seller.wallet` | `blueprints/escrow.py` ×3 (`request_payout()`) | **No** — no such route in `seller_bp` | Every "Request Payout" submission 500s (BUG-002) |
| `admin.reply_ticket` | `templates/admin/ticket_detail.html` | **No** — not defined in `admin.py` | Support ticket reply form is dead (BUG-003) |

A related, non-`url_for` finding: `admin.ticket_detail` itself is also undefined, and
`admin/support.html` has no link to it at all — the whole ticket-detail view is orphaned, not
just its reply action (BUG-003).

No other broken `url_for()` references were found — the remaining 120 endpoints are all
correctly wired (this includes spot-checking every nav link in `base.html`,
`admin/base.html`, `seller/base.html`, and `dashboard/base.html`).

**Everything (forms, `href`, AJAX `fetch()`) was checked**, not just page links:
`static/js/main.js` fetch targets were enumerated and all point at real `/api/...` routes;
its generic `data-ajax-form` handler gracefully falls back to `window.location.reload()` on a
non-JSON response, so a stale/expired session mid-AJAX-submit degrades gracefully rather than
silently failing.

---

## 2. Admin Panel Blank Page

No template-inheritance bug was found that would produce a literally-empty page: every
`templates/admin/*.html` file correctly `extends 'admin/base.html'` and overrides
`{% block page_content %}`, and `admin/base.html` correctly `extends 'base.html'` and overrides
`{% block content %}`, which in turn is rendered inside `<main>{% block content %}{% endblock %}</main>`
in `base.html`. The CSS classes the layout depends on (`.dashboard-layout`, `.sidebar`,
`.dashboard-content`, `.sidebar-link`) are all defined in `static/css/main.css`. This is not
where the "blank page" symptom originates.

**Most likely actual cause, given the evidence:** BUG-005 (production config). If `FLASK_ENV`
is not explicitly set to `production` on the deployment (the shipped `.env.example` defaults it
to `development`, and `DevelopmentConfig` hardcodes `DEBUG=True` regardless of `FLASK_DEBUG`),
then `admin_required` correctly gates the page, but **any exception raised while building the
dashboard's ~15 queries** (a timeout, a malformed row, a `None` where a dict is expected inside
one of the several `for` enrichment loops in `admin.dashboard()`) bypasses the styled
`errors/500.html` entirely — with `propagate_exceptions=True`, Gunicorn returns its own minimal
error response. Depending on how that minimal response happens to be viewed (some browsers/
proxies render a completely empty or near-empty body for a bare, un-styled 500 with no HTML
payload), this can present to the user as "the admin page is blank" rather than as a visible
error page. This also independently explains audit item **3** below, so it should be treated as
one root cause with two symptoms, not two separate bugs.

**Secondary, lower-probability contributor worth ruling out at runtime:** `admin.dashboard()`
issues on the order of 15+ sequential Supabase calls with no batching (see the N+1 note in
BUG_INVENTORY.md). If Supabase credentials are misconfigured or the project is paused/rate-
limited, `db_select()`'s own retry/error-swallowing design (`utils/supabase_client.py`) means
every one of those calls fails silently and returns `[]`/`None` rather than raising — in that
scenario the page **would** render, just as an all-zeros dashboard, which is visually close to
"blank" but is a different bug class (config/connectivity, not code) and would need to be ruled
out by checking `SUPABASE_URL`/`SUPABASE_SECRET_KEY` in the actual deployment environment and
the app's server logs for `"db_select(...) error: ..."` lines emitted by that wrapper.

**Recommendation:** fix BUG-005 first (it's cheap and clearly wrong regardless), redeploy, and
reproduce the blank page again. If it persists, capture the actual Gunicorn/Render log output
for the failing request — that will show the real exception and let this be pinned to an exact
line, versus the two credible hypotheses above.

---

## 3. Error Pages ("Internal Server Error" instead of custom pages)

Root cause identified with high confidence: **BUG-005**. `app.py` correctly registers
`@app.errorhandler(404/403/500/429)`, and all four templates (`errors/404.html`, `403.html`,
`500.html`) exist, correctly extend `base.html`, and use the right block name. The handlers
themselves are not the problem.

The problem is upstream: Flask only calls a registered `errorhandler` for **unhandled**
exceptions when `app.debug` is falsy (specifically, when `propagate_exceptions` is `False`,
which is the default *unless* `TESTING` or `DEBUG` is true). Because `DevelopmentConfig`
hardcodes `DEBUG = True` and `get_config()` silently falls back to `DevelopmentConfig` whenever
`FLASK_ENV` isn't exactly `"production"`, a deployment that forgets (or never explicitly sets)
`FLASK_ENV=production` runs with `DEBUG=True` in production. Under Gunicorn this does **not**
show Werkzeug's interactive debugger (that only attaches via `app.run(debug=True)`) — instead,
the exception propagates past Flask's own error handling straight to Gunicorn, which returns
its own bare `"Internal Server Error"` text response. This exactly matches the reported symptom
and explains why the *custom* pages — which are correctly built — never get a chance to render.

**Fix:** see BUG-005. This is a one-file, two-line change (`config.py`).

---

## 4. Store Creation

Full flow traced end-to-end as requested:

```
Seller clicks "Become a Seller" / saves Store Settings
  → form (CSRF token present, confirmed via grep — not the blocker)
  → POST /dashboard/become-seller  or  POST /seller/settings
  → @login_required / @verified_required / @seller_required (pass for a normal verified user)
  → store_name validated (non-empty) — fine
  → *** from python_slugify import slugify as _slugify ***
  → ModuleNotFoundError raised here, unconditionally
  → view function never reaches db_update()
  → Flask 500s (and, per finding #3 above, likely shows a bare error rather than the styled page)
```

**Root cause: BUG-001**, confirmed by directly attempting the import in this environment:
`import python_slugify` fails, `import slugify` succeeds, and `pip show python-slugify` shows
the package genuinely is installed under that PyPI name — the code is simply importing it by
the wrong module name. This is not a missing-dependency problem and not a database problem;
`utils/helpers.py` proves the correct import already exists elsewhere in the same codebase.

No other blocker was found downstream of that line (the `db_update` calls that would run
afterward are well-formed, `store_slug` is nullable + unique in `user_profiles`, and there is no
duplicate-store guard that would otherwise reject a second attempt).

---

## 5. All Major Features — Status Summary

Audited only what exists in the codebase, per instructions.

| Feature | Status | Notes |
|---|---|---|
| Registration / Login / Logout | OK | Lockout after 5 failed attempts, referral handling present (counter bug — BUG-008, cosmetic) |
| Forgot / Reset Password | OK | Token hashing + expiry correct; no user-enumeration leak |
| Email verification | Blocked in practice | Not a code bug itself, but see BUG-006 — emails silently don't send without `BREVO_API_KEY`, which `.env.example` never documents |
| Buyer dashboard (overview/orders/purchases/wishlist/messages/notifications/referrals) | OK | Functions correctly against present tests |
| Store Creation / "Become a Seller" | **Broken** | BUG-001 (Critical) |
| Store Settings | **Broken** | BUG-001 (same root cause, second call site) |
| Product create / edit / delete / pause / duplicate | OK | |
| Product listing / detail page | Mostly OK | "Already purchased" indicator unreliable on multi-sale listings — BUG-007 (does not affect actual download access control) |
| Cart / Coupons / Checkout | OK | Wallet-balance debit path already hardened via `checkout_wallet_debit_atomic` in this build |
| Escrow: confirm receipt / open dispute / dispute thread | OK | |
| Escrow: seller payout request | **Broken** | BUG-002 (Critical) |
| Escrow: admin payout approve/reject, admin dispute resolve | OK | Real-gateway-then-ledger ordering is correctly sequenced (money is sent before the internal ledger is debited, with rollback-safe error handling) |
| Admin dashboard / users / listings / orders / wallet / categories / coupons / reports / analytics / logs / settings / disputes / payouts | OK, except: | |
| Admin support tickets | **Broken (detail/reply)** | BUG-003 — list/filter view works, per-ticket view and reply do not |
| Payment webhooks (Stripe / Paystack / Flutterwave) | Mostly hardened | Idempotent crediting + replay protection all present and correct; Paystack alone is missing the "reject if secret unconfigured" guard the other two have — BUG-004 (Critical, security) |
| Notifications | OK | |
| Profile / Settings | OK | |
| Search / Categories | OK, functionally | In-Python filtering rather than DB-side (existing GIN full-text index on `listings` is unused) — a performance concern at scale, not a bug |

---

## 6. Database / Backend Consistency

- `schema.sql` does **not** contain the escrow/payout tables (`escrow_transactions`,
  `escrow_events`, `escrow_holds`, `disputes`, `dispute_messages`, `payout_requests`,
  `payout_history`, `webhook_events`, `seller_payout_accounts`) — those exist only in
  `migrations/002_escrow_system.sql` and `migrations/003_seller_payout_accounts.sql`. Anyone
  bootstrapping a fresh database from `schema.sql` alone gets an app that immediately fails on
  any escrow-touching route. See BUG-010.
- No mismatched column names or wrong field references were found between the blueprints and
  the schema/migrations for any table actually queried — this is a genuine strength of the
  codebase; every `db_select`/`db_insert`/`db_update` call's field names were spot-checked
  against the corresponding `CREATE TABLE` and matched.
- Financial-integrity design is notably *better* than a first read of "wallet balance stored on
  `users.balance`" would suggest: deposits, wallet-tx approvals, checkout debits, and escrow
  release/refund all route through dedicated Postgres RPC functions
  (`wallet_credit_idempotent`, `wallet_debit_atomic`, `wallet_tx_approve_atomic`,
  `escrow_release`, etc. — see `utils/supabase_client.py`), which avoids the classic
  read-balance-then-write-balance race condition for those paths. The one exception found is
  `admin.py`'s `refund_order()`, which still does a manual read-then-write — inconsistent with
  the rest of the codebase's own hardening (see BUG_INVENTORY.md, lower-priority notes).
- Webhook idempotency: `webhook_events` (migration 002) plus the `reference`-keyed
  idempotent-credit RPC together correctly prevent duplicate crediting from gateway retries —
  confirmed by reading all three webhook handlers end-to-end.

---

## 7. JavaScript

`static/js/main.js` was read in full for the areas most likely to hide silent failures:

- All `fetch()` calls target real, existing `/api/...` endpoints (cross-checked against the
  same endpoint list used for the Python/template audit) — no broken AJAX URLs found.
- CSRF: JSON `fetch()` POSTs correctly attach `X-CSRFToken` (via a shared `getCsrf()` helper
  reading the `<meta name="csrf-token">` tag that `base.html` renders); this matches what
  Flask-WTF's `CSRFProtect` expects for non-form-encoded requests, so there is no CSRF failure
  on the AJAX endpoints.
- The generic `data-ajax-form` submit handler checks the response's `Content-Type`: if JSON, it
  reads `redirect`/`success`/`error` keys; if not JSON (e.g. a login-redirect HTML page from an
  expired session), it does a full `window.location.reload()` rather than trying to parse HTML
  as JSON — this is a defensive, correct fallback, not a bug.
- No evidence of unregistered event listeners, undefined-variable references, or JSON-parsing
  crashes was found in this file. (A live browser console session against a running instance
  would be the only way to catch a purely runtime-only JS error not visible from static
  reading — flagged as out of scope for a static audit, not asserted as "clean.")

---

## 8. Templates

- Inheritance chains for `base.html` → `admin/base.html` / `seller/base.html` /
  `dashboard/base.html` → every leaf template were all checked for matching `extends`/`block`
  pairs; all leaf templates in `templates/admin/*` correctly use `page_content` (matching what
  `admin/base.html` defines), so there is no block-name mismatch bug anywhere in the admin
  template set.
- The one genuinely broken template reference is `admin/ticket_detail.html`'s
  `url_for('admin.reply_ticket', ...)` — see BUG-003. Every other `url_for()` call across every
  template resolved successfully in the mechanical cross-reference.
- CSRF tokens: every `<form method="POST">` found across `templates/` includes a
  `csrf_token()` call somewhere in the same file (spot-checked via `grep -L` for any POST-form
  template *missing* one — none found).

---

## 9. Security

Findings not already covered above:

- **BUG-004** (Paystack webhook signature bypass when secret is unconfigured) is the most
  significant finding in this section — a real forged-payment / wallet-inflation vector under a
  plausible misconfiguration, not a theoretical one.
- No hardcoded credentials, API keys, or secrets were found in the codebase — all sensitive
  config is read from environment variables with safe (empty-string) fallbacks.
- Password reset and email verification tokens are stored as SHA-256 hashes with expiry
  timestamps, not as raw tokens — correct practice, confirmed in `blueprints/auth.py`.
- Login lockout (5 failed attempts → 15 min lock) is implemented and is not bypassed by any
  code path checked.
- `ALLOWED_IMAGE_EXTENSIONS` includes `svg`, which can carry an XSS payload if served inline to
  other users' browsers — flagged as a hardening recommendation (see BUG_INVENTORY.md), not a
  currently-exploited bug, since no code path was found that strips or executes SVG content
  server-side beyond passing it through to storage.
- No SQL injection surface exists — all data access goes through the Supabase client's
  parameterized `.eq()`/`.insert()`/`.update()` builders or through named RPC function
  parameters; no raw string-interpolated SQL was found anywhere in the audited code.
- Authorization: `admin_required` allows both `admin` and `moderator` roles for most admin
  actions, while `super_admin_required` (strictly `admin`) is correctly reserved for the more
  sensitive actions (`set_role`, `refund_order`, wallet tx approve/reject) — this separation is
  consistently applied everywhere it was checked.
- Do not exploit anything performed — none of the above was actively tested against a live
  system, per instructions; these are static-analysis findings only.

---

## 10. Production / Render

- **`FLASK_ENV` default-to-development** is the standout production-readiness gap — see
  BUG-005. This single issue plausibly explains both the blank admin page and the generic
  error-page symptoms reported.
- **Email**: correctly re-architected to avoid Render's free-tier SMTP port blocking (moved to
  Brevo's HTTPS API, with an explicit `BREVO_TIMEOUT` specifically to avoid hanging a Gunicorn
  worker) — this is good, deliberate production engineering. The only gap is that
  `.env.example`/README weren't updated to match (BUG-006).
- **Gunicorn** is a listed dependency but there is no `Procfile`/`render.yaml` committed
  specifying its invocation — BUG-011 (low severity, but affects reproducibility).
- **Static files**: served via Flask's own `static` endpoint / `url_for('static', ...)`
  everywhere checked — no hardcoded absolute paths or environment-specific URLs found that
  would break between local dev and Render.
- **Database connection**: `get_supabase()` correctly validates `SUPABASE_URL` /
  `SUPABASE_SECRET_KEY` individually and raises a specific, actionable `RuntimeError` naming
  which one is missing, with a targeted hint about a common SDK-version pitfall
  (`sb_secret_...` non-JWT key format vs. old SDK versions) — this is already well-engineered
  and requires no fix.
- **Logging**: errors from `db_select`/`db_insert`/etc. are logged via
  `current_app.logger.error(...)`, and the 500 handler in `app.py` also logs the triggering
  exception — but as covered in items #2/#3, none of that logging is reached for the *specific*
  class of error that bypasses Flask's handler entirely under `DEBUG=True`, since Gunicorn
  intercepts it first. Fixing BUG-005 restores full visibility into that logging path too.

---

## Summary of Root Causes Behind the User-Reported Symptoms

| Reported symptom | Root cause | Bug ID |
|---|---|---|
| Admin panel blank page | Most likely: `DEBUG=True` in production swallowing exceptions before custom templates render; secondary possibility: Supabase misconfiguration returning empty data everywhere | BUG-005 (primary) |
| Errors only show generic "Internal Server Error" | Same root cause as above — Flask never invokes the registered `errorhandler(500)` when `propagate_exceptions=True` | BUG-005 |
| Sellers cannot create a store | Wrong import module name (`python_slugify` vs `slugify`) crashes the view before any DB write | BUG-001 |

These three reported symptoms very likely trace back to **two root causes**, both small,
precise, and already fully diagnosed — see FIX_ROADMAP.md Phase 1 for exact remediation order.
