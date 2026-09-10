"""
app.py
Flask server:
  - /pay/daily and /pay/monthly  -> redirect user into PayFast checkout
  - /payfast/notify              -> PayFast ITN (Instant Transaction Notification) webhook
  - /                            -> simple status page

Deploy this behind HTTPS (Railway, Render, a VPS + Caddy/Nginx, etc).
PayFast requires the notify_url to be publicly reachable.
"""

import os
import hashlib
import urllib.parse
import requests
from flask import Flask, request, redirect, jsonify
from dotenv import load_dotenv

from db import (
    init_db,
    add_or_extend_subscription,
    get_or_create_referral_code,
    get_referral_stats,
    get_performance_stats,
)
from telegram_client import create_single_use_invite, post_signal
from email_client import send_invite_email

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev")
init_db()  # ensure tables exist regardless of how the app is started

PAYFAST_MERCHANT_ID = os.getenv("PAYFAST_MERCHANT_ID")
PAYFAST_MERCHANT_KEY = os.getenv("PAYFAST_MERCHANT_KEY")
PAYFAST_PASSPHRASE = os.getenv("PAYFAST_PASSPHRASE", "")
PAYFAST_MODE = os.getenv("PAYFAST_MODE", "live")
PAYFAST_PROCESS_URL = (
    "https://sandbox.payfast.co.za/eng/process"
    if PAYFAST_MODE != "live"
    else "https://www.payfast.co.za/eng/process"
)

DAILY_PRICE = os.getenv("DAILY_PRICE", "100")
MONTHLY_PRICE = os.getenv("MONTHLY_PRICE", "1500")

YOUR_DOMAIN = os.getenv("PUBLIC_DOMAIN", "https://your-domain.example.com")

# --- Manual EFT (bank transfer) fallback, for launching before PayFast is fully wired up ---
BANK_NAME = os.getenv("BANK_NAME", "Your Bank")
BANK_ACCOUNT_HOLDER = os.getenv("BANK_ACCOUNT_HOLDER", "Your Name")
BANK_ACCOUNT_NUMBER = os.getenv("BANK_ACCOUNT_NUMBER", "0000000000")
BANK_BRANCH_CODE = os.getenv("BANK_BRANCH_CODE", "000000")
BANK_ACCOUNT_TYPE = os.getenv("BANK_ACCOUNT_TYPE", "Cheque/Current")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")  # required to use the manual-approve link


def build_signature(data: dict, passphrase: str) -> str:
    pairs = [f"{k}={urllib.parse.quote_plus(str(v))}" for k, v in data.items() if v not in (None, "")]
    query = "&".join(pairs)
    if passphrase:
        query += f"&passphrase={urllib.parse.quote_plus(passphrase)}"
    return hashlib.md5(query.encode()).hexdigest()


@app.route("/")
def index():
    return jsonify({"status": "ok", "service": "MT5 signals subscription server"})


@app.route("/pay/<plan>")
def pay(plan):
    """plan = 'daily' or 'monthly'. Query params: ?email=user@example.com (required),
    &ref=REFERRALCODE (optional, gives the referrer a free bonus day when this signup completes)."""
    email = request.args.get("email")
    ref_code = request.args.get("ref", "")
    if not email:
        return "Missing ?email= in the URL. Example: /pay/daily?email=you@example.com", 400
    if plan not in ("daily", "monthly"):
        return "Invalid plan - use 'daily' or 'monthly' in the URL, e.g. /pay/daily?email=...", 400

    # Clear, readable checks instead of letting a bad config crash silently
    problems = []
    if not PAYFAST_MERCHANT_ID or PAYFAST_MERCHANT_ID.strip() in ("", "your_merchant_id"):
        problems.append("PAYFAST_MERCHANT_ID is missing or still a placeholder in Railway's Variables.")
    if not PAYFAST_MERCHANT_KEY or PAYFAST_MERCHANT_KEY.strip() in ("", "your_merchant_key"):
        problems.append("PAYFAST_MERCHANT_KEY is missing or still a placeholder in Railway's Variables.")
    if not YOUR_DOMAIN or "example.com" in YOUR_DOMAIN:
        problems.append("PUBLIC_DOMAIN is missing or still the placeholder - set it to your real Railway URL.")

    try:
        amount_value = float(DAILY_PRICE if plan == "daily" else MONTHLY_PRICE)
    except ValueError:
        problems.append(
            f"{'DAILY_PRICE' if plan == 'daily' else 'MONTHLY_PRICE'} isn't a valid number "
            f"(currently: '{DAILY_PRICE if plan == 'daily' else MONTHLY_PRICE}')."
        )
        amount_value = None

    if problems:
        message = "Configuration problem(s) found:\n- " + "\n- ".join(problems)
        return message, 500

    data = {
        "merchant_id": PAYFAST_MERCHANT_ID,
        "merchant_key": PAYFAST_MERCHANT_KEY,
        "return_url": f"{YOUR_DOMAIN}/thank-you",
        "cancel_url": f"{YOUR_DOMAIN}/cancelled",
        "notify_url": f"{YOUR_DOMAIN}/payfast/notify",
        "email_address": email,
        "m_payment_id": f"{plan}-{email}",
        "amount": f"{amount_value:.2f}",
        "item_name": f"MT5 Signals - {plan.capitalize()} subscription",
        "custom_str1": plan,
        "custom_str2": ref_code,
    }
    data["signature"] = build_signature(data, PAYFAST_PASSPHRASE)
    query_string = urllib.parse.urlencode(data)
    return redirect(f"{PAYFAST_PROCESS_URL}?{query_string}")


@app.route("/pay-eft/<plan>")
def pay_eft(plan):
    """
    Manual bank transfer flow: shows the subscriber your banking details and a
    unique reference number to use. You check your banking app, then use
    /admin/approve to grant access once you see the payment land.
    """
    email = request.args.get("email")
    ref_code = request.args.get("ref", "")
    if not email:
        return "Missing ?email= in the URL. Example: /pay-eft/daily?email=you@example.com", 400
    if plan not in ("daily", "monthly"):
        return "Invalid plan - use 'daily' or 'monthly'", 400

    amount = DAILY_PRICE if plan == "daily" else MONTHLY_PRICE
    # A short, readable reference so you can match it to your bank statement
    reference = f"{plan.upper()[:3]}-{email.split('@')[0][:10].upper()}"

    referral_note = f"&ref={ref_code}" if ref_code else ""

    return f"""
    <html><body style="font-family: sans-serif; max-width: 500px; margin: 40px auto; line-height: 1.6;">
        <h2>Pay via Bank Transfer</h2>
        <p><b>Plan:</b> {plan.capitalize()}<br>
        <b>Amount:</b> R{amount}</p>
        <p><b>Bank:</b> {BANK_NAME}<br>
        <b>Account holder:</b> {BANK_ACCOUNT_HOLDER}<br>
        <b>Account number:</b> {BANK_ACCOUNT_NUMBER}<br>
        <b>Branch code:</b> {BANK_BRANCH_CODE}<br>
        <b>Account type:</b> {BANK_ACCOUNT_TYPE}</p>
        <p><b>Reference (important - use this exact reference):</b><br>
        <span style="font-size: 1.3em; background:#eee; padding: 4px 8px;">{reference}</span></p>
        <p>Once you've made the transfer, your access will be activated once the payment
        is confirmed - this can take a few minutes to a few hours depending on your bank.</p>
        <p style="color:#666; font-size:0.9em;">Your email on file: {email}{"<br>Referred by: " + ref_code if ref_code else ""}</p>
    </body></html>
    """


@app.route("/admin/approve")
def admin_approve():
    """
    Manual approval endpoint - use this yourself once you've checked your banking
    app and confirmed a payment landed. Requires the secret ADMIN_SECRET you set
    in Railway, so no one else can grant themselves free access.

    Example: /admin/approve?email=them@example.com&plan=daily&secret=YOUR_ADMIN_SECRET
    """
    import traceback

    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "Not authorized - check your ADMIN_SECRET.", 403

    email = request.args.get("email")
    plan = request.args.get("plan", "daily")
    ref_code = request.args.get("ref") or None
    if not email:
        return "Missing ?email=", 400
    if plan not in ("daily", "monthly"):
        return "Invalid plan", 400

    try:
        add_or_extend_subscription(email=email, plan=plan, payfast_payment_id="manual-eft", referred_by_code=ref_code)
    except Exception:
        return f"<pre>Failed while saving the subscription:\n\n{traceback.format_exc()}</pre>", 500

    try:
        invite_link = create_single_use_invite()
        my_referral_code = get_or_create_referral_code(email)
        my_referral_link = f"{YOUR_DOMAIN}/pay/daily?email=FRIEND_EMAIL&ref={my_referral_code}"
        send_invite_email(to_email=email, invite_link=invite_link, plan=plan, referral_link=my_referral_link)
        return f"Approved {email} for {plan} plan. Invite emailed."
    except Exception:
        return f"<pre>Approved in database, but failed on invite/email step:\n\n{traceback.format_exc()}</pre>", 500


@app.route("/admin/test-signal")
def admin_test_signal():
    """
    Posts a sample signal to your private channel immediately, so you can see
    the formatting without waiting for a real EMA/RSI crossover to trigger.
    Requires ADMIN_SECRET. Example:
    /admin/test-signal?secret=YOUR_ADMIN_SECRET&symbol=BTC
    """
    import traceback

    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "Not authorized - check your ADMIN_SECRET.", 403

    symbol = request.args.get("symbol", "BTC").upper()
    sample_signal = {
        "symbol": symbol,
        "direction": "BUY",
        "entry": 65432.10,
        "sl": 64800.00,
        "tp": 66500.00,
        "rsi": 58.3,
        "timestamp": "TEST POST - not a real signal",
    }
    try:
        post_signal(sample_signal)
        return f"Test signal posted to your private channel for {symbol}. Check Telegram."
    except Exception:
        return f"<pre>Failed to post test signal:\n\n{traceback.format_exc()}</pre>", 500


@app.route("/payfast/notify", methods=["POST"])
def payfast_notify():
    """
    PayFast calls this server-to-server after a successful payment.
    IMPORTANT: In production, also validate the source IP and re-confirm
    with PayFast's validate endpoint - see PayFast's ITN docs.
    """
    posted = request.form.to_dict()

    payment_status = posted.get("payment_status")
    email = posted.get("email_address")
    plan = posted.get("custom_str1", "daily")
    ref_code = posted.get("custom_str2") or None
    payment_id = posted.get("pf_payment_id", "")

    if payment_status == "COMPLETE" and email:
        add_or_extend_subscription(email=email, plan=plan, payfast_payment_id=payment_id, referred_by_code=ref_code)
        try:
            invite_link = create_single_use_invite()
            my_referral_code = get_or_create_referral_code(email)
            my_referral_link = f"{YOUR_DOMAIN}/pay/daily?email=FRIEND_EMAIL&ref={my_referral_code}"
            send_invite_email(
                to_email=email,
                invite_link=invite_link,
                plan=plan,
                referral_link=my_referral_link,
            )
            print(f"Subscriber {email} paid for {plan}. Invite emailed.")
        except Exception as e:
            print(f"Failed to create/send Telegram invite for {email}: {e}")

    return "OK", 200


@app.route("/my-referrals")
def my_referrals():
    email = request.args.get("email")
    if not email:
        return "Missing ?email=", 400
    code = get_or_create_referral_code(email)
    stats = get_referral_stats(email)
    return jsonify({
        "referral_code": code,
        "referral_link": f"{YOUR_DOMAIN}/pay/daily?email=FRIEND_EMAIL&ref={code}",
        "friends_referred": stats["referral_count"],
        "bonus_days_earned": stats["bonus_days_earned"],
    })


@app.route("/performance")
def performance():
    """
    Public track record - every signal ever posted, and whether it hit TP
    (win) or SL (loss), tracked automatically. This is the single biggest
    trust-builder for a signals service: real numbers, good and bad, not a
    cherry-picked highlight reel.
    """
    stats = get_performance_stats()
    win_rate_display = f"{stats['win_rate']}%" if stats["win_rate"] is not None else "Not enough closed signals yet"

    rows_html = ""
    for s in stats["recent"]:
        color = {"win": "green", "loss": "red", "open": "#888"}.get(s["outcome"], "#888")
        rows_html += (
            f"<tr><td>{s['posted_at'][:16]}</td><td>{s['symbol']}</td><td>{s['direction']}</td>"
            f"<td>{s['entry']}</td>"
            f"<td style='color:{color};font-weight:bold;'>{s['outcome'].upper()}</td></tr>"
        )

    return f"""
    <html><body style="font-family: sans-serif; max-width: 700px; margin: 40px auto; line-height: 1.6;">
        <h2>Our Track Record</h2>
        <p>Every signal we've posted, tracked automatically - wins and losses, no cherry-picking.</p>
        <div style="display:flex; gap: 24px; margin: 24px 0;">
            <div><b style="font-size:1.5em;">{stats['total_signals']}</b><br>Total signals</div>
            <div><b style="font-size:1.5em;color:green;">{stats['wins']}</b><br>Wins</div>
            <div><b style="font-size:1.5em;color:red;">{stats['losses']}</b><br>Losses</div>
            <div><b style="font-size:1.5em;">{stats['open']}</b><br>Still open</div>
            <div><b style="font-size:1.5em;">{win_rate_display}</b><br>Win rate</div>
        </div>
        <h3>Recent Signals</h3>
        <table style="width:100%; border-collapse: collapse;">
            <tr style="text-align:left; border-bottom: 2px solid #ccc;">
                <th>Posted</th><th>Symbol</th><th>Direction</th><th>Entry</th><th>Outcome</th>
            </tr>
            {rows_html}
        </table>
    </body></html>
    """


@app.route("/thank-you")
def thank_you():
    return "Payment received - check your email for your Telegram invite link."


@app.route("/cancelled")
def cancelled():
    return "Payment cancelled."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
