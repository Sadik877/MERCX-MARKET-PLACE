-- ════════════════════════════════════════════════════════════════
-- Migration 002: Escrow system
--
-- Purpose: replace "credit seller the instant an item is delivered"
-- with a proper escrow lifecycle:
--
--   Buyer pays -> Funds Held -> Seller Delivers -> Buyer Confirms
--                                                -> Funds Released
--                                        (or Automatic Release)
--
--   Dispute at any point before release -> Freeze Funds
--                                        -> Admin Decision
--                                        -> Refund Buyer /
--                                           Release Seller /
--                                           Partial Refund
--
-- This migration is purely ADDITIVE (new tables + new functions only).
-- It does not alter orders/users/wallet_transactions. It reuses the
-- row-locking pattern established in migrations/001 so escrow moves
-- money through the exact same audited, idempotent mechanism.
--
-- Apply with: psql <connection> -f migrations/002_escrow_system.sql
-- ════════════════════════════════════════════════════════════════

-- ── 1. Tables ──────────────────────────────────────────────────

-- One row per order (per seller, since orders are already split by
-- seller at checkout). Tracks where the buyer's payment currently
-- sits in the escrow lifecycle.
CREATE TABLE IF NOT EXISTS public.escrow_transactions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id             UUID NOT NULL REFERENCES public.orders(id),
    buyer_id             UUID NOT NULL REFERENCES public.users(id),
    seller_id            UUID NOT NULL REFERENCES public.users(id),
    amount               DECIMAL(15,2) NOT NULL CHECK (amount > 0),
    platform_fee         DECIMAL(15,2) NOT NULL DEFAULT 0,
    seller_earnings      DECIMAL(15,2) NOT NULL DEFAULT 0,
    refunded_amount      DECIMAL(15,2) NOT NULL DEFAULT 0,
    released_amount      DECIMAL(15,2) NOT NULL DEFAULT 0,
    payment_method       VARCHAR(30)  NOT NULL,
    payment_reference    VARCHAR(100) NOT NULL,          -- dedup key for the inbound payment
    status               VARCHAR(20)  NOT NULL DEFAULT 'held',
        -- held | delivered | disputed | released | refunded | partial_refunded | cancelled
    auto_release_at      TIMESTAMPTZ,                     -- set once delivered; auto-release deadline
    held_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at         TIMESTAMPTZ,
    released_at          TIMESTAMPTZ,
    refunded_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (payment_reference),
    UNIQUE (order_id)
);
CREATE INDEX IF NOT EXISTS idx_escrow_tx_status       ON public.escrow_transactions(status);
CREATE INDEX IF NOT EXISTS idx_escrow_tx_auto_release  ON public.escrow_transactions(auto_release_at) WHERE status = 'delivered';
CREATE INDEX IF NOT EXISTS idx_escrow_tx_buyer         ON public.escrow_transactions(buyer_id);
CREATE INDEX IF NOT EXISTS idx_escrow_tx_seller        ON public.escrow_transactions(seller_id);

-- Full audit trail: every state transition, who caused it, and why.
CREATE TABLE IF NOT EXISTS public.escrow_events (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    escrow_transaction_id UUID NOT NULL REFERENCES public.escrow_transactions(id),
    event_type           VARCHAR(40) NOT NULL,
        -- held | delivered | buyer_confirmed | auto_released | released |
        -- dispute_opened | dispute_resolved | refunded | partial_refunded | cancelled
    actor_id             UUID REFERENCES public.users(id),  -- NULL for system/cron actions
    from_status          VARCHAR(20),
    to_status             VARCHAR(20),
    amount               DECIMAL(15,2),
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_escrow_events_tx ON public.escrow_events(escrow_transaction_id, created_at);

-- The actual fund-hold ledger row (separate from escrow_transactions
-- so the hold amount / movements are auditable independently of the
-- lifecycle status, and so partial releases have their own ledger).
CREATE TABLE IF NOT EXISTS public.escrow_holds (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    escrow_transaction_id UUID NOT NULL REFERENCES public.escrow_transactions(id),
    held_amount          DECIMAL(15,2) NOT NULL CHECK (held_amount > 0),
    remaining_amount      DECIMAL(15,2) NOT NULL,
    status               VARCHAR(20) NOT NULL DEFAULT 'active',
        -- active | released | refunded | partially_released
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_escrow_holds_tx ON public.escrow_holds(escrow_transaction_id);

-- Disputes opened against an escrow transaction.
CREATE TABLE IF NOT EXISTS public.disputes (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    escrow_transaction_id UUID NOT NULL REFERENCES public.escrow_transactions(id),
    order_id             UUID NOT NULL REFERENCES public.orders(id),
    raised_by            UUID NOT NULL REFERENCES public.users(id),
    against_id           UUID NOT NULL REFERENCES public.users(id),
    reason               VARCHAR(50) NOT NULL,
    description          TEXT,
    status               VARCHAR(20) NOT NULL DEFAULT 'open',
        -- open | under_review | resolved
    resolution           VARCHAR(20),
        -- refund_buyer | release_seller | partial_refund
    resolution_amount    DECIMAL(15,2),
    resolution_note      TEXT,
    resolved_by          UUID REFERENCES public.users(id),
    resolved_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_disputes_status ON public.disputes(status);
CREATE INDEX IF NOT EXISTS idx_disputes_escrow ON public.disputes(escrow_transaction_id);

-- Back-and-forth thread on a dispute (buyer, seller, admin).
CREATE TABLE IF NOT EXISTS public.dispute_messages (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dispute_id   UUID NOT NULL REFERENCES public.disputes(id),
    sender_id    UUID NOT NULL REFERENCES public.users(id),
    message      TEXT NOT NULL,
    attachments  JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_admin_note BOOLEAN NOT NULL DEFAULT false,   -- internal note, hidden from both parties
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dispute_messages_dispute ON public.dispute_messages(dispute_id, created_at);

-- Seller payout requests (distinct from a buyer's wallet withdrawal
-- request — this is specifically "pay out my released escrow
-- earnings to my bank/mobile money/etc").
CREATE TABLE IF NOT EXISTS public.payout_requests (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seller_id         UUID NOT NULL REFERENCES public.users(id),
    amount            DECIMAL(15,2) NOT NULL CHECK (amount > 0),
    method            VARCHAR(30) NOT NULL,
    destination       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- bank/mobile-money/paypal details
    status            VARCHAR(20) NOT NULL DEFAULT 'pending',
        -- pending | approved | processing | paid | rejected | failed
    reference         VARCHAR(100) NOT NULL UNIQUE,
    admin_id          UUID REFERENCES public.users(id),
    notes             TEXT,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payout_requests_seller ON public.payout_requests(seller_id);
CREATE INDEX IF NOT EXISTS idx_payout_requests_status ON public.payout_requests(status);

-- Immutable record of completed/failed payout attempts (a payout
-- request can have >1 history row if a gateway attempt fails and is
-- retried).
CREATE TABLE IF NOT EXISTS public.payout_history (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payout_request_id UUID NOT NULL REFERENCES public.payout_requests(id),
    seller_id         UUID NOT NULL REFERENCES public.users(id),
    amount            DECIMAL(15,2) NOT NULL,
    method            VARCHAR(30) NOT NULL,
    status            VARCHAR(20) NOT NULL,   -- paid | failed
    gateway_reference VARCHAR(150),
    failure_reason    TEXT,
    processed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_payout_history_request ON public.payout_history(payout_request_id);

-- Every inbound webhook delivery from every gateway, logged BEFORE
-- business logic runs. This is what gives us replay protection that
-- is independent of the financial-reference idempotency already
-- built into wallet_credit_idempotent: a gateway's raw event id is
-- unique-constrained here, so even a non-financial or malformed
-- replay can't be processed twice, and every delivery (valid or not)
-- is auditable.
CREATE TABLE IF NOT EXISTS public.webhook_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gateway          VARCHAR(20) NOT NULL,        -- stripe | paystack | flutterwave
    event_id         VARCHAR(150) NOT NULL,       -- gateway's own unique event/transaction id
    event_type       VARCHAR(100),
    payload          JSONB NOT NULL,
    signature_valid  BOOLEAN NOT NULL,
    processed        BOOLEAN NOT NULL DEFAULT false,
    processing_error TEXT,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ,
    UNIQUE (gateway, event_id)
);
CREATE INDEX IF NOT EXISTS idx_webhook_events_gateway ON public.webhook_events(gateway, received_at);


-- ── 2. Helper: record an escrow event ─────────────────────────
CREATE OR REPLACE FUNCTION public._escrow_log_event(
    p_escrow_id   UUID,
    p_event_type  VARCHAR,
    p_actor_id    UUID,
    p_from_status VARCHAR,
    p_to_status   VARCHAR,
    p_amount      DECIMAL,
    p_metadata    JSONB DEFAULT '{}'::jsonb
) RETURNS VOID AS $$
BEGIN
    INSERT INTO public.escrow_events
        (escrow_transaction_id, event_type, actor_id, from_status, to_status, amount, metadata)
    VALUES
        (p_escrow_id, p_event_type, p_actor_id, p_from_status, p_to_status, p_amount, p_metadata);
END;
$$ LANGUAGE plpgsql;


-- ── 3. Record the webhook delivery (replay protection) ────────
-- Returns is_new=TRUE if this (gateway, event_id) has never been seen
-- before (caller should process it). If it HAS been seen, returns
-- is_new=FALSE plus was_processed: TRUE means it already completed
-- successfully (caller must skip — this is a true replay), FALSE
-- means the previous attempt errored out before finishing (caller
-- should safely retry — the downstream wallet_credit_idempotent
-- reference key ensures that retry still can't double-credit).
CREATE OR REPLACE FUNCTION public.webhook_event_record(
    p_gateway         VARCHAR,
    p_event_id        VARCHAR,
    p_event_type      VARCHAR,
    p_payload         JSONB,
    p_signature_valid BOOLEAN
) RETURNS TABLE (is_new BOOLEAN, id UUID, was_processed BOOLEAN) AS $$
DECLARE
    v_id UUID;
    v_existing RECORD;
BEGIN
    INSERT INTO public.webhook_events (gateway, event_id, event_type, payload, signature_valid)
    VALUES (p_gateway, p_event_id, p_event_type, p_payload, p_signature_valid)
    ON CONFLICT (gateway, event_id) DO NOTHING
    RETURNING public.webhook_events.id INTO v_id;

    IF v_id IS NULL THEN
        SELECT we.id, we.processed INTO v_existing
        FROM public.webhook_events we
        WHERE we.gateway = p_gateway AND we.event_id = p_event_id;
        RETURN QUERY SELECT false, v_existing.id, COALESCE(v_existing.processed, false);
    ELSE
        RETURN QUERY SELECT true, v_id, false;
    END IF;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION public.webhook_event_mark_processed(
    p_id UUID, p_error TEXT DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
    UPDATE public.webhook_events
    SET processed = (p_error IS NULL), processing_error = p_error, processed_at = now()
    WHERE id = p_id;
END;
$$ LANGUAGE plpgsql;


-- ── 4. Create the hold (buyer's payment captured -> funds held) ──
-- Idempotent on payment_reference (also enforced by the orders.id
-- UNIQUE and payment_reference UNIQUE constraints above): if this
-- exact order already has an escrow transaction, it's a no-op that
-- returns the existing row instead of raising.
CREATE OR REPLACE FUNCTION public.escrow_hold_create(
    p_order_id          UUID,
    p_buyer_id          UUID,
    p_seller_id         UUID,
    p_amount            DECIMAL(15,2),
    p_platform_fee      DECIMAL(15,2),
    p_seller_earnings   DECIMAL(15,2),
    p_payment_method    VARCHAR(30),
    p_payment_reference VARCHAR(100),
    p_instant_delivery  BOOLEAN DEFAULT false,
    p_auto_release_hours INTEGER DEFAULT 72
) RETURNS TABLE (
    escrow_id UUID, already_processed BOOLEAN, status VARCHAR
) AS $$
DECLARE
    v_existing RECORD;
    v_id UUID;
BEGIN
    SELECT * INTO v_existing FROM public.escrow_transactions
    WHERE order_id = p_order_id FOR UPDATE;

    IF FOUND THEN
        RETURN QUERY SELECT v_existing.id, true, v_existing.status;
        RETURN;
    END IF;

    INSERT INTO public.escrow_transactions
        (order_id, buyer_id, seller_id, amount, platform_fee, seller_earnings,
         payment_method, payment_reference, status,
         delivered_at, auto_release_at)
    VALUES
        (p_order_id, p_buyer_id, p_seller_id, p_amount, p_platform_fee, p_seller_earnings,
         p_payment_method, p_payment_reference,
         CASE WHEN p_instant_delivery THEN 'delivered' ELSE 'held' END,
         CASE WHEN p_instant_delivery THEN now() ELSE NULL END,
         CASE WHEN p_instant_delivery THEN now() + (p_auto_release_hours || ' hours')::interval ELSE NULL END)
    RETURNING id INTO v_id;

    INSERT INTO public.escrow_holds (escrow_transaction_id, held_amount, remaining_amount, status)
    VALUES (v_id, p_amount, p_amount, 'active');

    PERFORM public._escrow_log_event(v_id, 'held', p_buyer_id, NULL, 'held', p_amount,
        jsonb_build_object('payment_method', p_payment_method, 'reference', p_payment_reference));

    IF p_instant_delivery THEN
        PERFORM public._escrow_log_event(v_id, 'delivered', NULL, 'held', 'delivered', p_amount,
            jsonb_build_object('reason', 'instant_delivery_type'));
    END IF;

    RETURN QUERY SELECT v_id, false, (CASE WHEN p_instant_delivery THEN 'delivered' ELSE 'held' END);
END;
$$ LANGUAGE plpgsql;


-- ── 5. Seller marks delivered -> starts the auto-release clock ───
CREATE OR REPLACE FUNCTION public.escrow_mark_delivered(
    p_escrow_id UUID,
    p_seller_id UUID,
    p_auto_release_hours INTEGER DEFAULT 72
) RETURNS TABLE (success BOOLEAN, message VARCHAR) AS $$
DECLARE
    v_escrow RECORD;
BEGIN
    SELECT * INTO v_escrow FROM public.escrow_transactions
    WHERE id = p_escrow_id AND seller_id = p_seller_id FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT false, 'Escrow transaction not found for this seller'::VARCHAR;
        RETURN;
    END IF;

    IF v_escrow.status <> 'held' THEN
        -- Already delivered/released/disputed/etc: no-op, not an error,
        -- so a retried "mark delivered" request is harmless.
        RETURN QUERY SELECT true, ('No-op: already ' || v_escrow.status)::VARCHAR;
        RETURN;
    END IF;

    UPDATE public.escrow_transactions
    SET status = 'delivered',
        delivered_at = now(),
        auto_release_at = now() + (p_auto_release_hours || ' hours')::interval,
        updated_at = now()
    WHERE id = p_escrow_id;

    PERFORM public._escrow_log_event(p_escrow_id, 'delivered', p_seller_id, 'held', 'delivered',
        v_escrow.amount, '{}'::jsonb);

    RETURN QUERY SELECT true, 'Marked delivered'::VARCHAR;
END;
$$ LANGUAGE plpgsql;


-- ── 6. Release funds to the seller (buyer confirm OR auto-release) ──
-- The actual money movement: credits seller's wallet balance and
-- writes a wallet_transactions ledger row, all inside the same
-- row-locked transaction as the escrow status change, so a crash
-- mid-way can never leave the escrow "released" with no matching
-- credit (or vice versa).
CREATE OR REPLACE FUNCTION public.escrow_release(
    p_escrow_id UUID,
    p_actor_id  UUID,          -- buyer id, or NULL for system auto-release
    p_reason    VARCHAR DEFAULT 'buyer_confirmed'   -- buyer_confirmed | auto_released | dispute_release_seller
) RETURNS TABLE (
    success BOOLEAN, already_processed BOOLEAN, seller_id UUID,
    amount DECIMAL, message VARCHAR
) AS $$
DECLARE
    v_escrow RECORD;
    v_bal_before DECIMAL(15,2);
    v_bal_after  DECIMAL(15,2);
BEGIN
    SELECT * INTO v_escrow FROM public.escrow_transactions
    WHERE id = p_escrow_id FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT false, false, NULL::UUID, NULL::DECIMAL, 'Escrow transaction not found'::VARCHAR;
        RETURN;
    END IF;

    IF v_escrow.status IN ('released', 'refunded', 'partial_refunded', 'cancelled') THEN
        RETURN QUERY SELECT true, true, v_escrow.seller_id, v_escrow.seller_earnings,
            ('No-op: already ' || v_escrow.status)::VARCHAR;
        RETURN;
    END IF;

    IF v_escrow.status = 'disputed' THEN
        RETURN QUERY SELECT false, false, NULL::UUID, NULL::DECIMAL,
            'Cannot release: funds are frozen under an open dispute'::VARCHAR;
        RETURN;
    END IF;

    -- Lock the seller's row and credit the wallet.
    SELECT balance INTO v_bal_before FROM public.users WHERE id = v_escrow.seller_id FOR UPDATE;
    v_bal_after := COALESCE(v_bal_before, 0) + v_escrow.seller_earnings;

    UPDATE public.users SET balance = v_bal_after WHERE id = v_escrow.seller_id;

    INSERT INTO public.wallet_transactions
        (user_id, type, amount, balance_before, balance_after,
         reference, status, description, order_id)
    VALUES
        (v_escrow.seller_id, 'sale', v_escrow.seller_earnings, v_bal_before, v_bal_after,
         'ESCROW-RELEASE-' || v_escrow.id, 'completed',
         'Escrow release for order ' || v_escrow.order_id, v_escrow.order_id)
    ON CONFLICT (reference) DO NOTHING;

    UPDATE public.escrow_transactions
    SET status = 'released',
        released_amount = seller_earnings,
        released_at = now(),
        updated_at = now()
    WHERE id = p_escrow_id;

    UPDATE public.escrow_holds
    SET status = 'released', remaining_amount = 0, updated_at = now()
    WHERE escrow_transaction_id = p_escrow_id;

    PERFORM public._escrow_log_event(p_escrow_id, p_reason, p_actor_id, v_escrow.status, 'released',
        v_escrow.seller_earnings, '{}'::jsonb);

    RETURN QUERY SELECT true, false, v_escrow.seller_id, v_escrow.seller_earnings, 'Released'::VARCHAR;
END;
$$ LANGUAGE plpgsql;


-- ── 7. Batch auto-release (call from a cron / admin-triggered job) ──
-- Releases every escrow whose auto-release deadline has passed and
-- which is not frozen by a dispute. Loops row-by-row so one bad row
-- can't abort the whole batch, and reuses escrow_release() so the
-- money movement logic exists in exactly one place.
CREATE OR REPLACE FUNCTION public.escrow_auto_release_due()
RETURNS TABLE (escrow_id UUID, success BOOLEAN, message VARCHAR) AS $$
DECLARE
    v_row RECORD;
    v_result RECORD;
BEGIN
    FOR v_row IN
        SELECT id FROM public.escrow_transactions
        WHERE status = 'delivered' AND auto_release_at IS NOT NULL AND auto_release_at <= now()
        ORDER BY auto_release_at
        LIMIT 500
    LOOP
        SELECT * INTO v_result FROM public.escrow_release(v_row.id, NULL, 'auto_released');
        escrow_id := v_row.id;
        success   := v_result.success;
        message   := v_result.message;
        RETURN NEXT;
    END LOOP;
END;
$$ LANGUAGE plpgsql;


-- ── 8. Open a dispute (freezes the escrow) ────────────────────────
CREATE OR REPLACE FUNCTION public.escrow_open_dispute(
    p_escrow_id UUID,
    p_raised_by UUID,
    p_reason    VARCHAR,
    p_description TEXT DEFAULT NULL
) RETURNS TABLE (dispute_id UUID, success BOOLEAN, message VARCHAR) AS $$
DECLARE
    v_escrow RECORD;
    v_did UUID;
    v_against UUID;
BEGIN
    SELECT * INTO v_escrow FROM public.escrow_transactions WHERE id = p_escrow_id FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::UUID, false, 'Escrow transaction not found'::VARCHAR;
        RETURN;
    END IF;

    IF v_escrow.status IN ('released', 'refunded', 'partial_refunded', 'cancelled') THEN
        RETURN QUERY SELECT NULL::UUID, false,
            ('Cannot dispute: funds already ' || v_escrow.status)::VARCHAR;
        RETURN;
    END IF;

    IF v_escrow.status = 'disputed' THEN
        RETURN QUERY SELECT NULL::UUID, false, 'A dispute is already open for this order'::VARCHAR;
        RETURN;
    END IF;

    v_against := CASE WHEN p_raised_by = v_escrow.buyer_id THEN v_escrow.seller_id ELSE v_escrow.buyer_id END;

    INSERT INTO public.disputes
        (escrow_transaction_id, order_id, raised_by, against_id, reason, description, status)
    VALUES
        (p_escrow_id, v_escrow.order_id, p_raised_by, v_against, p_reason, p_description, 'open')
    RETURNING id INTO v_did;

    UPDATE public.escrow_transactions
    SET status = 'disputed', updated_at = now()
    WHERE id = p_escrow_id;

    PERFORM public._escrow_log_event(p_escrow_id, 'dispute_opened', p_raised_by, v_escrow.status,
        'disputed', v_escrow.amount, jsonb_build_object('dispute_id', v_did, 'reason', p_reason));

    RETURN QUERY SELECT v_did, true, 'Dispute opened; funds frozen'::VARCHAR;
END;
$$ LANGUAGE plpgsql;


-- ── 9. Admin resolves a dispute ───────────────────────────────────
-- p_resolution: 'refund_buyer' | 'release_seller' | 'partial_refund'
-- p_refund_amount is required (and validated) only for partial_refund.
CREATE OR REPLACE FUNCTION public.escrow_resolve_dispute(
    p_dispute_id    UUID,
    p_admin_id      UUID,
    p_resolution    VARCHAR,
    p_refund_amount DECIMAL(15,2) DEFAULT NULL,
    p_note          TEXT DEFAULT NULL
) RETURNS TABLE (success BOOLEAN, message VARCHAR) AS $$
DECLARE
    v_dispute RECORD;
    v_escrow  RECORD;
    v_buyer_bal_before DECIMAL(15,2);
    v_buyer_bal_after  DECIMAL(15,2);
    v_seller_bal_before DECIMAL(15,2);
    v_seller_bal_after  DECIMAL(15,2);
    v_seller_share DECIMAL(15,2);
    v_ratio DECIMAL;
BEGIN
    SELECT * INTO v_dispute FROM public.disputes WHERE id = p_dispute_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT false, 'Dispute not found'::VARCHAR;
        RETURN;
    END IF;

    IF v_dispute.status = 'resolved' THEN
        RETURN QUERY SELECT true, 'No-op: dispute already resolved'::VARCHAR;
        RETURN;
    END IF;

    SELECT * INTO v_escrow FROM public.escrow_transactions
    WHERE id = v_dispute.escrow_transaction_id FOR UPDATE;

    IF v_escrow.status <> 'disputed' THEN
        RETURN QUERY SELECT false,
            ('Escrow is not in a disputed state (currently ' || v_escrow.status || ')')::VARCHAR;
        RETURN;
    END IF;

    IF p_resolution = 'refund_buyer' THEN
        SELECT balance INTO v_buyer_bal_before FROM public.users WHERE id = v_escrow.buyer_id FOR UPDATE;
        v_buyer_bal_after := COALESCE(v_buyer_bal_before, 0) + v_escrow.amount;
        UPDATE public.users SET balance = v_buyer_bal_after WHERE id = v_escrow.buyer_id;

        INSERT INTO public.wallet_transactions
            (user_id, type, amount, balance_before, balance_after, reference, status, description, order_id)
        VALUES
            (v_escrow.buyer_id, 'refund', v_escrow.amount, v_buyer_bal_before, v_buyer_bal_after,
             'ESCROW-DISPUTE-REFUND-' || v_escrow.id, 'completed',
             'Dispute resolution: full refund for order ' || v_escrow.order_id, v_escrow.order_id)
        ON CONFLICT (reference) DO NOTHING;

        UPDATE public.escrow_transactions
        SET status = 'refunded', refunded_amount = amount, refunded_at = now(), updated_at = now()
        WHERE id = v_escrow.id;

        UPDATE public.escrow_holds SET status = 'refunded', remaining_amount = 0, updated_at = now()
        WHERE escrow_transaction_id = v_escrow.id;

    ELSIF p_resolution = 'release_seller' THEN
        SELECT balance INTO v_seller_bal_before FROM public.users WHERE id = v_escrow.seller_id FOR UPDATE;
        v_seller_bal_after := COALESCE(v_seller_bal_before, 0) + v_escrow.seller_earnings;
        UPDATE public.users SET balance = v_seller_bal_after WHERE id = v_escrow.seller_id;

        INSERT INTO public.wallet_transactions
            (user_id, type, amount, balance_before, balance_after, reference, status, description, order_id)
        VALUES
            (v_escrow.seller_id, 'sale', v_escrow.seller_earnings, v_seller_bal_before, v_seller_bal_after,
             'ESCROW-DISPUTE-RELEASE-' || v_escrow.id, 'completed',
             'Dispute resolution: released to seller for order ' || v_escrow.order_id, v_escrow.order_id)
        ON CONFLICT (reference) DO NOTHING;

        UPDATE public.escrow_transactions
        SET status = 'released', released_amount = seller_earnings, released_at = now(), updated_at = now()
        WHERE id = v_escrow.id;

        UPDATE public.escrow_holds SET status = 'released', remaining_amount = 0, updated_at = now()
        WHERE escrow_transaction_id = v_escrow.id;

    ELSIF p_resolution = 'partial_refund' THEN
        IF p_refund_amount IS NULL OR p_refund_amount <= 0 OR p_refund_amount >= v_escrow.amount THEN
            RETURN QUERY SELECT false,
                'partial_refund requires 0 < refund_amount < total held amount'::VARCHAR;
            RETURN;
        END IF;

        -- Refund the requested amount to the buyer; release the
        -- remainder to the seller, scaled down proportionally to the
        -- seller's original earnings share (so the platform fee is
        -- applied consistently rather than fully absorbed by either side).
        v_ratio := (v_escrow.amount - p_refund_amount) / v_escrow.amount;
        v_seller_share := ROUND(v_escrow.seller_earnings * v_ratio, 2);

        SELECT balance INTO v_buyer_bal_before FROM public.users WHERE id = v_escrow.buyer_id FOR UPDATE;
        v_buyer_bal_after := COALESCE(v_buyer_bal_before, 0) + p_refund_amount;
        UPDATE public.users SET balance = v_buyer_bal_after WHERE id = v_escrow.buyer_id;

        INSERT INTO public.wallet_transactions
            (user_id, type, amount, balance_before, balance_after, reference, status, description, order_id)
        VALUES
            (v_escrow.buyer_id, 'refund', p_refund_amount, v_buyer_bal_before, v_buyer_bal_after,
             'ESCROW-DISPUTE-PARTIAL-REFUND-' || v_escrow.id, 'completed',
             'Dispute resolution: partial refund for order ' || v_escrow.order_id, v_escrow.order_id)
        ON CONFLICT (reference) DO NOTHING;

        SELECT balance INTO v_seller_bal_before FROM public.users WHERE id = v_escrow.seller_id FOR UPDATE;
        v_seller_bal_after := COALESCE(v_seller_bal_before, 0) + v_seller_share;
        UPDATE public.users SET balance = v_seller_bal_after WHERE id = v_escrow.seller_id;

        INSERT INTO public.wallet_transactions
            (user_id, type, amount, balance_before, balance_after, reference, status, description, order_id)
        VALUES
            (v_escrow.seller_id, 'sale', v_seller_share, v_seller_bal_before, v_seller_bal_after,
             'ESCROW-DISPUTE-PARTIAL-RELEASE-' || v_escrow.id, 'completed',
             'Dispute resolution: partial release to seller for order ' || v_escrow.order_id, v_escrow.order_id)
        ON CONFLICT (reference) DO NOTHING;

        UPDATE public.escrow_transactions
        SET status = 'partial_refunded',
            refunded_amount = p_refund_amount,
            released_amount = v_seller_share,
            refunded_at = now(), released_at = now(), updated_at = now()
        WHERE id = v_escrow.id;

        UPDATE public.escrow_holds
        SET status = 'partially_released', remaining_amount = 0, updated_at = now()
        WHERE escrow_transaction_id = v_escrow.id;
    ELSE
        RETURN QUERY SELECT false, ('Unknown resolution type: ' || p_resolution)::VARCHAR;
        RETURN;
    END IF;

    UPDATE public.disputes
    SET status = 'resolved', resolution = p_resolution, resolution_amount = p_refund_amount,
        resolution_note = p_note, resolved_by = p_admin_id, resolved_at = now(), updated_at = now()
    WHERE id = p_dispute_id;

    PERFORM public._escrow_log_event(v_escrow.id, 'dispute_resolved', p_admin_id, 'disputed',
        (SELECT status FROM public.escrow_transactions WHERE id = v_escrow.id),
        COALESCE(p_refund_amount, v_escrow.amount),
        jsonb_build_object('dispute_id', p_dispute_id, 'resolution', p_resolution, 'note', p_note));

    RETURN QUERY SELECT true, ('Dispute resolved: ' || p_resolution)::VARCHAR;
END;
$$ LANGUAGE plpgsql;


-- ── 10. Seller payout approve / reject (mirrors wallet_tx_approve_atomic) ──
CREATE OR REPLACE FUNCTION public.payout_request_approve_atomic(
    p_payout_id UUID,
    p_admin_id  UUID,
    p_gateway_reference VARCHAR DEFAULT NULL
) RETURNS TABLE (success BOOLEAN, message VARCHAR) AS $$
DECLARE
    v_payout RECORD;
    v_bal_before DECIMAL(15,2);
    v_bal_after  DECIMAL(15,2);
BEGIN
    SELECT * INTO v_payout FROM public.payout_requests WHERE id = p_payout_id FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT false, 'Payout request not found'::VARCHAR;
        RETURN;
    END IF;

    IF v_payout.status <> 'pending' THEN
        RETURN QUERY SELECT true, ('No-op: already ' || v_payout.status)::VARCHAR;
        RETURN;
    END IF;

    SELECT balance INTO v_bal_before FROM public.users WHERE id = v_payout.seller_id FOR UPDATE;

    IF COALESCE(v_bal_before, 0) < v_payout.amount THEN
        UPDATE public.payout_requests
        SET status = 'rejected', admin_id = p_admin_id, notes = COALESCE(notes, '') || ' [auto-rejected: insufficient balance at approval time]',
            processed_at = now(), updated_at = now()
        WHERE id = p_payout_id;
        RETURN QUERY SELECT false, 'Insufficient balance at approval time; request rejected'::VARCHAR;
        RETURN;
    END IF;

    v_bal_after := v_bal_before - v_payout.amount;
    UPDATE public.users SET balance = v_bal_after WHERE id = v_payout.seller_id;

    INSERT INTO public.wallet_transactions
        (user_id, type, amount, balance_before, balance_after, reference, status, description)
    VALUES
        (v_payout.seller_id, 'withdrawal', v_payout.amount, v_bal_before, v_bal_after,
         v_payout.reference, 'completed', 'Seller payout — ' || v_payout.method)
    ON CONFLICT (reference) DO NOTHING;

    UPDATE public.payout_requests
    SET status = 'paid', admin_id = p_admin_id, processed_at = now(), updated_at = now()
    WHERE id = p_payout_id;

    INSERT INTO public.payout_history
        (payout_request_id, seller_id, amount, method, status, gateway_reference, processed_at)
    VALUES
        (p_payout_id, v_payout.seller_id, v_payout.amount, v_payout.method, 'paid',
         p_gateway_reference, now());

    RETURN QUERY SELECT true, 'Payout approved and paid'::VARCHAR;
END;
$$ LANGUAGE plpgsql;


CREATE OR REPLACE FUNCTION public.payout_request_reject_atomic(
    p_payout_id UUID,
    p_admin_id  UUID,
    p_note      VARCHAR DEFAULT ''
) RETURNS TABLE (success BOOLEAN, message VARCHAR) AS $$
DECLARE
    v_payout RECORD;
BEGIN
    SELECT * INTO v_payout FROM public.payout_requests WHERE id = p_payout_id FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT false, 'Payout request not found'::VARCHAR;
        RETURN;
    END IF;

    IF v_payout.status <> 'pending' THEN
        RETURN QUERY SELECT true, ('No-op: already ' || v_payout.status)::VARCHAR;
        RETURN;
    END IF;

    UPDATE public.payout_requests
    SET status = 'rejected', admin_id = p_admin_id, notes = p_note, processed_at = now(), updated_at = now()
    WHERE id = p_payout_id;

    INSERT INTO public.payout_history
        (payout_request_id, seller_id, amount, method, status, failure_reason, processed_at)
    VALUES
        (p_payout_id, v_payout.seller_id, v_payout.amount, v_payout.method, 'failed', p_note, now());

    RETURN QUERY SELECT true, 'Payout rejected'::VARCHAR;
END;
$$ LANGUAGE plpgsql;
