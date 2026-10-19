"""
Paystack checkout blueprint for MT5 Signal Bot 01.

IMPORTANT INTEGRATION NOTES
- This is an add-on blueprint, not a drop-in replacement for app.py.
- Register this blueprint from your existing Flask app after reviewing the TODOs.
- It intentionally does NOT grant Telegram access by itself. Connect the successful
  verification path to your existing subscription database/Telegram functions.
- Never trust a browser redirect as proof of payment.
"""
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
from flask import Blueprint, abort, current_app, jsonify, redirect, request, url_for

paystack_bp = Blueprint("paystack", __name__, url_prefix="/paystack")

PAYSTACK_API = "https://api.paystack.co"
TIMEOUT_SECONDS = 20


def _secret_key():
    key = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("PAYSTACK_SECRET_KEY is not configured")
    return key


def _enabled():
    return os.getenv("PAYSTACK_ENABLED", "false").lower() == "true"


def _plans():
    """Prices are controlled on the server; never accept an amount from the browser."""
    return {
        "daily": {"amount_kobo": 10000, "duration_hours": 12, "label": "Daily access (12 hours)"},
        "monthly": {"amount_kobo": 150000, "duration_hours": 24 * 30, "label": "Monthly access"},
        "premium_monthly": {"amount_kobo": 300000, "duration_hours": 24 * 30, "label": "Premium monthly access"},
    }


def _db_path():
    # Set this to the same persistent SQLite path your existing db.py uses.
    return os.getenv("DATABASE_PATH", "subscriptions.db")


def _ensure_payments_table():
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paystack_payments (
                reference TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                whatsapp TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                amount_kobo INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'initialized',
                transaction_id TEXT,
                created_at TEXT NOT NULL,
                verified_at TEXT,
                fulfilled_at TEXT
            )
        """)
        conn.commit()


def _paystack_post(path, payload):
    response = requests.post(
        f"{PAYSTACK_API}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {_secret_key()}",
                 "Content-Type": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("status"):
        raise RuntimeError(body.get("message", "Paystack API returned an error"))
    return body["data"]


def _paystack_get(path):
    response = requests.get(
        f"{PAYSTACK_API}{path}",
        headers={"Authorization": f"Bearer {_secret_key()}"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("status"):
        raise RuntimeError(body.get("message", "Paystack API returned an error"))
    return body["data"]


def _verify_reference(reference):
    return _paystack_get(f"/transaction/verify/{reference}")


def _is_valid_webhook_signature(raw_body, supplied_signature):
    if not supplied_signature:
        return False
    expected = hmac.new(
        _secret_key().encode("utf-8"),
        raw_body,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, supplied_signature)


def _money_to_kobo(amount):
    try:
        return int(Decimal(str(amount)) * 100)
    except (InvalidOperation, ValueError, TypeError):
        return None


def _record_initialized(reference, email, whatsapp, plan_id, amount_kobo):
    _ensure_payments_table()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("""
            INSERT INTO paystack_payments
                (reference, email, whatsapp, plan_id, amount_kobo, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'initialized', ?)
        """, (reference, email, whatsapp, plan_id, amount_kobo,
              datetime.now(timezone.utc).isoformat()))
        conn.commit()


def _get_payment(reference):
    _ensure_payments_table()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM paystack_payments WHERE reference = ?", (reference,)
        ).fetchone()
        return dict(row) if row else None


def _mark_verified(reference, transaction_id):
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("""
            UPDATE paystack_payments
            SET status='verified', transaction_id=?, verified_at=?
            WHERE reference=? AND status IN ('initialized', 'verified')
        """, (str(transaction_id), datetime.now(timezone.utc).isoformat(), reference))
        conn.commit()


def _mark_fulfilled(reference):
    """Atomic claim prevents duplicate webhook/return processing."""
    with sqlite3.connect(_db_path()) as conn:
        cur = conn.execute("""
            UPDATE paystack_payments
            SET status='fulfilled', fulfilled_at=?
            WHERE reference=? AND status='verified' AND fulfilled_at IS NULL
        """, (datetime.now(timezone.utc).isoformat(), reference))
        conn.commit()
        return cur.rowcount == 1


def _verify_and_check(reference):
    payment = _get_payment(reference)
    if not payment:
        return None, "Unknown payment reference"

    transaction = _verify_reference(reference)
    if transaction.get("status") != "success":
        return None, "Payment is not successful"

    if str(transaction.get("reference", "")) != reference:
        return None, "Transaction reference mismatch"

    if _money_to_kobo(transaction.get("amount", 0) / 100) != payment["amount_kobo"]:
        # Paystack's amount is normally already expressed in the smallest currency unit.
        if int(transaction.get("amount", -1)) != payment["amount_kobo"]:
            return None, "Payment amount mismatch"

    if transaction.get("currency") != os.getenv("PAYSTACK_CURRENCY", "ZAR").upper():
        return None, "Payment currency mismatch"

    _mark_verified(reference, transaction.get("id", ""))
    return transaction, None


@paystack_bp.post("/start")
def start_checkout():
    if not _enabled():
        return jsonify(error="Paystack checkout is currently disabled"), 503

    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    email = str(data.get("email", "")).strip().lower()
    whatsapp = str(data.get("whatsapp", "")).strip()
    plan_id = str(data.get("plan", "")).strip()

    if "@" not in email or len(email) > 254:
        return jsonify(error="Enter a valid email address"), 400
    if not whatsapp or len(whatsapp) > 40:
        return jsonify(error="Enter a valid WhatsApp number"), 400

    plan = _plans().get(plan_id)
    if not plan:
        return jsonify(error="Invalid subscription plan"), 400

    callback_url = os.getenv("PAYSTACK_CALLBACK_URL", "").strip()
    if not callback_url.startswith("https://"):
        return jsonify(error="PAYSTACK_CALLBACK_URL must be your public HTTPS return URL"), 500

    # Use a random reference so customers cannot choose or reuse a reference.
    import secrets
    reference = "mt5_" + secrets.token_urlsafe(24)

    try:
        _record_initialized(reference, email, whatsapp, plan_id, plan["amount_kobo"])
        checkout = _paystack_post("/transaction/initialize", {
            "email": email,
            "amount": plan["amount_kobo"],
            "currency": os.getenv("PAYSTACK_CURRENCY", "ZAR").upper(),
            "reference": reference,
            "callback_url": callback_url,
            "metadata": {
                "service": "MT5 Signal Bot 01",
                "plan_id": plan_id,
                "whatsapp": whatsapp,
            },
        })
    except Exception:
        current_app.logger.exception("Paystack checkout initialization failed")
        return jsonify(error="Could not start checkout. Please try again later."), 502

    return jsonify(
        authorization_url=checkout["authorization_url"],
        reference=reference,
        plan=plan["label"],
    )


@paystack_bp.get("/return")
def checkout_return():
    reference = request.args.get("reference", "").strip()
    if not reference:
        return jsonify(error="Missing transaction reference"), 400
    try:
        transaction, error = _verify_and_check(reference)
    except Exception:
        current_app.logger.exception("Paystack return verification failed")
        return jsonify(error="Could not verify payment yet. Contact support if you were charged."), 502

    if error:
        return jsonify(status="not_verified", message=error), 400

    # TODO: Call the existing subscription fulfillment function here.
    # Required safe pattern:
    # 1. Claim the payment atomically using _mark_fulfilled(reference).
    # 2. If the claim succeeds, extend/create the subscription using payment["whatsapp"]
    #    and _plans()[payment["plan_id"]]["duration_hours"].
    # 3. If fulfillment fails after claiming, implement a retry/reconciliation path;
    #    do not silently leave a paid customer without access.
    payment = _get_payment(reference)
    return jsonify(
        status="payment_verified",
        message="Payment verified. Subscription activation must be connected to your existing bot.",
        plan=_plans()[payment["plan_id"]]["label"],
    )


@paystack_bp.post("/webhook")
def paystack_webhook():
    raw_body = request.get_data(cache=True)
    signature = request.headers.get("x-paystack-signature", "")
    try:
        if not _is_valid_webhook_signature(raw_body, signature):
            abort(401)
    except RuntimeError:
        current_app.logger.exception("Paystack secret key is missing")
        abort(500)

    event = request.get_json(silent=True) or {}
    if event.get("event") != "charge.success":
        return jsonify(received=True), 200

    data = event.get("data") or {}
    reference = str(data.get("reference", "")).strip()
    if not reference:
        return jsonify(error="Missing reference"), 400

    try:
        transaction, error = _verify_and_check(reference)
    except Exception:
        current_app.logger.exception("Paystack webhook verification failed")
        return jsonify(error="Verification temporarily failed"), 503

    if error:
        # Do not fulfil. Return success for a genuine signed event with a nonmatching
        # transaction so Paystack doesn't retry forever; investigate this in logs.
        current_app.logger.warning("Paystack event not fulfilled: %s", error)
        return jsonify(received=True, fulfilled=False), 200

    # TODO: connect this to the same shared fulfillment service used by /return.
    # Webhook and return must call one idempotent function; never duplicate logic.
    return jsonify(received=True, verified=True, fulfilled=False), 200
