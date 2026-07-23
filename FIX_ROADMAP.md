# FIX ROADMAP — MercX Digital Marketplace

Companion to `FORENSIC_AUDIT_REPORT.md` and `BUG_INVENTORY.md`. Diagnosis only has been done —
**no fixes have been applied yet**, per instructions. This file sequences the work.

Do not skip ahead to Phase 4 (frontend redesign) until Phases 1–2 are actually fixed, verified
against a running instance, and this file is updated to mark them done.

---

## PHASE 1 — CRITICAL / BLOCKING
*Goal: nothing 500s on a core, common path; production shows the correct error UI; the three
user-reported symptoms are eliminated.*

1. **BUG-005** — Fix `config.py`: default `FLASK_ENV` to `"production"` instead of
   `"development"`; stop `DevelopmentConfig` from hardcoding `DEBUG=True` regardless of
   `FLASK_DEBUG`. This single fix directly addresses both the blank-admin-page symptom and the
   generic-error-page symptom. **Do this first** — it also makes every subsequent fix's real
   errors (if any slip through review) visible on the correct custom page instead of a bare
   500, which makes the rest of this phase easier to verify.
2. **BUG-001** — Fix the two broken `python_slugify` imports (`blueprints/seller.py`,
   `blueprints/dashboard.py`) — swap for the existing `make_slug()` helper. Unblocks Store
   Creation and Store Settings entirely.
3. **BUG-002** — Fix the three `url_for("seller.wallet")` calls in
   `blueprints/escrow.py::request_payout()` → `url_for("seller.withdrawals")`. Unblocks seller
   payout requests entirely.
4. **BUG-004** — Add the missing `if not secret: return jsonify(...), 400` guard to
   `paystack_webhook()` in `blueprints/api.py`, matching the Stripe/Flutterwave handlers.
   Financial-integrity/security fix; small and isolated.
5. **Verification step (required before moving to Phase 2):** with FLASK_ENV correctly forcing
   production config in a staging environment, deliberately trigger a few known-safe error
   conditions (e.g. hit a nonexistent listing id) and confirm `errors/404.html`/`500.html`
   actually render, then walk through: register → verify (once BUG-006 email vars are set,
   see Phase 2) → become a seller → create a listing → buy it as a second account → confirm
   receipt → seller requests a payout. This end-to-end pass is the real acceptance test for
   this phase, not just "the code compiles."

---

## PHASE 2 — HIGH PRIORITY
*Goal: every documented major feature works; broken links are gone; seller/product/checkout
flows are fully functional.*

6. **BUG-003** — Restore the admin support-ticket detail/reply routes in `admin.py`
   (`GET /support/<ticket_id>`, `POST /support/<ticket_id>/reply`) and add the missing "View"
   link from `admin/support.html` into it. `support_tickets`/`ticket_messages` tables already
   exist — this is pure blueprint code, no migration needed.
7. **BUG-006** — Update `.env.example` and `README.md` to drop the stale `MAIL_*` SMTP block
   and document `BREVO_API_KEY`/`BREVO_TIMEOUT` instead, so a fresh deployment's transactional
   email (verification, password reset, order/sale/payout/dispute notifications) actually works
   out of the box.
8. **BUG-007** — Fix the buyer-scoped "already purchased" check in
   `marketplace.py::listing_detail()` so it correctly checks the *current user's* completed
   orders rather than an unscoped, `.single()`-fragile query across all buyers of the listing.
   Note: this does not touch actual download-access control, which is already correct and
   separately enforced in `dashboard.py::download_item()` — scope this fix narrowly to the
   product-page "already own this" indicator.
9. **BUG-010** — Reconcile `schema.sql` with `migrations/002` and `migrations/003` (merge them
   into a single current-state schema, or clearly document the required apply order) so a fresh
   environment can actually be bootstrapped correctly. Do this once escrow is confirmed stable
   in Phase 1's verification pass, not before (avoid moving a schema target while still
   debugging Phase 1).
10. Full broken-link re-sweep: re-run the same `url_for()` cross-reference approach used in this
    audit after Phases 1–2 land, to catch any *new* dangling reference introduced while fixing
    the above (cheap, mechanical, ~seconds to run — see the script embedded in the audit
    session; keep it as a standing lint check if convenient going forward).

---

## PHASE 3 — MEDIUM PRIORITY
*Goal: consistency, correctness under load, defensive hardening. Nothing here blocks a normal
user from using the app today, but all of it will bite eventually.*

11. **BUG-008** — Fix the `referral_count` hardcoded-to-`1` bug (increment instead of overwrite,
    ideally via an atomic RPC to match the codebase's existing hardening pattern for other
    counters/balances).
12. **BUG-009** — Add a themed `@app.errorhandler(401)` for parity with 403/404/429/500.
13. **BUG-011** — Add a `render.yaml`/`Procfile` pinning the Gunicorn start command, worker
    count, and timeout, so production deployment is reproducible from the repo alone.
14. Bring `admin.py::refund_order()`'s wallet-balance update in line with the atomic-RPC pattern
    already used by `wallet_credit_idempotent`/`wallet_tx_approve_atomic` elsewhere, closing the
    one remaining manual read-then-write balance path.
15. Add `count`-only and `.in_()` capabilities to `utils/supabase_client.py::db_select()`, then
    use them to replace the "fetch the whole table just to call `len()`" pattern in
    `admin.dashboard()`, `main.index()`, `seller.dashboard()`/`analytics()`, and the various
    per-row enrichment loops (buyer/seller/user lookups inside `for` loops over orders/
    reviews/listings) — this is a real scalability concern, not urgent today but worth doing
    before data volume grows.
16. SVG upload hardening (`ALLOWED_IMAGE_EXTENSIONS` includes `svg`) — either sanitize on
    upload or serve user-uploaded SVGs non-inline (`Content-Disposition: attachment`) to close
    the stored-XSS surface.
17. Move product/listing search (`main.py::search()`, `marketplace.py::index()`'s title
    substring match) from in-Python filtering to the DB-side `to_tsvector` GIN index that
    `schema.sql` already defines but nothing currently queries against.

---

## PHASE 4 — FRONTEND REDESIGN
**Do not start until Phases 1–2 are fixed, deployed, and verified.** Explicitly out of scope
for this audit pass. Once unblocked, likely candidates based on what this audit surfaced (for
planning purposes only, not commitments):
- Admin panel visual pass (functionally sound once Phase 1 lands — sidebar/nav/inheritance
  structure is solid and doesn't need rework, just visual polish).
- Seller dashboard / payouts UI, once the payout-request flow is actually reachable (Phase 1
  item 3).
- Buyer experience polish around the "already purchased" / download states, once Phase 2 item 8
  lands.
- Mobile responsiveness pass across all of the above.

---

## Status Tracking

Update this table as work lands. Do not remove rows.

| Bug ID | Phase | Status |
|---|---|---|
| BUG-005 | 1 | **Fixed** — `config.py`: `get_config()` now defaults to `production`; `DevelopmentConfig` no longer hardcodes `DEBUG=True`. Startup log line added in `app.py` printing active config + DEBUG + FLASK_ENV. |
| BUG-001 | 1 | **Fixed** — both broken `python_slugify` imports (`seller.py::store_settings`, `dashboard.py::become_seller`) replaced with the existing `make_slug(..., suffix=False)` helper. |
| BUG-002 | 1 | **Fixed** — all 3 `url_for("seller.wallet")` calls in `escrow.py::request_payout()` corrected to `url_for("seller.withdrawals")`. |
| BUG-004 | 1 | **Fixed** — `paystack_webhook()` now rejects with 400 if `PAYSTACK_SECRET_KEY` is unconfigured, before computing/comparing the HMAC, matching Stripe/Flutterwave. |
| BUG-003 | 2 | **Fixed** — added `GET /admin/support/<ticket_id>` (`ticket_detail`) and `POST /admin/support/<ticket_id>/reply` (`reply_ticket`) to `blueprints/admin.py`; added "View" link column to `templates/admin/support.html`. |
| BUG-006 | 2 | **Fixed** — `.env.example` stale SMTP block replaced with Brevo API section (`BREVO_API_KEY`, `BREVO_TIMEOUT`, `MAIL_DEFAULT_SENDER`); deprecated SMTP vars documented as removed. `FLASK_ENV` default changed from `development` to `production` in `.env.example` to match code default. |
| BUG-007 | 2 | **Fixed** — `marketplace.py::listing_detail()` `already_bought` check now scoped to the current buyer's completed orders only; no longer uses a naked `listing_id`-only query with `single=True` that silently returns `None` for any listing with >1 sale. |
| BUG-010 | 2 | **Fixed** — all 9 escrow/payout tables (`escrow_transactions`, `escrow_events`, `escrow_holds`, `disputes`, `dispute_messages`, `payout_requests`, `payout_history`, `webhook_events`, `seller_payout_accounts`) appended to `schema.sql` using `CREATE TABLE IF NOT EXISTS` (safe to re-apply on existing DBs). Schema is now bootstrappable from a single file. |
| BUG-008 | 3 | **Already Fixed** in this zip — `auth.py::register()` fetches current `referral_count` and increments it atomically; the hardcoded `1` was removed in a prior session. Verified via `grep -n "referral_count" blueprints/auth.py`. |
| BUG-009 | 3 | **Fixed** — added `@app.errorhandler(401)` to `app.py` returning `errors/403.html` with status 401, for parity with 403/404/429/500. |
| BUG-011 | 3 | **Fixed** — added `Procfile` (`gunicorn app:app --workers 3 --timeout 60 --log-file - --access-logfile -`) and `render.yaml` (service type, build/start commands, all env var stubs) to repo root. |
| Item 14 (refund_order atomic RPC) | 3 | **Already Fixed** (found already resolved this session, session 3) — `admin.py::refund_order()` now calls `wallet_credit_idempotent(...)`, the same row-locked, idempotent RPC used by the payment webhooks, keyed on `REFUND-{order_id}`. No remaining manual read-then-write balance path in this function. Not a regression from this session — was already correct in the uploaded zip; the roadmap just hadn't been updated to reflect it. |
| Item 15 (`db_select` count/`.in_()`) | 3–4 | **COMPLETE (session 3–4).** `db_select()`'s `count_only`/`in_filters` params (session 3) are now in active use across all three blueprints named in this item's original scope — `seller.py`, `admin.py`, and `dashboard.py` (session 4) — see BUG_INVENTORY.md Session 4 sections for full per-function detail. Pattern applied throughout: `count_only=True` replacing `len(db_select(...))` for pure counts; enrichment moved to after pagination and batched via `in_filters` where pagination exists; batched in place where it doesn't (or where a search filter depends on the enriched fields, so enrichment can't be deferred). `main.py` doesn't have this pattern elsewhere (checked during the Phase 3 pass) — its own Item 15-adjacent work was the `search()` DB-side query change, tracked separately as Item 17. |
| Item 16 (SVG upload hardening) | 3 | **Fixed** (session 3) — `config.py::ALLOWED_IMAGE_EXTENSIONS` no longer includes `svg`. Chose extension removal over sanitize-on-upload or `Content-Disposition` headers because uploaded images are served directly from Supabase Storage public URLs embedded in `<img>` tags with no app-layer proxy in front of them to add headers or run a sanitizer at serve time — removing SVG from the allow-list was the only fix reachable from this codebase without adding new infrastructure. Client-side `accept="image/*"` attributes in upload forms were left as-is (they're just a UX hint; the server-side extension check is the actual enforcement point and is now correct). |
| Item 17 (wire up GIN search index) | 3 | **Partially addressed** (session 3) — `blueprints/main.py::search()` now queries Postgres via `.text_search()` when `q` is present, instead of fetching every active listing and substring-matching in Python. **Important caveat, do not mark this fully done:** this does *not* actually hit `schema.sql`'s `idx_listings_search` GIN index, because that index is built on a concatenated `title || short_description || description` expression, and Supabase-py's `.text_search()` only accepts a real column name — it was pointed at `title` alone. Real index usage needs a generated/stored `tsvector` column mirroring the index expression, added via a new migration, which needs a live DB to write and verify safely (not available in this sandbox) — left as a clearly-flagged follow-up. The change made today is still a net improvement and is safe: it's wrapped in try/except with a fallback to the exact previous Python-filtering behavior on any error, so it cannot make search worse, only faster/better when the DB call succeeds. `blueprints/marketplace.py::index()` was checked and does **not** need the same change — it only filters by category/price, it has no free-text field.  |
| BUG-012 | 4 | **Fixed** (session 4) — discovered while starting Phase 4 seller-dashboard work: the real, gateway-aware payout pipeline (`escrow.request_payout` / `seller_payout_accounts` / `payout_requests`, with a full admin review UI already built) was never linked from any template — sellers could only reach the disconnected legacy `dashboard.wallet_withdraw` flow. Fixed by wiring `seller/withdrawals.html`'s request modal to `escrow.request_payout` with a saved-account picker, and updating `seller.withdrawals()` to supply accounts + a unified (legacy + new) history. See BUG_INVENTORY.md BUG-012 for full detail. |

### Phase 1 verification performed

- Re-ran the mechanical `url_for()` vs. registered-endpoint cross-check used in the original
  audit: **zero** dangling references remain from BUG-001/BUG-002. The only endpoint still
  missing is `admin.reply_ticket` (BUG-003), which is explicitly Phase 2 scope — expected.
- `python3 -m py_compile` passed clean on all five touched files
  (`config.py`, `app.py`, `blueprints/seller.py`, `blueprints/dashboard.py`,
  `blueprints/escrow.py`, `blueprints/api.py`).
- `grep -rn "python_slugify"` across the repo now returns no results — confirms both call sites
  were caught, not just one.
- **Not yet done** (needs a real Supabase-backed environment, not available in this sandbox):
  the live walkthrough described in item 5 of Phase 1 above (register → verify → become a
  seller → create a listing → buy it → confirm receipt → request payout). Recommend running
  that pass in staging before considering Phase 1 fully closed, since static analysis can
  confirm these specific bugs are gone but can't substitute for one real end-to-end run.

---

### Phase 2 + Phase 3 verification performed (session 2)

- `python3 -m py_compile` passed clean on all three modified Python files:
  `blueprints/admin.py`, `blueprints/marketplace.py`, `app.py`.
- Full `url_for()` cross-reference re-sweep run against the correct endpoint registry
  (all 7 blueprints, all decorated `def` functions): **zero dangling references** remain.
  The sweep now correctly identifies `admin.ticket_detail` and `admin.reply_ticket` as
  present (they were just added). Complete list of registered endpoints confirmed accurate.
- `schema.sql` now contains all 9 escrow/payout tables. `CREATE TABLE IF NOT EXISTS` makes
  the appended section idempotent — safe to run against a DB that already has migrations 002/003.
- BUG-008 confirmed already fixed (not a regression from this session).
- BUG-009 confirmed: `errorhandler(401)` added. **Note:** the 401 handler reuses
  `errors/403.html` since the theme conveys "you are not permitted here" — a dedicated
  `errors/401.html` can be added in Phase 4 for a more precise UX.
- BUG-011 confirmed: `Procfile` and `render.yaml` created with reproducible Gunicorn config.
- `.env.example` `FLASK_ENV` corrected to `production` to match the Phase 1 code default.
- **Not yet done** (still requires a live Supabase-backed environment):
  - End-to-end walkthrough of Phase 1 items (noted in Phase 1 verification as still pending).
  - Admin support-ticket live test (Phase 2 / BUG-003): click "View" on a ticket, send a
    reply, change status — verify round-trip works and message thread renders.
  - Verify `already_bought` indicator on listing page for a multi-buyer listing
    (Phase 2 / BUG-007): requires placing an order as two separate buyer accounts.

---

## Session 3 — Incremental Update

**Continued from:** the "NEXT RECOMMENDED STEP" section below as it stood at the end of
session 2 — Phase 3 items 14–17 (all previously unstarted/non-blocking, listed as the
next recommended actions).

**Verification performed first (re-confirming session 2's claims, not re-diagnosing):**
- `python3 -m py_compile` clean on every `.py` file in `app.py`, `config.py`, `blueprints/`,
  `utils/` — confirms BUG-001 through BUG-011 fixes are actually present in this zip, not
  just described as fixed.
- Full `url_for()` vs. registered-endpoint mechanical sweep re-run fresh (123 endpoints,
  independent implementation from session 2's): **zero dangling references.** Confirms
  Phase 2 item 10 (broken-link re-sweep) is still clean after this session's edits too.
- `node --check` on the one JS file in the repo (`static/js/main.js`): no syntax errors.
- Spot-checked `blueprints/api.py::paystack_webhook()`, `app.py` error handlers, `config.py`
  FLASK_ENV default, and `admin.py::refund_order()` directly against the source (not just
  trusting the status table) — all matched what BUG_INVENTORY.md/FIX_ROADMAP.md claimed.

**Work completed this session:** Phase 3 items 14 (confirmed already fixed, not a
regression), 15 (fixed — additive), 16 (fixed), 17 (partially addressed, capped and
explained above). See the Status Tracking table above for full detail per item.

**Files changed this session:**
- `utils/supabase_client.py` — `db_select()` gained `count_only` and `in_filters` params.
- `config.py` — removed `svg` from `ALLOWED_IMAGE_EXTENSIONS`.
- `blueprints/main.py` — `search()` now tries DB-side `.text_search()` before falling back
  to the original Python-filter path; added `get_supabase`/`current_app` imports.

**No new numbered bugs found.** This session's own edits were compile-checked and swept
for dangling routes/imports; no new BUG-0xx entries were needed in `BUG_INVENTORY.md`.

---

## Session 4 — Phase 4: Seller Dashboard / Payouts UI (COMPLETE)

User directed continuation into Phase 4, choosing seller dashboard / payouts UI as the
starting area. Before any visual work, functional inspection surfaced **BUG-012** (real
payout pipeline never linked from any template — see BUG_INVENTORY.md). User chose to fix
it first, then redesign. Both done — see BUG-012 row above and the Session 4 sections of
BUG_INVENTORY.md for full detail.

Also used this area as the vehicle to finish Item 15 (N+1/count-query cleanup) across all
three related blueprints (`seller.py`, `admin.py`, `dashboard.py`) — see rows above.

Every template in `templates/seller/` has now been either fixed+redesigned
(`withdrawals.html`, `payout_account.html` cross-link) or explicitly checked and confirmed
to already meet the design bar (`dashboard.html`, `analytics.html`, `inventory.html`,
`orders.html`, `reviews.html`, `create_listing.html`, `edit_listing.html`,
`store_settings.html`) — see BUG_INVENTORY.md's "Seller dashboard / payouts UI area closed
out" note for the reasoning on why the latter group didn't get cosmetic changes.

**This Phase 4 area is done.** Next area is open.

## NEXT RECOMMENDED STEP (for next session)

1. **Live staging verification pass** — still the single biggest gap, unchanged since
   session 3. Now also specifically needs: submitting a real payout request through the new
   `seller/withdrawals.html` modal against a real saved payout account, and confirming it
   shows correctly in `admin/payouts.html` for approval (BUG-012's fix has only been
   verified by template rendering + compile checks, not a live round-trip). Also worth
   spot-checking the count_only()/in_filters() batching changes against real Supabase
   responses, since count="exact" mode and .in_() haven't been exercised against a live
   instance in this sandbox either.
2. **Pick the next Phase 4 area** — user has not yet indicated priority. Candidates per the
   original list: admin panel visual polish, buyer experience (product pages, purchases,
   downloads), public marketplace site (homepage, listing/search pages), mobile
   responsiveness pass across everything.
3. **Item 17 follow-through:** add a migration creating a generated/stored `tsvector`
   column matching `idx_listings_search`'s expression (`title || short_description ||
   description`), and point `.text_search()` at that column instead of bare `title`, so the
   existing GIN index is actually used. Needs a live DB to write and verify.
4. **Item 15 — COMPLETE.** No further follow-through needed unless a future session finds
   new N+1s introduced by other work.

---

## Session 5 — Phase 4: Buyer Experience (functional audit)

**Continued from:** Session 4's "NEXT RECOMMENDED STEP" — chose buyer experience as the next
Phase 4 area. Ran a full functional audit before any visual work.

**Work completed:**

Three new bugs found and fixed (see BUG_INVENTORY.md Session 5 section for full detail):

| Bug | File(s) | Status |
|---|---|---|
| BUG-013 | `dashboard.py::messages()`, `listing.html`, `seller_store.html` | **FIXED** — `?with=<seller_id>` handler added; template links updated |
| BUG-014 | `dashboard.py::send_message()` | **FIXED** — correct unread_count column + real increment instead of hardcoded `1` |
| BUG-015 | `app.py::inject_globals()` | **FIXED** — `count_only=True` for cart and notification badge counts (fires on every page) |

**Verification:**
- `py_compile` clean on `blueprints/dashboard.py` and `app.py`
- Full `url_for()` sweep (123 endpoints): zero dangling references

**Not yet done (buyer experience):**
- Visual polish pass on `dashboard/wallet.html`, `dashboard/orders.html`,
  `marketplace/cart.html`, `marketplace/checkout.html` — functionally sound, not yet reviewed
  for design consistency.
- `templates/index.html` and `templates/base.html` visual review (public site / landing page).
- Mobile responsiveness pass — deferred from every prior session, still open.
- Live staging walkthrough — still the biggest unverified gap (needs real Supabase environment).
- Item 17 GIN index migration (needs live DB).

## NEXT RECOMMENDED STEP (for next session)

1. **Continue buyer experience** — audit and optionally polish: `dashboard/wallet.html`,
   `dashboard/orders.html`, `marketplace/cart.html`, `marketplace/checkout.html`.
2. **Public site** — `templates/index.html` and `templates/base.html` visual review.
3. **Live staging verification pass** — still the largest unverified gap. Specifically:
   - End-to-end payout request via `seller/withdrawals.html` modal (BUG-012 fix)
   - `count_only()`/`in_filters()` batching against real Supabase responses
   - "Message Seller" → new conversation flow (BUG-013 fix)
4. **Item 17** — add migration for generated `tsvector` column matching `idx_listings_search`
   expression, then point `.text_search()` at it. Needs live DB.
5. **Mobile responsiveness** — deferred again; candidate for a dedicated session.
