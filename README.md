# ⚡ MercX Digital Marketplace

A production-ready marketplace for buying and selling digital products — software, templates, UI kits, source code, APIs, ebooks, and SaaS tools. Built with **Flask**, **Supabase (PostgreSQL)**, and **Tailwind CSS**, in a premium dark glassmorphism UI.

---

## Features

- **Marketplace** — browse, search, filter by category, sort, cart, checkout, coupons, secure digital downloads
- **Sellers** — create/edit/pause/delete listings, inventory management, order fulfillment (instant + manual delivery), analytics, store pages
- **Buyers** — wallet, order history, downloads with expiry/limits, wishlist, messaging, reviews, referrals
- **Admin panel** — user management, listing moderation, order/refund handling, wallet approvals, categories, coupons, reports, support tickets, audit logs, site settings, analytics
- **Security** — CSRF protection, rate limiting, password hashing, input sanitization, security headers, audit logging
- **Payments** — wallet balance, Stripe, Paystack, Flutterwave (webhook handlers included)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, Flask |
| Database | Supabase (PostgreSQL) |
| Frontend | Tailwind CSS, vanilla JS, Jinja2 |
| Charts | Chart.js |
| Icons | Feather Icons |
| Auth | Flask sessions + Werkzeug password hashing |
| Email | SMTP (HTML templates) |

---

## Project Structure

```
mercx_digital/
├── app.py                  # Flask app factory, blueprints, error handlers
├── config.py                # Environment-based configuration
├── schema.sql                # Full Supabase/Postgres schema, triggers, seed data
├── requirements.txt
├── .env.example               # Copy to .env and fill in your keys
├── blueprints/
│   ├── main.py                # Landing page, search, static pages
│   ├── auth.py                 # Login, register, password reset, email verification
│   ├── dashboard.py             # Buyer dashboard: wallet, orders, messages, settings
│   ├── seller.py                 # Seller dashboard: listings, orders, analytics
│   ├── marketplace.py             # Browse, listing detail, cart, checkout
│   ├── admin.py                    # Admin panel: users, listings, orders, wallet, etc.
│   └── api.py                       # AJAX endpoints + payment webhooks
├── utils/
│   ├── supabase_client.py           # Supabase query helpers
│   ├── decorators.py                 # @login_required, @seller_required, etc.
│   ├── helpers.py                     # Slugs, formatting, pagination, tokens
│   └── email.py                        # Transactional HTML emails
├── static/
│   ├── css/main.css                    # Design system (glassmorphism, dark mode)
│   └── js/main.js                       # Toasts, autocomplete, cart, wishlist, etc.
└── templates/                            # All Jinja2 templates (60+ pages)
```

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt --break-system-packages
```

### 2. Create a Supabase project
- Go to [supabase.com](https://supabase.com) and create a new project
- Open the **SQL Editor** and run the entire contents of `schema.sql`
- This creates all tables, indexes, triggers, default categories, and site settings

### 3. Configure environment variables
```bash
cp .env.example .env
```
Fill in at minimum:
- `SECRET_KEY` — any long random string
- `SUPABASE_URL` and `SUPABASE_SECRET_KEY` — from Supabase project settings → API
- `MAIL_*` — SMTP credentials (Gmail app password works fine for testing)

Payment gateway keys (`STRIPE_*`, `PAYSTACK_*`, `FLUTTERWAVE_*`) are optional — wallet payments work without them.

### 4. Create a storage bucket
In Supabase → Storage, create a bucket named `mercx-assets` (or match `SUPABASE_STORAGE_BUCKET` in your `.env`). Set it to public for listing images, and use signed URLs (already handled in code) for private product files.

### 5. Run the app
```bash
python app.py
```
Visit `http://localhost:5000`.

---

## Creating an Admin User

There's no seed admin — register a normal account through the UI, then promote it manually in Supabase:

```sql
update users set role = 'admin' where email = 'you@example.com';
```

Then visit `/admin` while logged in as that user.

---

## Deployment (Render / Railway / etc.)

- Set the same environment variables from `.env` in your host's dashboard
- Start command: `gunicorn app:app`
- Make sure `SESSION_COOKIE_SECURE=true` and `FLASK_ENV=production` in production

---

## Notes

- **RLS**: The schema enables Row Level Security on core tables. The Flask backend uses the Supabase **secret key** (`SUPABASE_SECRET_KEY`), which bypasses RLS — this is expected since all access control happens in the Flask app itself.
- **File delivery**: Digital files are stored in Supabase Storage and delivered via signed URLs with configurable expiry (`DOWNLOAD_EXPIRY_DAYS`) and download limits (`MAX_DOWNLOADS`).
- **Commission rate**: Set via `COMMISSION_RATE` in `.env` (default 10%), applied automatically to seller payouts on order completion.
