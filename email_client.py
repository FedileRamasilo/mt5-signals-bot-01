"""
email_client.py
Sends the Telegram invite link to a paying subscriber via Brevo's HTTPS API.

Why not plain SMTP? Railway (and most similar hosts on free/hobby plans)
block outbound SMTP connections (ports 25/465/587) to prevent spam abuse.
No amount of correct SMTP_HOST/PORT config will get past that - it's a
platform-level block, not a code or credentials issue. Brevo's API sends
over regular HTTPS instead, which is never blocked.

Free tier: 300 emails/day, no credit card required.
Sign up at https://www.brevo.com, verify your email, then go to
SMTP & API > API Keys to generate a key - that's your BREVO_API_KEY.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BREVO_API_KEY = (os.getenv("BREVO_API_KEY") or "").strip()
FROM_EMAIL = (os.getenv("FROM_EMAIL") or "").strip()
FROM_NAME = os.getenv("FROM_NAME", "Market Signals")

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


def send_invite_email(to_email: str, invite_link: str, plan: str, referral_link: str | None = None):
    if not BREVO_API_KEY:
        raise RuntimeError(
            "BREVO_API_KEY is empty or not set. Sign up free at brevo.com, "
            "get an API key under SMTP & API > API Keys, and set it in Railway's Variables."
        )
    if not FROM_EMAIL:
        raise RuntimeError("FROM_EMAIL is empty or not set - set it to a verified sender address in Brevo.")

    referral_block = ""
    if referral_link:
        referral_block = (
            f"<p>Want a free day? Share your link with a friend - when they subscribe, "
            f"you get a free day added automatically:<br>{referral_link}</p>"
        )

    html_body = (
        f"<p>Thanks for subscribing to the {plan} plan!</p>"
        f"<p>Join the private Telegram channel here (one-time use link):<br>"
        f"<a href='{invite_link}'>{invite_link}</a></p>"
        f"<p>Signals for BTC/USD, USD/CHF, and Gold will be posted there as they trigger.</p>"
        f"{referral_block}"
        f"<p style='color:#666;font-size:0.9em;'>Note: this is market analysis, not personalised "
        f"financial advice - always manage your own risk.</p>"
    )

    payload = {
        "sender": {"name": FROM_NAME, "email": FROM_EMAIL},
        "to": [{"email": to_email}],
        "subject": "Your Market Signals access link",
        "htmlContent": html_body,
    }
    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json",
    }

    resp = requests.post(BREVO_SEND_URL, json=payload, headers=headers, timeout=15)
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Brevo email send failed (status {resp.status_code}): {detail}")

    return resp.json()
