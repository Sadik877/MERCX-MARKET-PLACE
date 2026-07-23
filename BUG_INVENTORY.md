# BUG INVENTORY — MercX Digital Marketplace

Audit scope: full codebase in `MERCX-MARKET-PLACE-main__3_.zip` (includes escrow/payout subsystem).
Method: static code inspection + mechanical cross-reference of every `url_for()` call in
templates/blueprints against every registered Flask endpoint, plus manual reading of every
blueprint, template-inheritance chain, and the DB schema/migrations.

This file is additive. Do not delete or renumber existing entries when updating later —
append new ones or mark existing ones `STATUS: FIXED` / `STATUS: WONTFIX`.

Severity scale: **Critical** (data loss, money loss, security bypass, feature 100% broken) ·
**High** (feature broken for a common path) · **Medium** (degraded/incorrect behavior,
edge case) · **Low** (cosmetic, hygiene, best-practice).

---

## BUG-001
**Category:** Store Creation / Seller Onboarding
**Severity:** Critical
**File:** `blueprints/dashboard.py` (line ~588, inside `become_seller()`), `blueprints/seller.py` (line ~771, inside `store_settings()`)
**Route:** `POST /dashboard/become-seller`, `POST /seller/settings`
**Symptom:** Clicking "Become a Seller" / "Create Store", or saving Store Settings, returns a
500 Internal Server Error. The store is never created.
**Root Cause:** Both functions contain a local import:
```python
from python_slugify import slugify as _slugify
```
`python-slugify` is the **PyPI package name**, but the importable **module name is `slugify`**
(confirmed: `pip show python-slugify` succeeds, `import python_slugify` fails,
`import slugify` succeeds — verified in this environment). There is no module named
`python_slugify` on disk, so this line raises `ModuleNotFoundError` every single time either
function runs. `utils/helpers.py` already imports it correctly
(`from slugify import slugify as _slugify`) and exposes a ready-made `make_slug()` helper —
these two call sites simply never got updated to use it.
**Evidence:**
```
$ python3 -c "import slugify; print(slugify.__file__)"          # works
$ pip show python-slugify                                        # package IS installed
$ grep -rn "python_slugify" blueprints/
blueprints/seller.py:771:        from python_slugify import slugify as _slugify
blueprints/dashboard.py:588:    from python_slugify import slugify as _slugify
```
**Recommended Fix:** Delete both local imports; use the existing `make_slug()` from
`utils.helpers` (already imported at the top of `seller.py`; needs adding to the import list
in `dashboard.py`). This is the exact, complete root cause of the "Store Creation" failure
described in the audit brief — no other blocker exists in that flow (CSRF token is present,
`store_slug` is nullable+unique in schema, the `db_update` calls are otherwise correct).
**Dependencies/Risks:** None — this is an isolated, mechanical fix. Recommend also adding a
`try/except ImportError` regression test or a startup smoke-import check so a typo like this
can never reach production silently again.

---

## BUG-002
**Category:** Seller / Payouts (Escrow)
**Severity:** Critical
**File:** `blueprints/escrow.py`, function `request_payout()`, lines 405, 410, 422
**Route:** `POST /seller/payouts/request`
**Symptom:** Every "Request Payout" submission (valid or invalid amount, sufficient or
insufficient balance, success or failure) crashes with a 500 error. The feature is 100%
non-functional.
**Root Cause:** All three redirect targets call `url_for("seller.wallet")`. No such endpoint
exists — `seller_bp` has no `/wallet` route. The actual seller earnings/payout page is
`seller.withdrawals` (`GET /seller/withdrawals`, defined in `blueprints/seller.py`). This
raises `werkzeug.routing.BuildError` unconditionally inside the view function, before any
response can be sent — Flask converts that into a 500 for the browser.
**Evidence:** Mechanical cross-reference of every `url_for()` call against every registered
endpoint (122 endpoints enumerated from all blueprints) found exactly one dangling reference
to `seller.wallet`, used 3 times, all inside this function:
```
blueprints/escrow.py:405:        return redirect(url_for("seller.wallet"))
blueprints/escrow.py:410:        return redirect(url_for("seller.wallet"))
blueprints/escrow.py:422:        return redirect(url_for("seller.wallet"))
```
No `seller.wallet` endpoint exists anywhere in `blueprints/seller.py`.
**Recommended Fix:** Replace all three with `url_for("seller.withdrawals")`.
**Dependencies/Risks:** None. Confirm `seller/withdrawals.html` actually contains the payout
request form that posts to this route (it does, per the payout-account/withdrawals UI), so no
other template change is needed.

---

## BUG-003
**Category:** Admin / Support Tickets
**Severity:** High
**File:** `templates/admin/ticket_detail.html`, `blueprints/admin.py`
**Route:** Missing route — should be `GET /admin/support/<ticket_id>` and
`POST /admin/support/<ticket_id>/reply`
**Symptom:** There is no way for an admin to open an individual support ticket or reply to it.
The "Support" list page (`admin/support.html`) renders only status-filter tabs and (presumably)
a ticket list with no working per-row link, because no endpoint exists to link to.
**Root Cause:** `templates/admin/ticket_detail.html` references
`url_for('admin.reply_ticket', ticket_id=ticket.id)`, but `admin.py` defines no `reply_ticket`
function and no `ticket_detail` function/route at all in this build. The template is orphaned:
it was either deleted from the blueprint during the escrow refactor, or never finished. Both
`admin.ticket_detail` and `admin.reply_ticket` are dangling references.
**Evidence:**
```
$ grep -n "reply_ticket\|ticket_detail" templates/admin/*.html
templates/admin/ticket_detail.html:37: action="{{ url_for('admin.reply_ticket', ...) }}"
$ grep -n "def ticket_detail\|def reply_ticket" blueprints/admin.py
(no results)
$ grep -n "url_for('admin.ticket_detail'" templates/admin/support.html
(no results — support.html has no link into the detail page at all)
```
**Recommended Fix:** Re-add the two missing routes to `admin.py` (a `GET /support/<ticket_id>`
view that loads the ticket + its `ticket_messages` and renders `ticket_detail.html`, and a
`POST /support/<ticket_id>/reply` that inserts a `ticket_messages` row with `is_staff=True` and
optionally updates `support_tickets.status`), and add the missing "View" link from each row in
`admin/support.html` to `admin.ticket_detail`.
**Dependencies/Risks:** `support_tickets` / `ticket_messages` tables already exist in
`schema.sql`, so no migration is needed — this is pure blueprint code that regressed or was
never finished.

---

## BUG-004
**Category:** Security / Payment Webhooks
**Severity:** Critical
**File:** `blueprints/api.py`, function `paystack_webhook()` (~line 274)
**Route:** `POST /api/payment/paystack/webhook`
**Symptom:** If `PAYSTACK_SECRET_KEY` is ever unset/blank in the deployment environment, the
webhook silently accepts and processes **forged** payment notifications, crediting arbitrary
user wallets with arbitrary amounts.
**Root Cause:**
```python
secret  = current_app.config.get("PAYSTACK_SECRET_KEY", "")
sig     = request.headers.get("x-paystack-signature", "")
payload = request.get_data()
expected = hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()
if not hmac.compare_digest(sig, expected):
    return jsonify({"error": "Invalid signature"}), 400
```
There is no guard for `not secret` before computing/comparing the signature. If `secret` is
`""`, `expected` becomes `HMAC-SHA512("", payload)` — a value any attacker can compute offline
for a payload they control, since the "secret" is a known empty string. This is the exact class
of bug the sibling handlers were already patched for: `stripe_webhook()` has
`if not secret: return jsonify({"error": "Not configured"}), 400`, and
`flutterwave_webhook()` has `if not secret or not hmac.compare_digest(sig, secret): ...`.
Only the Paystack handler is missing the check — it appears the fix was applied to two of the
three gateways and missed on the third.
**Evidence:** Direct read of all three webhook handlers in `blueprints/api.py`; Stripe (line
~213) and Flutterwave (line ~346) both explicitly reject when the secret is empty, Paystack
(line ~278) does not.
**Recommended Fix:**
```python
secret = current_app.config.get("PAYSTACK_SECRET_KEY", "")
if not secret:
    return jsonify({"error": "Not configured"}), 400
sig = request.headers.get("x-paystack-signature", "")
...
```
**Dependencies/Risks:** None functionally — this only tightens an existing check. Should be
fixed before Phase 1 sign-off since it's a live financial-integrity exposure whenever the env
var is merely forgotten (a very plausible ops mistake, not a hypothetical).

---

## BUG-005
**Category:** Production Configuration / Error Handling
**Severity:** Critical
**File:** `config.py`
**Route:** N/A — affects every route
**Symptom:** In production, unhandled exceptions show a bare, unstyled **"Internal Server
Error"** instead of the custom `templates/errors/500.html` page. This matches the audit brief's
symptom #3 exactly.
**Root Cause:** Two compounding issues in `config.py`:
1. `get_config()` defaults to `"development"` whenever `FLASK_ENV` is not explicitly set:
   `env = os.environ.get("FLASK_ENV", "development")`. `.env.example` itself ships with
   `FLASK_ENV=development`, so any deployment that copies the example file (or simply forgets
   to override it on Render) silently runs `DevelopmentConfig`.
2. `DevelopmentConfig` **hardcodes** `DEBUG = True`, completely bypassing the `FLASK_DEBUG`
   env-var check done in the base `Config` class:
   ```python
   class Config:
       DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"   # respects env var
   class DevelopmentConfig(Config):
       DEBUG = True                                         # ...but this always wins
   ```
   With `app.debug = True`, Flask's `propagate_exceptions` defaults to `True`
   (`self.testing or self.debug`), which makes Flask **re-raise unhandled exceptions instead
   of invoking the registered `@app.errorhandler(500)`**. Under Gunicorn (no interactive
   debugger attached, unlike `app.run(debug=True)`), the re-raised exception is caught by the
   WSGI server, which returns its own bare "Internal Server Error" — never touching
   `errors/500.html`.
**Evidence:**
```python
# config.py
def get_config():
    env = os.environ.get("FLASK_ENV", "development")   # unsafe default
    return config_map.get(env, DevelopmentConfig)

class DevelopmentConfig(Config):
    DEBUG = True   # ignores FLASK_DEBUG entirely
```
```
# .env.example
FLASK_ENV=development
FLASK_DEBUG=0
```
README.md does list `FLASK_ENV=production` as a deploy checklist item (line ~290), confirming
the team knows it's required — but nothing in code enforces or defaults to it, so a single
missed Render environment variable reproduces exactly the reported symptom.
**Recommended Fix:**
- Change the default in `get_config()` to `"production"` (fail safe, not fail open):
  `os.environ.get("FLASK_ENV", "production")`.
- Make `DevelopmentConfig.DEBUG` respect `FLASK_DEBUG` rather than hardcoding `True`
  (e.g. drop the override and let it inherit from `Config`, or explicitly read the env var).
- Add a startup log line printing the active config class and `DEBUG` value so this is
  immediately visible in Render's deploy logs.
- Confirm `SESSION_COOKIE_SECURE` and other prod-only hardening also don't silently regress
  under the same default-env problem (currently they're in `ProductionConfig`, same exposure).
**Dependencies/Risks:** Low risk, high value — this is a one-line-default + one-line-override
fix that closes a real production-hardening gap (a debug-mode Flask app leaking `propagate
_exceptions=True` is also a minor information-disclosure risk in mixed scenarios, e.g. if
Werkzeug's debugger becomes reachable another way).

---

## BUG-006
**Category:** Documentation / Production Configuration (Email)
**Severity:** High
**File:** `.env.example`, `utils/email.py`, `config.py`
**Route:** N/A — affects verification emails, password reset, order confirmation, deposit/
withdrawal/payout/dispute notifications
**Symptom:** In a fresh deployment following `.env.example`, all transactional email sending
silently no-ops (verification links, password resets, sale/payout notifications never arrive),
with no error surfaced to the user or admin.
**Root Cause:** The codebase was migrated from SMTP to the Brevo HTTP API — `config.py` now
only defines `BREVO_API_KEY` / `BREVO_TIMEOUT` and no longer reads any `MAIL_SERVER` /
`MAIL_PORT` / `MAIL_USERNAME` / `MAIL_PASSWORD` / `MAIL_USE_TLS` values (a deliberate change,
per the code comment: *"Render's free tier blocks all outbound traffic on SMTP ports"*).
However, `.env.example` was never updated to match — it still documents the old SMTP variables
and has **no `BREVO_API_KEY` entry at all**. `send_email()` in `utils/email.py` checks for a
configured key and returns `False` (logs a warning, does not raise) when it's missing, so
nothing in the running app signals the misconfiguration beyond a log line.
**Evidence:**
```
$ grep -n "BREVO" .env.example
(no results)
$ grep -n "MAIL_" .env.example
MAIL_SERVER=smtp.gmail.com   ... (6 more stale SMTP lines)
$ grep -n "BREVO_API_KEY\|MAIL_SERVER" config.py
config.py:33:    BREVO_API_KEY        = os.environ.get("BREVO_API_KEY", "")
(no MAIL_SERVER / MAIL_USERNAME / MAIL_PASSWORD reads anywhere in config.py)
```
**Recommended Fix:** Update `.env.example` to remove the obsolete `MAIL_*` block and add
`BREVO_API_KEY=` (with a comment linking to where to generate one) and
`BREVO_TIMEOUT=10`. Also update `README.md`'s environment-variable table (still documents the
old SMTP flow per a spot check) and its deploy checklist.
**Dependencies/Risks:** None — documentation-only fix, but high priority since it silently
breaks a user-facing, security-relevant flow (email verification / password reset).

---

## BUG-007
**Category:** Buyer / Product Ownership Check
**Severity:** High
**File:** `blueprints/marketplace.py`, function `listing_detail()` (the `already_bought` check)
**Route:** `GET /marketplace/p/<slug>`
**Symptom:** On any listing that has been purchased by more than one distinct buyer, the
"already purchased" state becomes unreliable — either throws a suppressed backend error
(silently defaults to `already_bought = False`) or checks the wrong buyer, depending on the
underlying Supabase driver's handling of `.single()`.
**Root Cause:**
```python
order = db_select("order_items", "id", filters={"listing_id": listing["id"]}, single=True)
if order:
    parent = db_select("orders", "buyer_id,status", filters={"id": order.get("order_id")}, single=True)
    if parent and parent["buyer_id"] == uid and parent["status"] == "completed":
        already_bought = True
```
This query filters `order_items` **only by `listing_id`**, not by the current buyer. Combined
with `single=True` (which asks PostgREST for exactly one row and errors if 0 or 2+ rows match),
any listing with more than one sale will make this query fail; `db_select`'s internal
`_with_retry` catches that failure, logs it, and returns `None` — so `already_bought` silently
stays `False` even for a buyer who genuinely owns the product. On a listing with **exactly one**
sale ever, the code "works" only by accident (it checks whichever single row exists, regardless
of whose it is).
**Evidence:** Direct read of `marketplace.py`; `db_select(..., single=True)` semantics
confirmed in `utils/supabase_client.py` (`q.single().execute().data`, wrapped in
`_with_retry` which swallows the resulting exception and returns `None`).
**Recommended Fix:** Query needs to be buyer-scoped. Simplest correct approach with the
existing wrapper: fetch the buyer's own completed orders first
(`db_select("orders", "id", filters={"buyer_id": uid, "status": "completed"})`), then check
whether any of those order ids has an `order_items` row for this `listing_id`
(`db_select("order_items", "id", filters={"listing_id": ..., "order_id": <in that set>})` —
since the wrapper has no `.in_()` support today, loop the (typically small) order-id list, or
add an `.in_()` capability to `db_select`, which is worth doing regardless — see
FIX_ROADMAP Phase 3).
**Dependencies/Risks:** Affects the "already purchased" badge/download-shortcut on the product
page; does **not** affect actual download access control, which is separately and correctly
re-verified in `dashboard.py`'s `download_item()` route.

---

## BUG-008
**Category:** Data Integrity / Referrals
**Severity:** Medium
**File:** `blueprints/auth.py`, function `register()`
**Route:** `POST /auth/register`
**Symptom:** A referrer's `referral_count` is reset to `1` on every new referral instead of
being incremented — after a second successful referral the stored count still reads `1`, not
`2`.
**Root Cause:**
```python
db_update("user_profiles", {"referral_count": 1}, {"user_id": referrer_id})
```
This is a hardcoded literal `1`, not `(profile.get("referral_count") or 0) + 1`. (The dollar
amount, `referral_earnings`/wallet ledger, is handled correctly elsewhere via
`balance_before`/`balance_after` — only this counter column is wrong.)
**Evidence:** `grep -n "referral_count" -r .` shows exactly one write site, and it's a literal.
**Recommended Fix:** Fetch the current `referral_count` (or `total_sales`-style increment) before
the update, or — better — replace with an atomic Postgres increment via `db_rpc`/a small SQL
function, consistent with how `wallet_credit_idempotent` etc. already avoid read-then-write
races elsewhere in this codebase.
**Dependencies/Risks:** Cosmetic/reporting-only; does not affect money movement.

---

## BUG-009
**Category:** Security / Missing Error Handler
**Severity:** Low
**File:** `app.py`
**Route:** N/A
**Symptom:** A 401 response (e.g. from an API route that manually returns 401, or from any
future `abort(401)`) falls back to Flask's plain default error page instead of a themed one.
**Root Cause:** `app.py` registers `@app.errorhandler(404)`, `403`, `500`, and `429`, but never
`401`. Most of the app's auth checks redirect (via `login_required`) rather than `abort(401)`,
so this is rarely hit today, but it's an inconsistency against the audit brief's explicit ask
to verify a "401 handler."
**Evidence:** `grep -n "errorhandler" app.py` → only 404/403/500/429 present.
**Recommended Fix:** Add a themed `@app.errorhandler(401)` for parity, or confirm/document that
401 is intentionally not used anywhere outside JSON API responses (which already return their
own JSON body and don't need an HTML error page).
**Dependencies/Risks:** None.

---

## BUG-010
**Category:** Database / Schema Consistency
**Severity:** Medium
**File:** `schema.sql` vs `migrations/002_escrow_system.sql`, `migrations/003_seller_payout_accounts.sql`
**Route:** N/A
**Symptom:** Provisioning a fresh database from `schema.sql` alone (e.g. new environment,
disaster recovery, a new contributor reading "the schema") produces a database **missing**
`escrow_transactions`, `escrow_events`, `escrow_holds`, `disputes`, `dispute_messages`,
`payout_requests`, `payout_history`, `webhook_events`, and `seller_payout_accounts` — nine
tables the app now depends on for its core money-movement flow.
**Root Cause:** The escrow/payout system was added entirely via `migrations/002_...sql` and
`migrations/003_...sql` and was never folded back into `schema.sql`, which still only describes
the pre-escrow schema. There's no single file that represents "the current schema."
**Evidence:**
```
$ grep -n "CREATE TABLE public\." schema.sql | grep -iE "escrow|dispute|payout"
(no results)
$ grep -n "CREATE TABLE" migrations/*.sql
migrations/002_escrow_system.sql: escrow_transactions, escrow_events, escrow_holds,
  disputes, dispute_messages, payout_requests, payout_history, webhook_events
migrations/003_seller_payout_accounts.sql: seller_payout_accounts
```
**Recommended Fix:** Either (a) merge migrations 001–003 into `schema.sql` and keep future
changes as incremental migrations on top of that new baseline, or (b) explicitly document
`schema.sql` as "historical / superseded — apply schema.sql THEN every file in migrations/ in
order" in its header and in `README.md`'s setup instructions. Option (a) is strongly preferred
for anyone bootstrapping a new environment.
**Dependencies/Risks:** No runtime risk to the current live database (which presumably already
has the migrations applied) — this is purely a reproducibility/onboarding risk.

---

## BUG-011
**Category:** Render / Production Deployment
**Severity:** Low
**File:** repository root
**Route:** N/A
**Symptom:** No explicit, version-controlled start command for Render.
**Root Cause:** `gunicorn==22.0.0` is in `requirements.txt`, confirming the app is meant to run
under Gunicorn in production, but there is no `Procfile` and no `render.yaml` in the repo. This
means the Gunicorn start command (`gunicorn app:app`, worker count, timeout, etc.) exists only
inside Render's dashboard UI, outside version control — it can't be code-reviewed, diffed, or
reliably reproduced by a teammate spinning up a second environment.
**Evidence:** `find . -iname "Procfile*" -o -iname "render.yaml"` → no results.
**Recommended Fix:** Add a `render.yaml` (or at minimum a `Procfile`) pinning the start command,
e.g. `web: gunicorn app:app --workers 3 --timeout 30 --log-file -`, and check it in.
**Dependencies/Risks:** None — additive only.

---

## Additional lower-priority observations (not individually numbered, grouped for brevity)

- **N+1 / "fetch entire table then `len()`" pattern** is pervasive across `admin.py`,
  `seller.py`, `dashboard.py`, and `main.py` (homepage) for simple counts (`total_users`,
  `total_orders`, etc.) and for per-row enrichment loops (fetching the buyer/seller/user record
  one row at a time inside a `for` loop over orders/reviews/listings). Functionally correct
  today at low data volume; will visibly slow down the admin dashboard, seller dashboard, and
  homepage as data grows. `db_select` has no `count`-only mode and no way to batch an `IN (...)`
  lookup — both are worth adding to `utils/supabase_client.py` as shared infrastructure rather
  than patched per call site. Tracked for Phase 3, not blocking.
- **Wallet balance reads are non-atomic in a few remaining spots** outside the already-hardened
  `wallet_credit_idempotent` / `wallet_debit_atomic` / `wallet_tx_approve_atomic` RPC-backed
  paths (e.g. `admin.py`'s `refund_order()` still does a plain read-then-`db_update` of
  `users.balance` rather than calling an atomic RPC). Lower risk than the webhook paths since
  refunds are single-admin-initiated and rare, but inconsistent with the rest of the codebase's
  own hardening pattern.
- `ALLOWED_IMAGE_EXTENSIONS` includes `svg`. Seller-uploaded SVGs are served back to buyers
  as-is; SVG can embed `<script>`/event-handler XSS payloads. Worth either stripping active
  content on upload (e.g. via `bleach`/a dedicated SVG sanitizer) or serving user-uploaded SVGs
  with `Content-Disposition: attachment` / a locked-down `Content-Security-Policy` on the
  storage bucket, not inline.
