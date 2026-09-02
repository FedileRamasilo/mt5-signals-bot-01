"""
email_client.py
Sends the Telegram invite link to a paying subscriber via SMTP.
Works with Gmail (using an App Password), SendGrid's SMTP relay, Mailgun's
SMTP relay, or any other SMTP provider - just fill in the .env values.
"""

import os
import smtplib
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USERNAME)
FROM_NAME = os.getenv("FROM_NAME", "Market Signals")


def send_invite_email(to_email: str, invite_link: str, plan: str, referral_link: str | None = None):
    subject = "Your Market Signals access link"
    referral_block = ""
    if referral_link:
        referral_block = (
            f"\n\nWant a free day? Share your link with a friend - when they subscribe, "
            f"you get a free day added automatically:\n{referral_link}"
        )

    body = (
        f"Thanks for subscribing to the {plan} plan!\n\n"
        f"Join the private Telegram channel here (one-time use link):\n"
        f"{invite_link}\n\n"
        f"Signals for BTC/USD, USD/CHF, and Gold will be posted there as they trigger."
        f"{referral_block}\n\n"
        f"Note: this is market analysis, not personalised financial advice - "
        f"always manage your own risk."
    )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
