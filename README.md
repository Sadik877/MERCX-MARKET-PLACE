# ⚡ MercX Digital Marketplace

A production-ready marketplace for buying and selling digital products — software, templates, UI kits, source code, APIs, ebooks, and SaaS tools. Built with **Flask**, **Supabase (PostgreSQL)**, and **Tailwind CSS**, in a premium dark glassmorphism UI.

---

## Architecture

```
Browser → Flask (Blueprints) → Supabase PostgreSQL
                ↓
        Atomic DB Functions (wallet, escrow)
                ↓
        Payment Gateways (Stripe, Paystack, Flutterwave)
                ↓
        SMTP Email + In-app Notifications
```

### Blueprints

| Blueprint | Prefix | Purpose |
|---|---|---|
| `main` | `/` | Landing, search, static pages |
| `auth` | `/auth` | Login, register, password reset |
| `dashboard` | `/dashboard` | Buyer wallet, orders, downloads, messages |
| `marketplace` | `/` | Browse, listing detail, cart, checkout |
| `seller` | `/seller` | Seller dashboard, listings, analytics |
| `escrow` | `/` | Escrow lifecycle, disputes, payouts |
| `admin` | `/admin` | Full admin panel |
| `api` | `/api` | AJAX endpoints, payment webhooks |

### Project Structure

```
mercx/
├── app.py                       # Flask factory, blueprints, error handlers
├── config.py                    # Environment configuration
├── schema.sql                   # Full Postgres schema
├── requirements.txt
├── .env.example
├── blueprints/
│   ├── escrow.py                # Escrow, disputes, payouts (NEW)
│   ├── admin.py                 # Admin panel
│   ├── seller.py                # Seller hub
│   ├── dashboard.py             # Buyer dashboard
│   ├── marketplace.py           # Browse / checkout
│   ├── auth.py                  # Auth
│   ├── main.py                  # Landing
│   └── api.py                   # Webhooks + AJAX
├── migrations/
│   ├── 001_atomic_wallet_functions.sql
│   ├── 002_escrow_system.sql
│   └── 003_seller_payout_accounts.sql
├── scripts/
│   └── cron_auto_release.py     # Cron job for escrow auto-release
├── tests/
│   └── test_escrow_lifecycle.py # Escrow lifecycle test suite
├── utils/
│   ├── supabase_client.py
│   ├── decorators.py
│   ├── helpers.py
│   ├── email.py
│   └── gateways.py              # Stripe / Paystack / Flutterwave
└── templates/
    ├── admin/
    │   ├── base.html            # Admin nav (Disputes + Payouts)
    │   ├── disputes.html
    │   └── payouts.html
    ├── seller/
    │   ├── base.html            # Seller nav (Payout Accounts)
    │   └── payout_account.html
    └── dashboard/
        ├── purchases.html       # Escrow timeline + actions
        └── dispute_detail.html
```

---

## Escrow Flow

```
Checkout → escrow_create()
              │  status: held
              ↓
        Seller delivers product
              │  status: delivered  ← auto_release_at starts
              ↓
    ┌─────────┴──────────┐
    │                    │
Buyer confirms      Buyer opens dispute
    │                    │  status: disputed
status: released    Admin reviews
    │                    │
Seller paid       ┌──────┴────────┐
                  │               │
            release_seller   refund_buyer
                               (full / partial)
```

### Escrow Statuses

| Status | Description |
|---|---|
| `held` | Funds locked after checkout |
| `delivered` | Seller marked delivery; countdown started |
| `released` | Funds sent to seller wallet |
| `disputed` | Buyer opened dispute; funds frozen |
| `refunded` | Admin refunded buyer |

### Auto-Release

If the buyer does not confirm or dispute within the review window, escrow is released automatically via the cron job. The window is set by `AUTO_RELEASE_DAYS` (default: 7 days after delivery).

---

## Gateway Setup

### Stripe

```env
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

Webhook events handled: `payment_intent.succeeded`, `charge.refunded`

### Paystack

```env
PAYSTACK_SECRET_KEY=sk_live_...
PAYSTACK_WEBHOOK_SECRET=...
```

Webhook events: `charge.success`, `transfer.success`

### Flutterwave

```env
FLUTTERWAVE_SECRET_KEY=FLWSECK_...
FLUTTERWAVE_WEBHOOK_SECRET=...
```

Webhook events: `charge.completed`, `transfer.completed`

---

## Webhook Setup

1. Point your gateway's webhook URL to: `https://yourdomain.com/api/webhooks/<gateway>`
   - Stripe: `/api/webhooks/stripe`
   - Paystack: `/api/webhooks/paystack`
   - Flutterwave: `/api/webhooks/flutterwave`

2. Configure the webhook secrets in your `.env`. Each gateway signs its payloads; the handler verifies the signature before processing.

3. Webhooks are idempotent — replaying the same event reference is a no-op.

---

## Cron Setup

The `scripts/cron_auto_release.py` script calls the `/admin/escrow/auto-release` endpoint to sweep all overdue escrows and release them to sellers.

### Run manually

```bash
CRON_SECRET=your-secret BASE_URL=https://yourapp.com python scripts/cron_auto_release.py
```

### Crontab (every hour)

```cron
0 * * * * CRON_SECRET=your-secret BASE_URL=https://yourapp.com python /app/scripts/cron_auto_release.py >> /var/log/mercx-escrow.log 2>&1
```

### Render / Railway / Heroku Scheduler

Set the run command to:
```
python scripts/cron_auto_release.py
```
And add `CRON_SECRET` and `BASE_URL` to the service environment variables.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | ✓ | — | Flask session secret |
| `SUPABASE_URL` | ✓ | — | Supabase project URL |
| `SUPABASE_SECRET_KEY` | ✓ | — | Supabase secret key (`sb_secret_...`) |
| `FLASK_ENV` | — | `development` | Set to `production` for deployment |
| `MAIL_SERVER` | — | — | SMTP host |
| `MAIL_PORT` | — | `587` | SMTP port |
| `MAIL_USERNAME` | — | — | SMTP login |
| `MAIL_PASSWORD` | — | — | SMTP password |
| `MAIL_DEFAULT_SENDER` | — | — | From address |
| `STRIPE_SECRET_KEY` | — | — | Stripe secret key |
| `STRIPE_WEBHOOK_SECRET` | — | — | Stripe webhook signing secret |
| `PAYSTACK_SECRET_KEY` | — | — | Paystack secret key |
| `PAYSTACK_WEBHOOK_SECRET` | — | — | Paystack webhook signing secret |
| `FLUTTERWAVE_SECRET_KEY` | — | — | Flutterwave secret key |
| `FLUTTERWAVE_WEBHOOK_SECRET` | — | — | Flutterwave webhook signing secret |
| `COMMISSION_RATE` | — | `0.05` | Platform commission (0.05 = 5%) |
| `AUTO_RELEASE_DAYS` | — | `7` | Days after delivery before auto-release |
| `DOWNLOAD_EXPIRY_DAYS` | — | `30` | Download link validity |
| `MAX_DOWNLOADS` | — | `5` | Max download attempts per item |
| `CRON_SECRET` | — | — | Shared secret for cron endpoint auth |
| `BASE_URL` | — | `http://127.0.0.1:5000` | Used by cron script to hit the API |
| `SUPABASE_STORAGE_BUCKET` | — | `mercx-assets` | Supabase Storage bucket name |
| `SESSION_COOKIE_SECURE` | — | `false` | Set `true` in production |

---

## Testing

```bash
pip install pytest pytest-mock
pytest tests/ -v
```

The test suite in `tests/test_escrow_lifecycle.py` covers:

- Wallet funding & idempotency
- Escrow creation & platform fee calculation
- Delivery marking & auto-release timer
- Buyer manual confirmation
- Auto-release trigger logic
- Dispute opening & freezing
- Full refund
- Partial refund & amount validation
- Seller payout request & approval
- Webhook replay protection
- Gateway failure handling
- Concurrency / optimistic locking

All tests run without a live database or gateway — the entire escrow lifecycle is exercised through unit-level simulations.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Create Supabase project

- Go to [supabase.com](https://supabase.com) and create a new project
- Run `schema.sql` in the SQL Editor, then apply the three migrations in order:
  ```bash
  # In Supabase SQL Editor or via psql:
  \i migrations/001_atomic_wallet_functions.sql
  \i migrations/002_escrow_system.sql
  \i migrations/003_seller_payout_accounts.sql
  ```

### 3. Configure environment

```bash
cp .env.example .env
# Fill in required variables (see table above)
```

### 4. Create storage bucket

In Supabase → Storage, create a bucket named `mercx-assets`. Set listing images to public, use signed URLs for product files (handled automatically).

### 5. Run the app

```bash
python app.py
```

---

## Deployment

### Render / Railway

1. Set all environment variables in the service dashboard
2. Start command: `gunicorn app:app`
3. Add a cron job service pointing to `scripts/cron_auto_release.py` (hourly)

### Production checklist

- [ ] `FLASK_ENV=production`
- [ ] `SESSION_COOKIE_SECURE=true`
- [ ] `SECRET_KEY` is a long random string
- [ ] Webhook secrets configured for all active gateways
- [ ] `CRON_SECRET` set and matches the cron job environment
- [ ] Supabase RLS policies reviewed (Flask uses the secret key which bypasses RLS)
- [ ] SMTP sender configured and verified

---

## Creating an Admin User

Register normally through the UI, then promote in Supabase:

```sql
UPDATE users SET role = 'admin' WHERE email = 'you@example.com';
```

Then visit `/admin`.

---

## Notes

- **RLS**: The schema enables Row Level Security. The Flask backend uses the Supabase secret key which bypasses RLS — all access control is enforced in Flask decorators (`@login_required`, `@seller_required`, `@admin_required`).
- **Atomic operations**: Wallet debits/credits and escrow state transitions use PostgreSQL stored procedures (see `migrations/001` and `002`) to prevent race conditions.
- **Escrow is 1:1 with orders**: One escrow transaction per order (`UNIQUE(order_id)` constraint in migration 002). No separate escrow record means no funds were held (e.g. a wallet top-up order).
