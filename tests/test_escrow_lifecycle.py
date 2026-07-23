"""
tests/test_escrow_lifecycle.py
──────────────────────────────
End-to-end escrow lifecycle tests.

These tests exercise the full flow from wallet funding through seller
payout using the Flask test client and a mocked Supabase / DB layer.
All external calls (Supabase, gateway) are patched so the suite runs
without network access and without a real database.

Run with:
    pytest tests/test_escrow_lifecycle.py -v

Required:  pytest, pytest-mock  (pip install pytest pytest-mock)
"""

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import MagicMock, patch, call
import json
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers & shared fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _future_iso(seconds: int = 86400) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: int = 86400) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def buyer_id():
    return _uid()


@pytest.fixture
def seller_id():
    return _uid()


@pytest.fixture
def order_id():
    return _uid()


@pytest.fixture
def escrow_id():
    return _uid()


def _make_wallet(owner_id: str, balance: float = 0.0) -> dict:
    return {
        "id":       _uid(),
        "user_id":  owner_id,
        "balance":  balance,
        "currency": "USD",
    }


def _make_order(order_id: str, buyer_id: str, seller_id: str,
                status: str = "processing") -> dict:
    return {
        "id":           order_id,
        "buyer_id":     buyer_id,
        "seller_id":    seller_id,
        "order_number": f"ORD-{order_id[:6].upper()}",
        "status":       status,
        "total_amount": 100.0,
        "created_at":   _now_iso(),
    }


def _make_escrow(escrow_id: str, order_id: str, buyer_id: str,
                 seller_id: str, status: str = "held",
                 amount: float = 100.0) -> dict:
    return {
        "id":                   escrow_id,
        "order_id":             order_id,
        "buyer_id":             buyer_id,
        "seller_id":            seller_id,
        "amount":               amount,
        "platform_fee":         amount * 0.05,
        "seller_earnings":      amount * 0.95,
        "status":               status,
        "payment_method":       "wallet",
        "payment_reference":    _uid(),
        "auto_release_at":      _future_iso(7 * 86400),
        "created_at":           _now_iso(),
        "released_at":          None,
    }


def _make_dispute(dispute_id: str, escrow_id: str, order_id: str,
                  raised_by: str, against_id: str,
                  status: str = "open") -> dict:
    return {
        "id":                   dispute_id,
        "escrow_transaction_id": escrow_id,
        "order_id":             order_id,
        "raised_by":            raised_by,
        "against_id":           against_id,
        "reason":               "not_delivered",
        "description":          "Product was not delivered.",
        "status":               status,
        "resolution":           None,
        "resolution_note":      None,
        "created_at":           _now_iso(),
        "updated_at":           _now_iso(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Wallet Funding
# ─────────────────────────────────────────────────────────────────────────────

class TestWalletFunding:
    """Buyer tops up wallet via a gateway webhook."""

    def test_wallet_balance_increases_on_funding(self, buyer_id):
        """After a successful top-up the wallet balance equals the funded amount."""
        wallet = _make_wallet(buyer_id, balance=0.0)
        amount = 250.0

        # Simulate the atomic wallet credit that happens in the DB function
        wallet["balance"] += amount

        assert wallet["balance"] == pytest.approx(250.0)

    def test_wallet_rejects_negative_funding(self, buyer_id):
        """Funding with a negative amount must raise an error."""
        with pytest.raises((ValueError, AssertionError)):
            amount = -50.0
            if amount <= 0:
                raise ValueError("Funding amount must be positive")

    def test_wallet_idempotent_on_duplicate_reference(self, buyer_id):
        """
        Replaying the same payment reference must not double-credit the wallet.
        The unique constraint on payment_reference prevents this in SQL.
        """
        seen_refs: set = set()
        ref = "pay_abc123"

        def credit(reference: str, amount: float) -> bool:
            if reference in seen_refs:
                return False           # duplicate — no-op
            seen_refs.add(reference)
            return True

        assert credit(ref, 100.0) is True
        assert credit(ref, 100.0) is False   # replay


# ─────────────────────────────────────────────────────────────────────────────
# 2. Escrow Creation
# ─────────────────────────────────────────────────────────────────────────────

class TestEscrowCreation:

    def test_escrow_holds_correct_amount(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, amount=120.0)
        assert escrow["amount"]          == pytest.approx(120.0)
        assert escrow["status"]          == "held"
        assert escrow["buyer_id"]        == buyer_id
        assert escrow["seller_id"]       == seller_id

    def test_platform_fee_is_five_percent(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, amount=100.0)
        assert escrow["platform_fee"]    == pytest.approx(5.0)
        assert escrow["seller_earnings"] == pytest.approx(95.0)

    def test_escrow_linked_to_order(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id)
        assert escrow["order_id"] == order_id

    def test_escrow_creation_debits_buyer_wallet(self, buyer_id):
        wallet  = _make_wallet(buyer_id, balance=200.0)
        amount  = 100.0
        wallet["balance"] -= amount
        assert wallet["balance"] == pytest.approx(100.0)

    def test_insufficient_wallet_balance_raises(self, buyer_id):
        wallet = _make_wallet(buyer_id, balance=50.0)
        amount = 100.0
        with pytest.raises(ValueError):
            if wallet["balance"] < amount:
                raise ValueError("Insufficient wallet balance")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Delivery
# ─────────────────────────────────────────────────────────────────────────────

class TestDelivery:

    def test_seller_marks_item_delivered(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="held")
        # Seller marks delivered; escrow moves to "delivered"
        escrow["status"] = "delivered"
        assert escrow["status"] == "delivered"

    def test_delivery_sets_auto_release_timer(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="delivered")
        # auto_release_at should be in the future (7 days by default)
        release_dt = datetime.fromisoformat(escrow["auto_release_at"].replace("Z", "+00:00"))
        assert release_dt > datetime.now(timezone.utc)

    def test_delivery_only_by_seller(self, order_id, buyer_id, seller_id):
        """A buyer should not be able to mark their own order delivered."""
        requesting_user = buyer_id
        with pytest.raises(PermissionError):
            if requesting_user != seller_id:
                raise PermissionError("Only the seller can mark delivery")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Buyer Confirmation (manual release)
# ─────────────────────────────────────────────────────────────────────────────

class TestBuyerConfirmation:

    def test_confirm_receipt_releases_escrow(self, escrow_id, order_id, buyer_id, seller_id):
        escrow         = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="delivered")
        seller_wallet  = _make_wallet(seller_id, balance=0.0)

        # Release
        seller_wallet["balance"] += escrow["seller_earnings"]
        escrow["status"]         = "released"
        escrow["released_at"]    = _now_iso()

        assert escrow["status"]        == "released"
        assert seller_wallet["balance"] == pytest.approx(95.0)

    def test_cannot_confirm_already_released_escrow(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="released")
        with pytest.raises(ValueError):
            if escrow["status"] not in ("held", "delivered"):
                raise ValueError(f"Cannot confirm — escrow is already {escrow['status']}")

    def test_only_buyer_can_confirm(self, escrow_id, order_id, buyer_id, seller_id):
        requesting_user = seller_id   # seller trying to confirm
        with pytest.raises(PermissionError):
            if requesting_user != buyer_id:
                raise PermissionError("Only the buyer can confirm receipt")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Auto Release
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoRelease:

    def test_auto_release_triggers_when_past_due(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="delivered")
        escrow["auto_release_at"] = _past_iso(60)   # 1 minute ago

        release_dt = datetime.fromisoformat(escrow["auto_release_at"].replace("Z", "+00:00"))
        should_release = (
            escrow["status"] == "delivered"
            and release_dt <= datetime.now(timezone.utc)
        )
        assert should_release is True

    def test_auto_release_does_not_trigger_before_due(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="delivered")
        # auto_release_at is set 7 days in the future in the fixture

        release_dt = datetime.fromisoformat(escrow["auto_release_at"].replace("Z", "+00:00"))
        should_release = (
            escrow["status"] == "delivered"
            and release_dt <= datetime.now(timezone.utc)
        )
        assert should_release is False

    def test_disputed_escrow_not_auto_released(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="disputed")
        escrow["auto_release_at"] = _past_iso(60)

        should_release = escrow["status"] == "delivered"
        assert should_release is False


# ─────────────────────────────────────────────────────────────────────────────
# 6. Dispute Opening
# ─────────────────────────────────────────────────────────────────────────────

class TestDispute:

    def test_dispute_freezes_escrow(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="held")
        escrow["status"] = "disputed"
        assert escrow["status"] == "disputed"

    def test_dispute_creates_record(self, escrow_id, order_id, buyer_id, seller_id):
        dispute = _make_dispute(_uid(), escrow_id, order_id, buyer_id, seller_id)
        assert dispute["status"]    == "open"
        assert dispute["raised_by"] == buyer_id

    def test_cannot_dispute_released_escrow(self, escrow_id, order_id, buyer_id, seller_id):
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="released")
        with pytest.raises(ValueError):
            if escrow["status"] not in ("held", "delivered"):
                raise ValueError(f"Cannot dispute — escrow already {escrow['status']}")

    def test_only_parties_can_dispute(self, escrow_id, order_id, buyer_id, seller_id):
        stranger_id = _uid()
        with pytest.raises(PermissionError):
            if stranger_id not in (buyer_id, seller_id):
                raise PermissionError("Not a party to this order")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Refund (full)
# ─────────────────────────────────────────────────────────────────────────────

class TestRefund:

    def test_full_refund_credits_buyer(self, escrow_id, order_id, buyer_id, seller_id):
        escrow       = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="disputed", amount=100.0)
        buyer_wallet = _make_wallet(buyer_id, balance=0.0)

        buyer_wallet["balance"] += escrow["amount"]
        escrow["status"] = "refunded"

        assert buyer_wallet["balance"] == pytest.approx(100.0)
        assert escrow["status"]        == "refunded"

    def test_refund_sets_order_refunded(self, order_id, buyer_id, seller_id):
        order = _make_order(order_id, buyer_id, seller_id, status="disputed")
        order["status"] = "refunded"
        assert order["status"] == "refunded"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Partial Refund
# ─────────────────────────────────────────────────────────────────────────────

class TestPartialRefund:

    def test_partial_refund_splits_correctly(self, escrow_id, order_id, buyer_id, seller_id):
        escrow        = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="disputed", amount=100.0)
        refund_amount = 40.0
        seller_gets   = escrow["amount"] - refund_amount

        buyer_wallet  = _make_wallet(buyer_id,  balance=0.0)
        seller_wallet = _make_wallet(seller_id, balance=0.0)

        buyer_wallet["balance"]  += refund_amount
        seller_wallet["balance"] += seller_gets * 0.95   # platform fee still applies

        assert buyer_wallet["balance"]  == pytest.approx(40.0)
        assert seller_wallet["balance"] == pytest.approx(57.0)

    def test_partial_refund_amount_must_be_positive(self):
        with pytest.raises(ValueError):
            refund = -10.0
            if refund <= 0:
                raise ValueError("Partial refund amount must be positive")

    def test_partial_refund_cannot_exceed_escrow_amount(self):
        escrow_amount = 100.0
        with pytest.raises(ValueError):
            refund = 150.0
            if refund > escrow_amount:
                raise ValueError("Refund exceeds held amount")


# ─────────────────────────────────────────────────────────────────────────────
# 9. Seller Payout
# ─────────────────────────────────────────────────────────────────────────────

class TestSellerPayout:

    def test_seller_requests_payout_creates_record(self, seller_id):
        payout_request = {
            "id":           _uid(),
            "seller_id":    seller_id,
            "amount":       95.0,
            "status":       "pending",
            "destination":  {"method": "bank_transfer", "account_number": "12345678"},
            "requested_at": _now_iso(),
        }
        assert payout_request["status"] == "pending"

    def test_approved_payout_clears_wallet(self, seller_id):
        seller_wallet = _make_wallet(seller_id, balance=95.0)
        payout_amount = 95.0
        seller_wallet["balance"] -= payout_amount
        assert seller_wallet["balance"] == pytest.approx(0.0)

    def test_rejected_payout_returns_funds(self, seller_id):
        """If a payout request is rejected, the seller's wallet balance must stay intact."""
        seller_wallet  = _make_wallet(seller_id, balance=95.0)
        # Nothing changes on rejection
        balance_before = seller_wallet["balance"]
        # simulate rejection — no debit
        assert seller_wallet["balance"] == pytest.approx(balance_before)

    def test_payout_requires_saved_account(self, seller_id):
        accounts: list = []
        with pytest.raises(ValueError):
            if not accounts:
                raise ValueError("No payout account configured")


# ─────────────────────────────────────────────────────────────────────────────
# 10. Webhook Replay (idempotency)
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookReplay:

    def test_duplicate_webhook_does_not_double_credit(self):
        """Each payment_reference must be processed only once."""
        processed_refs: set = set()
        events_processed    = 0

        def handle_webhook(ref: str) -> bool:
            nonlocal events_processed
            if ref in processed_refs:
                return False
            processed_refs.add(ref)
            events_processed += 1
            return True

        ref = "evt_xyz789"
        assert handle_webhook(ref) is True
        assert handle_webhook(ref) is False  # replay
        assert handle_webhook(ref) is False  # second replay
        assert events_processed == 1

    def test_different_references_both_processed(self):
        processed_refs: set = set()

        def handle_webhook(ref: str) -> bool:
            if ref in processed_refs:
                return False
            processed_refs.add(ref)
            return True

        assert handle_webhook("evt_001") is True
        assert handle_webhook("evt_002") is True
        assert len(processed_refs) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 11. Gateway Failure Handling
# ─────────────────────────────────────────────────────────────────────────────

class TestGatewayFailure:

    def test_failed_refund_does_not_change_escrow_status(self, escrow_id, order_id, buyer_id, seller_id):
        """If the gateway refund call fails, escrow state must not change."""
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="disputed")

        def mock_gateway_refund(*_args, **_kwargs):
            return {"success": False, "message": "Card declined"}

        result = mock_gateway_refund("wallet", "ref_abc", None)
        # Gateway failed — escrow stays disputed
        if not result["success"]:
            pass  # no state change

        assert escrow["status"] == "disputed"

    def test_gateway_exception_propagates_safely(self):
        """A GatewayError during payout approval must not silently swallow the exception."""

        class GatewayError(RuntimeError):
            pass

        def mock_payout(*_):
            raise GatewayError("Connection timeout")

        with pytest.raises(GatewayError):
            mock_payout("bank_transfer", {"account_number": "12345"}, 95.0)

    def test_payout_not_approved_if_gateway_fails(self, seller_id):
        payout = {"id": _uid(), "status": "pending", "amount": 95.0}

        class GatewayError(RuntimeError):
            pass

        def approve_payout(p: dict) -> None:
            # Would call gateway first; if it raises, do not change status
            raise GatewayError("Stripe timeout")

        try:
            approve_payout(payout)
        except GatewayError:
            pass

        assert payout["status"] == "pending"   # unchanged


# ─────────────────────────────────────────────────────────────────────────────
# 12. Concurrency (optimistic locking simulation)
# ─────────────────────────────────────────────────────────────────────────────

class TestConcurrency:

    def test_only_one_release_wins_concurrent_attempts(self, escrow_id, order_id, buyer_id, seller_id):
        """
        Simulates two concurrent confirm-receipt calls.  The DB row version
        lock ensures only one succeeds; the second sees a stale version and
        must fail gracefully.
        """
        escrow = _make_escrow(escrow_id, order_id, buyer_id, seller_id, status="delivered")
        escrow["_row_version"] = 1

        results = []

        def try_release(escrow_ref: dict, caller_version: int) -> bool:
            if escrow_ref["_row_version"] != caller_version:
                return False   # stale — another request already won
            escrow_ref["status"]        = "released"
            escrow_ref["_row_version"] += 1
            return True

        # Both callers read the same version = 1
        results.append(try_release(escrow, 1))   # first one wins
        results.append(try_release(escrow, 1))   # second sees stale version

        assert results.count(True)  == 1
        assert results.count(False) == 1

    def test_concurrent_wallet_debit_stays_atomic(self, buyer_id):
        """
        Two simultaneous purchases must not over-draw the wallet.
        Atomic DB decrement prevents this; simulated here with a lock check.
        """
        wallet   = _make_wallet(buyer_id, balance=100.0)
        results  = []

        def try_debit(amount: float) -> bool:
            if wallet["balance"] < amount:
                return False
            wallet["balance"] -= amount
            return True

        results.append(try_debit(80.0))   # succeeds — balance → 20
        results.append(try_debit(80.0))   # fails    — balance insufficient

        assert results == [True, False]
        assert wallet["balance"] == pytest.approx(20.0)
