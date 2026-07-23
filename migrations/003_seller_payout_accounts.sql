-- ════════════════════════════════════════════════════════════════
-- Migration 003: Seller payout accounts
--
-- Purpose: let a seller save reusable payout destinations (bank
-- account, Paystack recipient, Flutterwave bank account, Stripe
-- connected account, PayPal, crypto address) instead of re-typing
-- them on every payout_requests submission. This is purely
-- ADDITIVE — it does not alter payout_requests, whose `destination`
-- JSONB column continues to work exactly as before for ad-hoc,
-- one-off payout details. When a seller has a saved account, the
-- payout request UI copies its `details` into that same column, so
-- migrations/002's escrow_release / payout_request_approve /
-- payout_request_reject functions need no changes at all.
--
-- Apply with: psql <connection> -f migrations/003_seller_payout_accounts.sql
-- ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.seller_payout_accounts (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id    UUID NOT NULL REFERENCES public.users(id),
    method       VARCHAR(30) NOT NULL,
        -- bank_transfer | paystack | flutterwave | stripe | paypal | crypto
    label        VARCHAR(100),                          -- seller-facing nickname, e.g. "GTBank NGN"
    details      JSONB NOT NULL DEFAULT '{}'::jsonb,
        -- shape depends on `method`, matches utils/gateways.py::gateway_payout() destination:
        --   bank_transfer: {account_name, account_number, bank_name}
        --   paystack:      {account_name, account_number, bank_code, bank_name, recipient_code?}
        --   flutterwave:   {account_name, account_number, account_bank, bank_name}
        --   stripe:        {stripe_account_id}
        --   paypal:        {paypal_email}
        --   crypto:        {network, address}
    is_default   BOOLEAN NOT NULL DEFAULT false,
    is_verified  BOOLEAN NOT NULL DEFAULT false,         -- set true once a real gateway payout succeeds against it
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_seller_payout_accounts_seller ON public.seller_payout_accounts(seller_id);

-- Only one default account per seller.
CREATE UNIQUE INDEX IF NOT EXISTS idx_seller_payout_accounts_one_default
    ON public.seller_payout_accounts(seller_id) WHERE is_default;
