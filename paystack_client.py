"""
paystack_client.py
Talks to Paystack (card + instant payments, South African Rand).

How a payment flows:
  1. initialize_transaction()  -> Paystack gives us a checkout page URL; we send the customer there
  2. The customer pays on Paystack's page (we never see card details)
  3. Paystack tells us it worked in TWO ways: the customer is sent back to our /paystack/return page,
     and Paystack calls our /paystack/webhook. We never trust either on its own:
     verify_transaction() asks Paystack directly "was this reference really paid, and for how much?"

Test first with your TEST secret key (sk_test_...), then switch to the LIVE key (sk_live_...).
"""

import os
import hmac
import hashlib
import requests
from dotenv import load_dotenv

load_dotenv()

API = "https://api.paystack.co"


def secret_key() -> str:
    return (os.getenv("PAYSTACK_SECRET_KEY") or "").strip()


def is_enabled() -> bool:
    return bool(secret_key())


def _headers():
    return {"Authorization": f"Bearer {secret_key()}", "Content-Type": "application/json"}


def _fail(resp, what):
    try:
        detail = resp.json().get("message", resp.text)
    except Exception:
        detail = resp.text
    raise RuntimeError(f"Paystack {what} failed (status {resp.status_code}): {detail}")


def initialize_transaction(email: str, amount_rand: float, reference: str, callback_url: str, metadata: dict | None = None) -> str:
    """Start a payment. Returns the Paystack checkout URL to send the customer to.
    Paystack wants the amount in cents (R150 -> 15000)."""
    body = {
        "email": email,
        "amount": int(round(amount_rand * 100)),
        "currency": "ZAR",
        "reference": reference,
        "callback_url": callback_url,
        "metadata": metadata or {},
    }
    resp = requests.post(f"{API}/transaction/initialize", json=body, headers=_headers(), timeout=20)
    if not resp.ok:
        _fail(resp, "initialize")
    return resp.json()["data"]["authorization_url"]


def verify_transaction(reference: str) -> dict:
    """Ask Paystack directly about this reference. Returns its data: status, amount (cents), currency..."""
    resp = requests.get(f"{API}/transaction/verify/{reference}", headers=_headers(), timeout=20)
    if not resp.ok:
        _fail(resp, "verify")
    return resp.json().get("data", {})


def verify_webhook_signature(raw_body: bytes, header_signature: str) -> bool:
    """Paystack signs every webhook with HMAC-SHA512 of the raw body, keyed with your secret key."""
    key = secret_key()
    if not key or not header_signature:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header_signature)
