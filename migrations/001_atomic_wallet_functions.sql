-- ════════════════════════════════════════════════════════════════
-- Migration 001: Atomic wallet operations + webhook idempotency
--
-- Purpose: Fix critical race-condition / double-spend issues found
-- in the audit by moving all balance read-modify-write sequences
-- into single-statement, row-locked Postgres functions (RPCs).
--
-- This migration is purely ADDITIVE:
--   - No existing table is altered or dropped.
--   - No existing column is removed or renamed.
--   - No business rule (commission calc, discount calc, etc.) is
--     changed — these functions only replace the *mechanism* by
--     which balance changes are applied, not the amounts.
--
-- Apply with: psql <connection> -f migrations/001_atomic_wallet_functions.sql
-- ════════════════════════════════════════════════════════════════

-- ── 1. Idempotent wallet credit (deposits from payment webhooks) ──
--
-- Locks the user row (FOR UPDATE) and performs balance read + check +
-- write + ledger insert as a single atomic transaction. If a
-- wallet_transactions row with the same `reference` already exists,
-- this is a no-op (returns the existing row) instead of crediting
-- twice or raising an unhandled unique-constraint error. This is
-- what makes webhook retries safe.
CREATE OR REPLACE FUNCTION public.wallet_credit_idempotent(
    p_user_id        UUID,
    p_amount         DECIMAL(15,2),
    p_reference      VARCHAR(100),
    p_payment_method VARCHAR(50),
    p_description     TEXT,
    p_type           VARCHAR(30) DEFAULT 'deposit'
)
RETURNS TABLE (
    tx_id            UUID,
    already_processed BOOLEAN,
    balance_before    DECIMAL(15,2),
    balance_after     DECIMAL(15,2)
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_existing_id  UUID;
    v_bal_before   DECIMAL(15,2);
    v_bal_after    DECIMAL(15,2);
    v_new_tx_id    UUID;
BEGIN
    -- Idempotency guard: if we've already recorded this reference,
    -- return the existing transaction and do nothing else. This
    -- makes the function safe to call multiple times for the same
    -- webhook event (provider retries, duplicate deliveries, etc).
    IF p_reference IS NOT NULL THEN
        SELECT id INTO v_existing_id
        FROM public.wallet_transactions
        WHERE reference = p_reference
        LIMIT 1;

        IF v_existing_id IS NOT NULL THEN
            SELECT wt.balance_before, wt.balance_after
            INTO v_bal_before, v_bal_after
            FROM public.wallet_transactions wt
            WHERE wt.id = v_existing_id;

            RETURN QUERY SELECT v_existing_id, TRUE, v_bal_before, v_bal_after;
            RETURN;
        END IF;
    END IF;

    -- Lock the user row so no concurrent credit/debit can interleave
    -- with this one until we commit.
    SELECT balance INTO v_bal_before
    FROM public.users
    WHERE id = p_user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'User % not found', p_user_id;
    END IF;

    v_bal_after := v_bal_before + p_amount;

    UPDATE public.users
    SET balance = v_bal_after
    WHERE id = p_user_id;

    INSERT INTO public.wallet_transactions (
        user_id, type, amount, balance_before, balance_after,
        reference, status, payment_method, description
    ) VALUES (
        p_user_id, p_type, p_amount, v_bal_before, v_bal_after,
        p_reference, 'completed', p_payment_method, p_description
    )
    RETURNING id INTO v_new_tx_id;

    RETURN QUERY SELECT v_new_tx_id, FALSE, v_bal_before, v_bal_after;
END;
$$;


-- ── 2. Atomic wallet debit with balance check (purchases) ─────────
--
-- Used by checkout for wallet-based payments. Locks the row, checks
-- sufficient balance, deducts, and writes the ledger entry all in
-- one transaction. Returns success=false (rather than raising) when
-- balance is insufficient, so calling code can flash a friendly
-- message exactly as before.
CREATE OR REPLACE FUNCTION public.wallet_debit_atomic(
    p_user_id     UUID,
    p_amount      DECIMAL(15,2),
    p_reference   VARCHAR(100),
    p_description TEXT,
    p_order_id    UUID DEFAULT NULL,
    p_type        VARCHAR(30) DEFAULT 'purchase'
)
RETURNS TABLE (
    tx_id           UUID,
    success         BOOLEAN,
    balance_before  DECIMAL(15,2),
    balance_after   DECIMAL(15,2)
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_bal_before DECIMAL(15,2);
    v_bal_after  DECIMAL(15,2);
    v_new_tx_id  UUID;
    v_existing_id UUID;
BEGIN
    IF p_reference IS NOT NULL THEN
        SELECT id INTO v_existing_id
        FROM public.wallet_transactions
        WHERE reference = p_reference
        LIMIT 1;

        IF v_existing_id IS NOT NULL THEN
            SELECT wt.balance_before, wt.balance_after
            INTO v_bal_before, v_bal_after
            FROM public.wallet_transactions wt
            WHERE wt.id = v_existing_id;

            RETURN QUERY SELECT v_existing_id, TRUE, v_bal_before, v_bal_after;
            RETURN;
        END IF;
    END IF;

    SELECT balance INTO v_bal_before
    FROM public.users
    WHERE id = p_user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'User % not found', p_user_id;
    END IF;

    IF v_bal_before < p_amount THEN
        RETURN QUERY SELECT NULL::UUID, FALSE, v_bal_before, v_bal_before;
        RETURN;
    END IF;

    v_bal_after := v_bal_before - p_amount;

    UPDATE public.users
    SET balance = v_bal_after
    WHERE id = p_user_id;

    INSERT INTO public.wallet_transactions (
        user_id, type, amount, balance_before, balance_after,
        reference, status, description, order_id
    ) VALUES (
        p_user_id, p_type, p_amount, v_bal_before, v_bal_after,
        p_reference, 'completed', p_description, p_order_id
    )
    RETURNING id INTO v_new_tx_id;

    RETURN QUERY SELECT v_new_tx_id, TRUE, v_bal_before, v_bal_after;
END;
$$;


-- ── 3. Atomic pending-transaction approval (admin deposit/withdrawal) ──
--
-- Replaces the read -> check -> update("users") -> update("wallet_
-- transactions") sequence in admin.py with a single locked
-- transaction. Locks BOTH the wallet_transactions row (so two admins
-- clicking "approve" at the same time can't both process it) and the
-- user row (so balance math can't race with checkout/webhooks).
CREATE OR REPLACE FUNCTION public.wallet_tx_approve_atomic(
    p_tx_id     UUID,
    p_admin_id  UUID
)
RETURNS TABLE (
    success       BOOLEAN,
    message       TEXT,
    tx_type       VARCHAR(30),
    user_id       UUID,
    amount        DECIMAL(15,2)
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_tx          RECORD;
    v_bal_before  DECIMAL(15,2);
    v_bal_after   DECIMAL(15,2);
BEGIN
    -- Lock the transaction row first. If another request (a second
    -- admin, or a bulk-approve loop hitting the same id) already has
    -- this row locked, this blocks until it's released, then re-reads
    -- the now-updated status below -- preventing double-approval.
    SELECT * INTO v_tx
    FROM public.wallet_transactions
    WHERE id = p_tx_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT FALSE, 'Transaction not found or already processed.'::TEXT,
                            NULL::VARCHAR(30), NULL::UUID, NULL::DECIMAL(15,2);
        RETURN;
    END IF;

    IF v_tx.status <> 'pending' THEN
        RETURN QUERY SELECT FALSE, 'Transaction not found or already processed.'::TEXT,
                            v_tx.type, v_tx.user_id, v_tx.amount;
        RETURN;
    END IF;

    -- Lock the user row so the balance check + update can't race
    -- with a concurrent checkout, webhook credit, or another
    -- approval for the same user.
    SELECT balance INTO v_bal_before
    FROM public.users
    WHERE id = v_tx.user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT FALSE, 'User not found.'::TEXT,
                            v_tx.type, v_tx.user_id, v_tx.amount;
        RETURN;
    END IF;

    IF v_tx.type = 'deposit' THEN
        v_bal_after := v_bal_before + v_tx.amount;

    ELSIF v_tx.type = 'withdrawal' THEN
        IF v_tx.amount > v_bal_before THEN
            RETURN QUERY SELECT FALSE, 'Insufficient user balance to approve withdrawal.'::TEXT,
                                v_tx.type, v_tx.user_id, v_tx.amount;
            RETURN;
        END IF;
        v_bal_after := v_bal_before - v_tx.amount;

    ELSE
        RETURN QUERY SELECT FALSE, ('Unsupported transaction type: ' || v_tx.type)::TEXT,
                            v_tx.type, v_tx.user_id, v_tx.amount;
        RETURN;
    END IF;

    UPDATE public.users
    SET balance = v_bal_after
    WHERE id = v_tx.user_id;

    UPDATE public.wallet_transactions
    SET status = 'completed',
        balance_before = v_bal_before,
        balance_after  = v_bal_after,
        processed_by   = p_admin_id
    WHERE id = p_tx_id;

    RETURN QUERY SELECT TRUE, 'Transaction approved.'::TEXT,
                        v_tx.type, v_tx.user_id, v_tx.amount;
END;
$$;


-- ── 4. Atomic pending-transaction rejection ────────────────────────
--
-- Locks the row so rejection can't race with an in-flight approval
-- of the same transaction (whichever gets the lock first "wins";
-- the second sees status <> 'pending' and reports accordingly).
CREATE OR REPLACE FUNCTION public.wallet_tx_reject_atomic(
    p_tx_id    UUID,
    p_admin_id UUID,
    p_note     TEXT
)
RETURNS TABLE (
    success BOOLEAN,
    message TEXT,
    tx_type VARCHAR(30),
    user_id UUID,
    amount  DECIMAL(15,2)
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_tx RECORD;
BEGIN
    SELECT * INTO v_tx
    FROM public.wallet_transactions
    WHERE id = p_tx_id
    FOR UPDATE;

    IF NOT FOUND OR v_tx.status <> 'pending' THEN
        RETURN QUERY SELECT FALSE, 'Transaction not found or already processed.'::TEXT,
                            NULL::VARCHAR(30), NULL::UUID, NULL::DECIMAL(15,2);
        RETURN;
    END IF;

    UPDATE public.wallet_transactions
    SET status = 'cancelled',
        admin_note = p_note,
        processed_by = p_admin_id
    WHERE id = p_tx_id;

    RETURN QUERY SELECT TRUE, 'Transaction rejected.'::TEXT,
                        v_tx.type, v_tx.user_id, v_tx.amount;
END;
$$;


-- ── 5. Atomic, race-free counters (views / downloads) ──────────────
-- Minor related fix: these were also non-atomic read-modify-writes
-- in the audited code (listing views, order_item download_count).
-- Included here since they touch the same "atomic mutation" concern
-- and are trivial to fix alongside the wallet functions.

CREATE OR REPLACE FUNCTION public.increment_listing_views(p_listing_id UUID)
RETURNS VOID
LANGUAGE sql
AS $$
    UPDATE public.listings SET views = COALESCE(views, 0) + 1 WHERE id = p_listing_id;
$$;

-- Increments download_count only if under the limit; returns the
-- resulting row (or NULL if the limit was already reached), so the
-- caller can distinguish "incremented" from "blocked" atomically
-- instead of the previous check-then-write race.
CREATE OR REPLACE FUNCTION public.increment_download_count_atomic(p_order_item_id UUID)
RETURNS TABLE (
    allowed        BOOLEAN,
    new_count      INTEGER,
    max_downloads  INTEGER
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_row RECORD;
BEGIN
    SELECT * INTO v_row
    FROM public.order_items
    WHERE id = p_order_item_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT FALSE, 0, 0;
        RETURN;
    END IF;

    IF COALESCE(v_row.download_count, 0) >= COALESCE(v_row.max_downloads, 5) THEN
        RETURN QUERY SELECT FALSE, v_row.download_count, v_row.max_downloads;
        RETURN;
    END IF;

    UPDATE public.order_items
    SET download_count = COALESCE(download_count, 0) + 1
    WHERE id = p_order_item_id;

    RETURN QUERY SELECT TRUE, COALESCE(v_row.download_count, 0) + 1, v_row.max_downloads;
END;
$$;


-- ── 6. Multi-seller checkout: one atomic function for the whole
--        wallet-payment order-creation flow ────────────────────────
--
-- The original checkout() built one or more `orders` rows, their
-- `order_items`, notifications, coupon usage, AND the buyer's wallet
-- debit, all as separate REST calls with no shared transaction. If
-- the process crashed/errored partway (e.g. after creating orders
-- but before debiting the wallet, or vice versa), the buyer could
-- end up with free orders or a debited wallet with no orders.
--
-- This function performs the financial-integrity-critical part —
-- the atomic debit + ledger entry, guarded by the same idempotency
-- reference check — as a single DB transaction. Order/order_item
-- creation remains in application code (unchanged business logic),
-- but is now only executed after this function confirms the debit
-- succeeded, and the reference makes retries of the whole checkout
-- safe to re-attempt without double-charging.
CREATE OR REPLACE FUNCTION public.checkout_wallet_debit_atomic(
    p_user_id     UUID,
    p_amount      DECIMAL(15,2),
    p_reference   VARCHAR(100),
    p_description TEXT
)
RETURNS TABLE (
    tx_id          UUID,
    success        BOOLEAN,
    already_done   BOOLEAN,
    balance_before DECIMAL(15,2),
    balance_after  DECIMAL(15,2)
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_bal_before  DECIMAL(15,2);
    v_bal_after   DECIMAL(15,2);
    v_new_tx_id   UUID;
    v_existing    RECORD;
BEGIN
    IF p_reference IS NOT NULL THEN
        SELECT id, balance_before, balance_after INTO v_existing
        FROM public.wallet_transactions
        WHERE reference = p_reference
        LIMIT 1;

        IF FOUND THEN
            RETURN QUERY SELECT v_existing.id, TRUE, TRUE,
                                v_existing.balance_before, v_existing.balance_after;
            RETURN;
        END IF;
    END IF;

    SELECT balance INTO v_bal_before
    FROM public.users
    WHERE id = p_user_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'User % not found', p_user_id;
    END IF;

    IF v_bal_before < p_amount THEN
        RETURN QUERY SELECT NULL::UUID, FALSE, FALSE, v_bal_before, v_bal_before;
        RETURN;
    END IF;

    v_bal_after := v_bal_before - p_amount;

    UPDATE public.users
    SET balance = v_bal_after
    WHERE id = p_user_id;

    INSERT INTO public.wallet_transactions (
        user_id, type, amount, balance_before, balance_after,
        reference, status, description
    ) VALUES (
        p_user_id, 'purchase', p_amount, v_bal_before, v_bal_after,
        p_reference, 'completed', p_description
    )
    RETURNING id INTO v_new_tx_id;

    RETURN QUERY SELECT v_new_tx_id, TRUE, FALSE, v_bal_before, v_bal_after;
END;
$$;
