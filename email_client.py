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

    access_duration = "12 hours" if plan == "daily" else "30 days"
    renewal_note = " - you will need to pay again for your next 12-hour window" if plan == "daily" else ""
    html_body = (
        f"<p>Thanks for subscribing to the {plan} plan! Your access is valid for <b>{access_duration}</b> "
        f"from now{renewal_note}.</p>"
        f"<p>Join the private Telegram channel here (one-time use link, valid for 12 hours):<br>"
        f"<a href='{invite_link}'>{invite_link}</a></p>"
        f"<p>Signals for BTC/USD, USD/CHF, Gold, and USD/ZAR will be posted there as they trigger.</p>"
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


def send_email(to_email: str, subject: str, html_body: str):
    """Send any email through Brevo's HTTPS API."""
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY is empty or not set.")
    if not FROM_EMAIL:
        raise RuntimeError("FROM_EMAIL is empty or not set - set it to a verified sender address in Brevo.")
    payload = {
        "sender": {"name": FROM_NAME, "email": FROM_EMAIL},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    headers = {"accept": "application/json", "api-key": BREVO_API_KEY, "content-type": "application/json"}
    resp = requests.post(BREVO_SEND_URL, json=payload, headers=headers, timeout=15)
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Brevo email send failed (status {resp.status_code}): {detail}")
    return resp.json()


def send_login_email(to_email: str, link: str, valid_for: str = "20 minutes", welcome: bool = False):
    """The email with a one-tap sign-in link for the subscriber website."""
    if welcome:
        subject = "Your signals access is active"
        intro = "<p>Thank you - your access is active.</p>"
    else:
        subject = "Your sign-in link"
        intro = "<p>Here is your sign-in link.</p>"
    html = (
        f"{intro}"
        f"<p><a href='{link}' style='display:inline-block;padding:12px 18px;background:#0a7d3b;color:#fff;"
        f"text-decoration:none;border-radius:6px;'>Sign in to your signals</a></p>"
        f"<p style='word-break:break-all;color:#666;font-size:0.9em;'>{link}</p>"
        f"<p style='color:#666;font-size:0.9em;'>This link works once and is valid for {valid_for}. "
        f"If you did not ask for it, you can ignore this email.</p>"
    )
    return send_email(to_email, subject, html)
