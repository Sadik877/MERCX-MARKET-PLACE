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
| BUG-003 | 2 | Not started |
| BUG-006 | 2 | Not started |
| BUG-007 | 2 | Not started |
| BUG-010 | 2 | Not started |
| BUG-008 | 3 | Not started |
| BUG-009 | 3 | Not started |
| BUG-011 | 3 | Not started |

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
