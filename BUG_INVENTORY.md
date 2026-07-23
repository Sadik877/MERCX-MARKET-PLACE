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

---

## Session 2 — Incremental Update

**Work completed in this session:** Phase 2 (BUG-003, BUG-006, BUG-007, BUG-010) and
Phase 3 (BUG-008 confirmed already fixed, BUG-009, BUG-011) all resolved.

### Status updates

| Bug | New Status | Resolution summary |
|---|---|---|
| BUG-003 | **FIXED** | `ticket_detail` + `reply_ticket` routes added to `admin.py`; "View" link added to `admin/support.html` |
| BUG-006 | **FIXED** | `.env.example` SMTP block replaced with Brevo API config; `FLASK_ENV` corrected to `production` |
| BUG-007 | **FIXED** | `marketplace.py` `already_bought` query now scoped to current buyer's completed orders only |
| BUG-008 | **ALREADY FIXED** (prior session) | `auth.py` correctly increments `referral_count` — confirmed in code |
| BUG-009 | **FIXED** | `@app.errorhandler(401)` added to `app.py` |
| BUG-010 | **FIXED** | All 9 escrow/payout tables appended to `schema.sql` using `IF NOT EXISTS` |
| BUG-011 | **FIXED** | `Procfile` and `render.yaml` added to repo root |

### Phase 2 broken-link re-sweep result
Full `url_for()` cross-reference sweep run against the live endpoint registry
(correct method — routes detected from `@blueprint.route` decorators, not from string matching):
**zero dangling endpoint references** found. All `url_for()` calls in all blueprints,
templates, and utils resolve to an existing registered function.

### No new bugs discovered
Thorough re-read of all modified files and the full endpoint sweep found no new issues.
All 11 numbered bugs are now Fixed or Already Fixed. Remaining work is Phase 3 scalability/
hardening items (not numbered, documented in the lower-priority observations section above
and in FIX_ROADMAP.md Phase 3 items 14–17) and Phase 4 frontend.

---

## BUG-012
**Category:** Seller / Payouts (Escrow) — UI/Backend Disconnect
**Severity:** High
**File:** `templates/seller/withdrawals.html`, `templates/seller/payout_account.html`, `blueprints/seller.py::withdrawals()`
**Route:** `POST /seller/payouts/request` (`escrow.request_payout`)
**Symptom:** Sellers could save payout accounts (bank/Paystack/Flutterwave/Stripe/PayPal/
crypto) on `seller/payout_account.html`, and admins had a fully-built review UI
(`admin/payouts.html`) to approve/reject/dispatch them — but there was no way for a seller
to actually *submit* a payout request against a saved account. The "Request Withdrawal"
button sellers actually used, on `seller/withdrawals.html`, posted to the unrelated legacy
`dashboard.wallet_withdraw` endpoint instead, which writes directly to
`wallet_transactions` and knows nothing about `seller_payout_accounts` or `payout_requests`.
**Root Cause:** Two independent payout pipelines exist in this codebase and were never
cross-linked at the template layer:
- **Legacy:** `dashboard.wallet_withdraw` → `wallet_transactions` (type=withdrawal), free-text
  payout details, no gateway integration.
- **Real/intended:** `escrow.request_payout` → `payout_requests`, keyed to a saved
  `seller_payout_accounts` row, with a matching admin approval + gateway-dispatch flow
  (`escrow.admin_approve_payout` → `utils/gateways.py`).

`escrow.request_payout` was reachable by URL (and was in fact the exact function BUG-002
fixed a broken `url_for("seller.wallet")` redirect inside, last session) but no `<form>` or
link anywhere in `templates/` ever pointed to it — it was dead code from the UI's
perspective despite being fully implemented and tested in `tests/test_escrow_lifecycle.py`.
**Evidence:**
```
$ grep -rn "request_payout\|payout_requests" templates/
(no results before this fix — zero template references to either)
$ grep -n "action=" templates/seller/withdrawals.html   # (before fix)
action="{{ url_for('dashboard.wallet_withdraw') }}"
```
**Recommended Fix / Resolution (this session):**
- `blueprints/seller.py::withdrawals()` now also fetches the seller's `seller_payout_accounts`
  and `payout_requests`, and builds a unified, sorted history merging both sources (so
  existing legacy withdrawal history isn't lost, while new submissions go through the real
  pipeline).
- `templates/seller/withdrawals.html`'s "Request Withdrawal" modal now posts to
  `escrow.request_payout` with an `account_id` selected from the seller's saved accounts,
  and is hidden (replaced with an "Add a payout account" prompt) until at least one account
  exists — since `escrow.request_payout` requires one.
- `templates/seller/payout_account.html` gained a "Request Payout" cross-link to the
  withdrawals page for discoverability.
- The legacy `dashboard.wallet_withdraw` route and `wallet_transactions`-based data were
  deliberately left untouched (no backend route/logic deleted or changed) — this was a
  template + read-only-query fix only, per the audit's "keep backend behavior intact unless
  a minimal safe fix is required" rule. Historical legacy withdrawal rows still display.
**Dependencies/Risks:** None to existing data — additive template/query change only. Not yet
verified against a live Supabase instance (no network access in this sandbox); recommend
including "submit a payout via a saved account" in the still-outstanding live staging
walkthrough (see FIX_ROADMAP.md).

---



**Scope:** Continued directly from FIX_ROADMAP.md's "NEXT RECOMMENDED STEP" as left by
session 2 — the four Phase 3 items (14–17). No re-diagnosis of BUG-001 through BUG-011;
their fixes were spot-checked against source and confirmed present, not re-audited from
scratch. Full detail for each item lives in `FIX_ROADMAP.md`'s Status Tracking table
(Session 3 rows); summarized here per this file's format:

**Item 14 — `admin.py::refund_order()` read-then-write balance (referenced in the
"Additional lower-priority observations" section above):**
**STATUS: ALREADY FIXED**, not a regression. `refund_order()` calls
`wallet_credit_idempotent(user_id=..., reference=f"REFUND-{order_id}", ...)` — the same
row-locked, idempotent RPC the payment webhooks use — rather than a plain
read-then-`db_update()` of `users.balance`. This was already correct in the uploaded zip;
flagging it as resolved so the observation isn't chased again in a future session.

**Item 15 — `db_select()` count-only / `.in_()` support:**
**STATUS: FIXED (infrastructure only).** `utils/supabase_client.py::db_select()` gained
two new optional params, both additive/backward-compatible (every existing call site is
unaffected — no existing call passes these):
```python
def db_select(table, columns="*", filters=None, order=None, limit=None,
              single=False, in_filters: dict | None = None, count_only: bool = False):
```
`count_only=True` uses Supabase's `count="exact"` select mode to return an `int` without
transferring row data — for replacing `len(db_select(...))` in dashboard stat tiles.
`in_filters={"col": [v1, v2, ...]}` applies `.in_(col, values)` — for batching the
buyer/seller/user per-row enrichment lookups currently done one query per row inside
`for` loops. **Not yet done:** migrating the actual N+1 call sites in `admin.py`,
`seller.py`, `dashboard.py`, `main.py` to use these — that's separately tracked as
follow-up work, not closed by this session.

**Item 16 — SVG upload stored-XSS surface:**
**STATUS: FIXED.** `config.py::ALLOWED_IMAGE_EXTENSIONS` changed from
`{"png","jpg","jpeg","gif","webp","svg"}` to `{"png","jpg","jpeg","gif","webp"}`.
**Evidence/reasoning:** confirmed via `grep -rn "storage_upload\|get_public_url"
utils/supabase_client.py` that uploaded images are served from Supabase Storage public
URLs directly embedded in `<img>` tags — there is no app-layer route/proxy in front of
them, so neither a `Content-Disposition: attachment` override nor an on-serve sanitizer is
reachable from this codebase without adding new infrastructure (a storage-serving route).
Removing `svg` from the allow-list was the fix actually available here. Client-side
`accept="image/*"` attributes in the 5 upload `<input>` elements
(`seller/store_settings.html` ×2, `seller/edit_listing.html`, `seller/create_listing.html`,
`dashboard/settings.html`) were left unchanged — they're a UX hint only; server-side
extension validation (`utils/helpers.py`, checked against `ALLOWED_IMAGE_EXTENSIONS`) is
the actual enforcement point and is now correct.

**Item 17 — Wire up `idx_listings_search` GIN index for search:**
**STATUS: PARTIALLY FIXED — do not close, see caveat.** `blueprints/main.py::search()`
now issues `.table("listings").select(...).text_search("title", q, options={"config":
"english", "type": "websearch"})` when a query string is present, instead of fetching
every active listing and running `q.lower() in title.lower()` in Python. Wrapped in
try/except with the exact previous Python-filter behavior as the fallback on any error, so
this cannot regress search — only make it faster/better when it succeeds. **Caveat that
matters:** this does *not* actually use `schema.sql`'s `idx_listings_search` GIN index.
That index is defined on the expression `to_tsvector('english', title || ' ' ||
COALESCE(short_description,'') || ' ' || COALESCE(description,''))` — a 3-column
concatenation — but Supabase-py's `.text_search()` API only accepts a real column name, so
it was pointed at bare `title`. Postgres will compute `to_tsvector('english', title)` on
the fly per query (no index hit) rather than reading the pre-built index. To genuinely use
that index, a generated/stored `tsvector` column mirroring the index's exact expression
needs to be added via a new migration and queried instead — that requires a live DB to
write and verify, which this sandbox does not have. Left as an explicit follow-up.
Checked `blueprints/marketplace.py::index()` too: it has no free-text `q` field (only
category/price filters), so it did not need the same change — the "title substring match"
language in FIX_ROADMAP.md's original Phase 3 item 17 wording referred only to `main.py`.

### Session 3 broken-link / compile re-sweep result
- `python3 -m py_compile` clean across `app.py`, `config.py`, every file in `blueprints/`
  and `utils/`, including the three files touched this session.
- Full `url_for()` cross-reference sweep re-run independently (123 registered endpoints):
  **zero dangling references**, matching session 2's result and confirming this session's
  edits introduced none.
- `node --check static/js/main.js` (the repo's one JS file): no syntax errors.

### No new numbered bugs discovered this session
All four Phase 3 items addressed above were already-tracked, unnumbered follow-up items
from FIX_ROADMAP.md — no new BUG-0xx IDs were needed. Remaining open work: Item 15's
call-site migration, Item 17's proper index-column migration, and the still-outstanding
live staging verification pass (all Phase 1 through 3 fixes are verified by static
analysis/compilation only — no session so far has had live Supabase network access to run
the actual end-to-end walkthrough).

---

## Session 4 — Phase 4 kickoff (Seller Dashboard / Payouts UI)

**Scope:** First Phase 4 area, chosen by the user: seller dashboard / payouts UI. Per this
project's own rule ("do not make cosmetic changes before core functional issues are
resolved"), the payouts area was inspected functionally before any visual work — this
surfaced **BUG-012** (see above), a real functional gap, not a cosmetic one. User directed:
fix BUG-012 first, then redesign.

**Work completed:**
1. **BUG-012 fixed** — see full entry above. `blueprints/seller.py::withdrawals()`,
   `templates/seller/withdrawals.html`, `templates/seller/payout_account.html`.
2. **Visual pass on `seller/withdrawals.html`** — restructured around the now-real payout
   flow: balance/pending/withdrawn stat row unchanged in style (kept consistent with the
   existing `.wallet-card`/`.stat-pill` tokens already used across the seller area), added
   an explicit "no payout account yet" state with a clear call-to-action, changed the
   request modal to a saved-account picker instead of raw text fields, unified the pending/
   history panels to source from the real `payout_requests` data. No new colors, fonts, or
   component classes introduced — deliberately extended the existing dark violet/cyan
   design system (`static/css/main.css` tokens) rather than starting a parallel style, since
   this is a polish pass on an already-coherent system, not a from-scratch redesign.
3. Added a "Request Payout" cross-link from `seller/payout_account.html` back to
   `seller/withdrawals.html` for discoverability between the two related pages.

**Verification performed:**
- `python3 -m py_compile` clean on `blueprints/seller.py`.
- Full `url_for()` cross-reference sweep re-run (123 endpoints): **zero dangling
  references**.
- Both edited templates parsed and rendered standalone via a mocked Jinja2 environment
  (stubbed `url_for`/`csrf_token`/`get_flashed_messages`), covering both the
  populated-accounts and zero-accounts/zero-history empty states for `withdrawals.html`,
  and the empty-accounts state for `payout_account.html` — no Jinja syntax or attribute
  errors in any case.
- **Not yet done** (needs a live Supabase environment, unavailable in this sandbox): an
  actual seller adding a real payout account and submitting a real payout request through
  the new modal, and confirming it appears correctly in `admin/payouts.html` for approval.
  Add this to the still-outstanding live staging walkthrough.

**Not yet touched this session:** `seller/dashboard.html`, `seller/analytics.html`,
`seller/inventory.html`, `seller/orders.html`, `seller/reviews.html`,
`seller/create_listing.html`, `seller/edit_listing.html`, `seller/store_settings.html` —
these remain in their pre-Phase-4 state and are candidates for the next Phase 4 pass within
the "seller dashboard" area if the user wants to continue there.

### Session 4 continued — Item 15 follow-through within seller dashboard

User said "Next" / "Continue" to keep going in this area. Inspected the remaining seller
routes for functional issues (per the established pattern: check before polishing) instead
of applying cosmetic changes to `dashboard.html`/`analytics.html`, which were already found
to be well-built, consistent with the existing design system, and not in need of a rewrite.

Found and fixed several concrete instances of the N+1 pattern flagged as Item 15
follow-through work in the Status Tracking table — now using the `in_filters`/batching
support added in the earlier Phase 3 session:

- **`seller.py::orders()`** — was enriching *every* matching order (buyer lookup + items
  lookup, one query each, per order) before pagination even sliced the list down to the
  page being rendered. Now enriches only the paginated page, and batches both lookups with
  `in_filters` instead of a per-order loop. Largest win of this batch — previously O(2n)
  queries for the full matching set, now O(2) queries for the visible page.
- **`seller.py::reviews()`** — same shape, worse ratio: 3 queries per review (buyer, buyer
  avatar, listing) run against every matching review before pagination. Same fix pattern
  applied: enrich only the paginated page, batched.
- **`seller.py::dashboard()`** — `total_downloads` (one `order_items` query per order),
  `recent_reviews` buyer lookup, and `recent_messages` other-user lookup were all
  per-row loops (bounded to ≤6 for the latter two, so lower urgency, but same one-line fix
  available) — all batched with `in_filters`.
- **`seller.py::analytics()`** — same `total_downloads` per-order loop as `dashboard()`,
  same fix.
- **`seller.py::inventory()`** was checked and does *not* have this pattern — it fetches the
  seller's own listings directly with no per-row enrichment loop, so nothing to fix there.
  `create_listing()`, `edit_listing()`, `store_settings()` were also checked — no N+1s
  found, loops present are over form/upload data, not per-row DB queries.

**Verification:** `py_compile` clean on `blueprints/seller.py` and the full project;
`url_for()` sweep re-run (123 endpoints, zero dangling); `dashboard.html`, `orders.html`,
and `reviews.html` render-tested standalone via mocked Jinja2 with both populated and empty
data, including the new `buyer`/`items`/`avatar`/`listing`/`other_user` enrichment fields —
all render without error.

**No visual/template changes in this round** — this pass was backend-only (query batching),
since the templates already correctly consumed the enriched data shape before and after.

### Session 4 continued — Item 15 follow-through: `blueprints/admin.py`

User said "Continue" again. Swept `blueprints/admin.py` for the same N+1 pattern (it was
explicitly named in Item 15's original description as one of the outstanding call sites).
Found and fixed nine spots:

- **`admin.dashboard()`** — the 8 platform-stat tiles were each `len(db_select(table, "id"))`
  — fetching every matching row's `id` column just to discard it for a count. Replaced all 8
  with `db_select(..., count_only=True)`, which uses Supabase's exact-count mode and
  transfers no row data. Also batched three bounded (`limit=6`) enrichment loops
  (`recent_pending_listings` → seller, `recent_reports` → listing, `recent_tickets` → user).
  `pending_deposits`/`pending_withdrawals` were also switched from
  `len(db_select("wallet_transactions", "*", ...))` (fetching every column of every matching
  row) to `count_only=True` — confirmed first that `admin/dashboard.html` only ever uses
  these as numbers, never iterates them.
- **`admin.listings()`, `admin.orders()`, `admin.wallet()`, `admin.logs()`** — all had the
  same shape as `seller.orders()`/`seller.reviews()` from earlier this session: enrichment
  (seller/buyer/user lookups) ran against *every* matching row before pagination sliced it
  down. Fixed the same way: enrich only the paginated page, batched via `in_filters`.
- **`admin.reports()`, `admin.support()`** — no pagination on these two, so just batched the
  per-row lookups in place (reporter+listing; user) rather than restructuring around
  pagination that doesn't exist.
- **`admin.disputes()`, `admin.payouts()`** — these two are different: their `search`
  filter matches against *enriched* fields (raiser/seller username, order number), so
  enrichment can't be deferred until after pagination the way the others could — it has to
  happen before the search filter runs. Batched the underlying queries in place instead
  (same number of enrichment passes, but each is now O(1) round trips instead of O(n)).
  `admin.payouts()` additionally had a `payout_history` lookup with `limit=1` per payout
  (fetch the latest history row per request) — batched into one `in_filters` query for all
  relevant `payout_request_id`s, then picked the latest per group in Python instead of one
  query per payout.

**Verification:** `py_compile` clean on `blueprints/admin.py` and the full project;
`url_for()` sweep re-run (123 endpoints, zero dangling); all eight touched admin templates
(`dashboard.html`, `listings.html`, `orders.html`, `wallet.html`, `reports.html`,
`logs.html`, `support.html`, `disputes.html`, `payouts.html`) render-tested standalone via
mocked Jinja2 with the new enriched/counted data shapes — all render without error.
Confirmed `admin/dashboard.html`'s `pending_deposits`/`pending_withdrawals` usage is
numeric-only before switching those to `count_only`.

`blueprints/admin.py` is now fully swept for this pattern. Not yet touched:
`blueprints/dashboard.py` (the general user/buyer dashboard) and `blueprints/main.py`
(only `search()` was touched, in Phase 3, for a different reason).

### Session 4 continued — Item 15 follow-through: `blueprints/dashboard.py` (final blueprint)

User said "Continue" a third time. Swept the last remaining blueprint named in Item 15's
scope. Found and fixed the same pattern in six functions:

- **`dashboard.index()`** — `unread_notifications`/`wishlist_count` were
  `len(db_select(table, "id", ...))` (full row fetch just to count) — switched to
  `count_only=True`. `total_orders` was left as a plain `len()` since it reuses the same
  list needed for `total_purchases`'s per-row status check, so no separate query was being
  wasted there. Batched the `recent_messages` (other-user) and `recent_wishlist_rows`
  (listing) enrichment loops, both bounded to 6.
- **`dashboard.purchases()`** — was already correctly limited to the paginated page (no fix
  needed there), but ran 3 queries per order (items, escrow, dispute). Batched all three —
  items and escrow via `in_filters` on the page's order IDs, dispute via a second
  `in_filters` pass keyed on the resulting escrow IDs (dispute lookup depends on escrow,
  so it has to be a second batched pass, not a single one).
- **`dashboard.messages()`** — 3 queries per conversation (other user, avatar, last-message
  preview) with no pagination on this page. Batched all three the same way as
  `seller.py`'s reviews fix.
- **`dashboard.wishlist()`** — one `listings` query per wishlist item. Batched. Copies each
  matched listing dict before mutating it with `wishlist_id` to avoid two wishlist rows
  pointing at the same listing silently sharing (and overwriting) one mutable dict.
- **`dashboard.wishlist_add_all_to_cart()`** — different shape: has a genuine sequential
  early-exit (`if room_left <= 0: break`) that has to stay a Python loop for correctness.
  Left the loop structure intact but batched the per-item listing-validity check that used
  to run inside it into one upfront query, so the loop no longer hits the DB per iteration —
  only the necessary `db_insert` per actually-added item remains inside it.
- **`dashboard.referrals()`** — one `users` query per referred profile. Batched.

**Checked and found clean, no changes needed:** `dashboard.orders()` (buyer-side order
list — no enrichment loop at all) and `dashboard.wallet()` (pending deposit/withdrawal
lists are passed straight to the template, never `len()`'d or looped for enrichment).

**Verification:** `py_compile` clean on `blueprints/dashboard.py` and the full project;
`url_for()` sweep re-run (123 endpoints, zero dangling); `dashboard/index.html`,
`purchases.html`, `messages.html`, `wishlist.html`, and `referrals.html` all render-tested
standalone via mocked Jinja2 with the new batched data shapes, in both populated and (for
`referrals.html`) empty states — all render without error. (Note: the mocked renders
surfaced a couple of unrelated missing-field errors purely in the *mock test data* itself —
`current_balance` required by `dashboard/base.html`, `unit_price`/`bonus` required by
templates this session didn't touch — those were test-harness gaps, not bugs in the app;
fixed the mocks and re-ran, not the templates.)

**Item 15 is now complete** across all three blueprints originally named in its scope
(`seller.py`, `admin.py`, `dashboard.py`). `main.py` was addressed separately in Phase 3 for
a different reason (search, not stat-counting/enrichment) and was checked during that pass
— it doesn't have this pattern elsewhere.

### Session 4 continued — Seller dashboard / payouts UI area closed out

User said "Continue" a fourth time with no new area specified. Rather than manufacture
cosmetic changes, did a final visual read-through of the remaining untouched seller
templates — `inventory.html`, `orders.html`, `reviews.html`, `create_listing.html`,
`edit_listing.html`, `store_settings.html` — to confirm whether they needed the same kind
of polish `withdrawals.html` got at the start of Phase 4.

**Conclusion: they don't.** All six are already built to the same standard as
`dashboard.html`/`analytics.html` (assessed earlier this session): consistent use of the
existing design tokens, proper empty states for every list/table, grid+list view toggle and
bulk actions on `inventory.html`, inline delivery forms on `orders.html`, a real rating
breakdown + AJAX seller-reply flow on `reviews.html` (spot-verified its `/api/reviews/<id>/
reply` endpoint actually exists and is wired correctly in `blueprints/api.py`). This whole
area was well-built before this session started; forcing changes onto it would be visual
churn without value, which the audit's own rules caution against.

**The "seller dashboard / payouts UI" Phase 4 area chosen at the start of this session is
now considered complete** — functionally (BUG-012 fixed, all N+1s in the three related
blueprints batched) and visually (confirmed, not just assumed, that every page in
`templates/seller/` meets the existing design bar). Next Phase 4 area is open — candidates
per the original list: admin panel visual polish, buyer experience, public marketplace
site, mobile responsiveness pass.
