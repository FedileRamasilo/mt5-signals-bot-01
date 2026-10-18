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
import re
import hashlib
import urllib.parse
import requests
from flask import Flask, request, redirect, jsonify, make_response
from datetime import datetime, timedelta
from html import escape
from dotenv import load_dotenv

from db import (
    init_db,
    add_or_extend_subscription,
    get_or_create_referral_code,
    get_referral_stats,
    get_performance_stats,
    get_revenue_today,
    get_revenue_summary,
    attach_invite_link,
    get_members_overview,
    get_latest_subscription,
    get_subscription_by_token,
    claim_token,
    release_token,
    store_invite_link,
    log_payment_request,
    get_pending_requests,
    mark_requests_approved,
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
# Your own WhatsApp number (e.g. 27821234567) - lets customers send you proof of payment in one tap


def normalize_contact(raw):
    """Turn what a customer typed into one consistent ID: a WhatsApp number like 27821234567
    (or a lower-case email). Returns None if it doesn't look valid."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if "@" in raw:
        return raw.lower() if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", raw) else None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 10:
        digits = "27" + digits[1:]           # 082 123 4567 -> 27821234567
    elif len(digits) == 9:
        digits = "27" + digits               # 821234567 -> 27821234567
    return digits if 11 <= len(digits) <= 15 else None


ADMIN_WHATSAPP = normalize_contact(os.getenv("ADMIN_WHATSAPP", "")) or ""


def is_phone(contact):
    return bool(contact) and "@" not in contact


def make_reference(plan, contact):
    base = contact.split("@")[0] if "@" in contact else contact
    return f"{plan.upper()[:3]}-{base[:12].upper()}"


def whatsapp_url(contact, text):
    """Opens WhatsApp with the message already typed out for this person."""
    return f"https://wa.me/{contact}?text={urllib.parse.quote(text)}" if is_phone(contact) else None


def sast(dt, fmt="%d %b %H:%M"):
    return (dt + timedelta(hours=2)).strftime(fmt)


def build_access_message(link, expires_at, plan, referral_code=None):
    length = "12 hours" if plan == "daily" else "30 days"
    msg = (
        f"Payment received - thank you!\n\n"
        f"Your signals access ({length}) runs until {sast(expires_at)} SAST.\n\n"
        f"Tap your personal link to join the private channel:\n{link}\n\n"
        f"This link is for YOU only - if you share it, the next person is asked to pay. "
        f"When your time ends you are removed automatically. Renew any time: {YOUR_DOMAIN}/subscribe"
    )
    if referral_code:
        msg += f"\n\nShare with a trader friend and earn a free day: {YOUR_DOMAIN}/subscribe?ref={referral_code}"
    return msg


PAGE_STYLE = "font-family: sans-serif; max-width: 520px; margin: 30px auto; line-height: 1.6; padding: 0 14px;"
BUTTON_STYLE = ("display:inline-block; padding:12px 18px; background:#0a7d3b; color:#fff; "
                "text-decoration:none; border-radius:6px; font-size:1em; word-break:break-all;")


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
    """Bank-transfer page: shows your banking details and the customer's own reference."""
    contact = normalize_contact(request.args.get("contact") or request.args.get("email"))
    ref_code = request.args.get("ref", "")
    if plan not in ("daily", "monthly"):
        return "Invalid plan - use 'daily' or 'monthly'", 400
    if not contact:
        return redirect("/subscribe?error=1")

    amount = DAILY_PRICE if plan == "daily" else MONTHLY_PRICE
    access_duration = "12 hours" if plan == "daily" else "30 days"
    reference = make_reference(plan, contact)
    log_payment_request(contact, plan, ref_code or None, reference, float(amount))

    proof = ""
    if ADMIN_WHATSAPP:
        proof_text = f"Hi, I have paid R{amount} - reference {reference}"
        proof = (f'<p><a style="{BUTTON_STYLE}" href="https://wa.me/{ADMIN_WHATSAPP}?text={urllib.parse.quote(proof_text)}">'
                 f'Send proof of payment on WhatsApp</a></p>')

    where = f"to your WhatsApp ({escape(contact)})" if is_phone(contact) else f"to {escape(contact)}"
    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="{PAGE_STYLE}">
        <h2>Pay via Bank Transfer</h2>
        <p><b>Plan:</b> {plan.capitalize()} ({access_duration} access)<br>
        <b>Amount:</b> R{amount}</p>
        <p><b>Bank:</b> {escape(BANK_NAME)}<br>
        <b>Account holder:</b> {escape(BANK_ACCOUNT_HOLDER)}<br>
        <b>Account number:</b> {escape(BANK_ACCOUNT_NUMBER)}<br>
        <b>Branch code:</b> {escape(BANK_BRANCH_CODE)}<br>
        <b>Account type:</b> {escape(BANK_ACCOUNT_TYPE)}</p>
        <p><b>Use this exact reference:</b><br>
        <span style="font-size: 1.3em; background:#eee; padding: 4px 8px;">{escape(reference)}</span></p>
        <p>Once your payment is confirmed, your personal access link is sent {where}.</p>
        {proof}
    </body></html>
    """


def gate_page(message, status=200):
    """Shown to anyone who doesn't have a valid, unused personal link - they must pay first."""
    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="{PAGE_STYLE}">
        <h2>Access required</h2>
        <p>{escape(message)}</p>
        <p><a style="{BUTTON_STYLE}" href="/subscribe">Get your own access</a></p>
    </body></html>
    """, status


def join_page(link, expires_at):
    if link:
        body = (f'<p>Your access runs until <b>{sast(expires_at)} SAST</b>.</p>'
                f'<p><a style="{BUTTON_STYLE}" href="{escape(link)}">Join the private channel</a></p>'
                f'<p style="color:#666;font-size:0.9em;">Open this on the phone that has Telegram. '
                f'This button works once - please do not share it.</p>')
    else:
        body = (f'<p>You are already in the channel. Your access runs until <b>{sast(expires_at)} SAST</b>.</p>'
                f'<p>Open Telegram to see your signals.</p>')
    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="{PAGE_STYLE}"><h2>Welcome</h2>{body}</body></html>
    """


@app.route("/join/<token>")
def join(token):
    """A subscriber's personal link. The FIRST person to open it while their access is active gets a
    one-time Telegram link. Anyone else who opens it (e.g. a friend it was forwarded to) is sent to pay."""
    row = get_subscription_by_token(token)
    if not row:
        return gate_page("This link is not valid. Get your own access below.")
    now = datetime.utcnow()
    expires = datetime.fromisoformat(row["expires_at"])
    if expires - now < timedelta(seconds=60):
        return gate_page("This access has ended. Pay again to get a new link.")

    cookie_name = f"jt_{token}"
    if row["redeemed_at"]:
        # Already used. Only the same browser that used it may see it again (e.g. they refreshed the page).
        if request.cookies.get(cookie_name):
            if row["telegram_user_id"]:
                return join_page(None, expires)
            if row["invite_link"]:
                return join_page(row["invite_link"], expires)
        return gate_page("This link has already been used. Each link is for one person - get your own access below.")

    if not claim_token(token):
        return gate_page("This link has already been used. Each link is for one person - get your own access below.")
    seconds_left = int((expires - now).total_seconds())
    try:
        link = create_single_use_invite(expire_seconds=seconds_left)
    except Exception:
        release_token(token)
        return gate_page("We could not prepare your channel link just now. Please message us on WhatsApp.", 503)
    store_invite_link(token, link)
    resp = make_response(join_page(link, expires))
    resp.set_cookie(cookie_name, "1", max_age=seconds_left, httponly=True, samesite="Lax")
    return resp


@app.route("/admin/approve")
def admin_approve():
    """
    Approve a payment once you have seen it in your banking app. Creates this person's personal
    link and gives you a one-tap WhatsApp button to send it. Requires ADMIN_SECRET.
    Example: /admin/approve?contact=27821234567&plan=daily&secret=YOUR_ADMIN_SECRET
    """
    import traceback

    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "Not authorized - check your ADMIN_SECRET.", 403

    contact = normalize_contact(request.args.get("contact") or request.args.get("email"))
    plan = request.args.get("plan", "daily")
    ref_code = request.args.get("ref") or None
    if not contact:
        return "Missing or invalid contact - use the customer's WhatsApp number, e.g. ?contact=27821234567", 400
    if plan not in ("daily", "monthly"):
        return "Invalid plan", 400

    paid_amount = expected_amount_for_plan(plan)
    note = ""
    latest = get_latest_subscription(contact)
    just_approved = (
        latest and latest["plan"] == plan
        and datetime.utcnow() - datetime.fromisoformat(latest["created_at"]) < timedelta(seconds=120)
    )
    if just_approved:
        # A refresh or double-tap: do not charge/extend them twice, just show the same link again.
        expires_at = datetime.fromisoformat(latest["expires_at"])
        token = latest["access_token"]
        note = "<p style='color:#a60;'>Already approved a moment ago - showing the same link (not counted twice).</p>"
    else:
        try:
            expires_at = add_or_extend_subscription(
                email=contact, plan=plan, payfast_payment_id="manual-eft",
                referred_by_code=ref_code, amount=paid_amount,
            )
        except Exception:
            return f"<pre>Failed while saving the subscription:\n\n{traceback.format_exc()}</pre>", 500
        token = get_latest_subscription(contact)["access_token"]
    mark_requests_approved(contact)

    link = f"{YOUR_DOMAIN}/join/{token}"
    message = build_access_message(link, expires_at, plan, get_or_create_referral_code(contact))
    wa = whatsapp_url(contact, message)

    email_note = ""
    if not is_phone(contact):
        try:
            send_invite_email(to_email=contact, invite_link=link, plan=plan)
            email_note = "<p>Email sent.</p>"
        except Exception as e:
            email_note = f"<p>Email FAILED ({escape(str(e))}) - send the message below yourself.</p>"

    wa_button = (f'<p><a style="{BUTTON_STYLE}" href="{escape(wa)}">Send on WhatsApp</a></p>' if wa else "")
    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="{PAGE_STYLE}">
        <h2>Approved</h2>{note}
        <p><b>{escape(contact)}</b> - {escape(plan)} plan, R{paid_amount:g}<br>
        Access ends: <b>{sast(expires_at)} SAST</b></p>
        {wa_button}{email_note}
        <p>Or copy this message:</p>
        <textarea readonly rows="9" style="width:100%; font-size:0.95em;">{escape(message)}</textarea>
        <p style="color:#666;font-size:0.9em;">The personal link works for ONE person, once. If it is forwarded, the next person is sent to pay.</p>
    </body></html>
    """


@app.route("/admin/pending")
def admin_pending():
    """Everyone who asked to pay and has not been approved yet. Check your bank app, then tap Approve."""
    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "Not authorized - check your ADMIN_SECRET.", 403
    rows_html = ""
    for r in get_pending_requests():
        q = urllib.parse.urlencode({"contact": r["contact"], "plan": r["plan"], "ref": r["ref_code"] or "", "secret": secret})
        when = sast(datetime.fromisoformat(r["created_at"]))
        rows_html += (
            f"<tr><td>{when}</td><td>{escape(r['contact'])}</td><td>{escape(r['plan'])}</td>"
            f"<td>R{r['amount']:g}</td><td><b>{escape(r['reference'])}</b></td>"
            f"<td><a href='/admin/approve?{escape(q)}'>Approve</a></td></tr>"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='6'>Nobody waiting.</td></tr>"
    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="font-family: sans-serif; max-width: 820px; margin: 30px auto; padding: 0 12px;">
        <h2>Waiting for payment</h2>
        <p>Find the matching <b>reference</b> in your bank app, then tap Approve.</p>
        <table style="width:100%; border-collapse: collapse;">
            <tr style="text-align:left; border-bottom: 2px solid #ccc;">
                <th>Asked (SAST)</th><th>WhatsApp</th><th>Plan</th><th>Amount</th><th>Reference</th><th></th>
            </tr>{rows_html}
        </table>
    </body></html>
    """


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


PAYFAST_VALIDATE_URL = (
    "https://sandbox.payfast.co.za/eng/query/validate"
    if PAYFAST_MODE != "live"
    else "https://www.payfast.co.za/eng/query/validate"
)


def verify_payfast_signature(posted: dict, passphrase: str) -> bool:
    """Recompute the signature from the posted data (excluding 'signature' itself)
    and compare it to what PayFast sent - confirms the data wasn't tampered with."""
    received_signature = posted.get("signature", "")
    data_without_signature = {k: v for k, v in posted.items() if k != "signature"}
    expected_signature = build_signature(data_without_signature, passphrase)
    return expected_signature == received_signature


def verify_with_payfast_server(posted: dict) -> bool:
    """PayFast's official ITN check: post the notification data back to their
    validate endpoint. Only trust the payment if they confirm it's genuine -
    this stops anyone from faking a request straight to your /payfast/notify URL."""
    try:
        resp = requests.post(PAYFAST_VALIDATE_URL, data=posted, timeout=15)
        return resp.text.strip() == "VALID"
    except Exception:
        return False


def expected_amount_for_plan(plan: str) -> float:
    return float(DAILY_PRICE if plan == "daily" else MONTHLY_PRICE)


@app.route("/payfast/notify", methods=["POST"])
def payfast_notify():
    """
    PayFast calls this server-to-server after a successful payment.
    Follows PayFast's official ITN validation: verify the signature, confirm
    the notification with PayFast's own validate endpoint, and check the
    amount matches what we expect for the plan - only then do we grant access.
    This is what makes it safe to auto-approve real money with no human check.
    """
    posted = request.form.to_dict()

    payment_status = posted.get("payment_status")
    email = posted.get("email_address")
    plan = posted.get("custom_str1", "daily")
    ref_code = posted.get("custom_str2") or None
    payment_id = posted.get("pf_payment_id", "")

    if payment_status != "COMPLETE" or not email:
        return "OK", 200  # nothing to do - not a completed payment

    if not verify_payfast_signature(posted, PAYFAST_PASSPHRASE):
        print(f"REJECTED webhook for {email}: signature mismatch (possible tampering)")
        return "Invalid signature", 400

    posted_amount = None
    if plan in ("daily", "monthly"):
        try:
            posted_amount = float(posted.get("amount_gross", posted.get("amount", 0)))
            if abs(posted_amount - expected_amount_for_plan(plan)) > 0.01:
                print(f"REJECTED webhook for {email}: amount mismatch (got {posted_amount}, expected {expected_amount_for_plan(plan)})")
                return "Amount mismatch", 400
        except (ValueError, TypeError):
            print(f"REJECTED webhook for {email}: could not parse amount")
            return "Invalid amount", 400

    if not verify_with_payfast_server(posted):
        print(f"REJECTED webhook for {email}: PayFast server validation failed")
        return "Validation failed", 400

    # All checks passed - genuinely confirmed by PayFast, safe to grant access
    add_or_extend_subscription(
        email=email, plan=plan, payfast_payment_id=payment_id,
        referred_by_code=ref_code, amount=posted_amount,
    )
    try:
        invite_link = create_single_use_invite()
        attach_invite_link(email, invite_link)
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


@app.route("/admin/revenue")
def admin_revenue():
    """
    Private revenue dashboard - see today's total and a 7-day breakdown.
    Requires ADMIN_SECRET. Example: /admin/revenue?secret=YOUR_ADMIN_SECRET
    """
    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "Not authorized - check your ADMIN_SECRET.", 403

    today = get_revenue_today()
    summary = get_revenue_summary(days=7)

    by_plan_html = "".join(f"<li>{plan}: R{amt}</li>" for plan, amt in today["by_plan"].items())
    rows_html = "".join(
        f"<tr><td>{d['date']}</td><td>R{d['total']}</td><td>{d['count']}</td></tr>" for d in summary
    )

    return f"""
    <html><body style="font-family: sans-serif; max-width: 600px; margin: 40px auto; line-height: 1.6;">
        <h2>Revenue</h2>
        <h3>Today ({today['date']})</h3>
        <p style="font-size:1.8em;"><b>R{today['total_revenue']}</b></p>
        <p>{today['payment_count']} payment(s) today</p>
        <ul>{by_plan_html}</ul>
        <h3>Last 7 Days</h3>
        <table style="width:100%; border-collapse: collapse;">
            <tr style="text-align:left; border-bottom: 2px solid #ccc;">
                <th>Date</th><th>Total</th><th>Payments</th>
            </tr>
            {rows_html}
        </table>
    </body></html>
    """


@app.route("/subscribe")
def subscribe():
    """One generic link for ALL your advertising: the visitor types their WhatsApp number and
    is taken to the bank-transfer page with their own reference."""
    err = '<p style="color:#c00;">Please enter a valid WhatsApp number, e.g. 082 123 4567</p>' if request.args.get("error") else ""
    ref = escape(request.args.get("ref", ""))
    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="{PAGE_STYLE}">
        <h2>Get the signals</h2>
        <p>BTC/USD, USD/CHF, Gold and USD/ZAR - entry, stop-loss and take-profit sent to a private Telegram channel.</p>
        {err}
        <form action="/subscribe-go" method="get">
            <input type="hidden" name="ref" value="{ref}">
            <p><label>Your WhatsApp number<br>
            <input type="tel" name="contact" placeholder="082 123 4567" required style="width:100%; padding:10px; font-size:1em;"></label></p>
            <p><label>Plan<br>
            <select name="plan" style="width:100%; padding:10px; font-size:1em;">
                <option value="daily">12 hours - R{DAILY_PRICE}</option>
                <option value="monthly">30 days - R{MONTHLY_PRICE}</option>
            </select></label></p>
            <button type="submit" style="padding:12px 20px; font-size:1em;">Continue to payment</button>
        </form>
        <p style="color:#666;font-size:0.9em;">See our real results first: <a href="/performance">track record</a></p>
    </body></html>
    """


@app.route("/subscribe-go")
def subscribe_go():
    contact = normalize_contact(request.args.get("contact"))
    plan = request.args.get("plan", "daily")
    if not contact:
        return redirect("/subscribe?error=1")
    if plan not in ("daily", "monthly"):
        plan = "daily"
    params = {"contact": contact}
    if request.args.get("ref"):
        params["ref"] = request.args.get("ref")
    return redirect(f"/pay-eft/{plan}?{urllib.parse.urlencode(params)}")


@app.route("/admin/members")
def admin_members():
    """Who has paid, when their access ends, whether they have joined the channel,
    and whether the background jobs are alive. Requires ADMIN_SECRET."""
    secret = request.args.get("secret", "")
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        return "Not authorized - check your ADMIN_SECRET.", 403

    try:
        import scheduler as sched
        st = sched.status
    except Exception:
        st = {}

    def fmt(dt):
        return sast(dt, "%H:%M:%S") + " SAST" if dt else "never"

    now = datetime.utcnow().isoformat()
    rows_html = ""
    for m in get_members_overview():
        active = m["expires_at"] and m["expires_at"] >= now
        ends = sast(datetime.fromisoformat(m["expires_at"]))
        plan = m["plan"] or "daily"
        renew_link = f"{YOUR_DOMAIN}/pay-eft/{plan}?contact={urllib.parse.quote(m['email'])}"
        if active:
            text = f"Hi! Your signals access ends at {ends} SAST. Renew early so you do not miss any signals: {renew_link}"
        else:
            text = f"Hi! Your signals access has ended. Renew here to get your new personal link: {renew_link}"
        wa = whatsapp_url(m["email"], text)
        remind = f"<a href='{escape(wa)}'>Remind</a>" if wa else "-"
        rows_html += (
            f"<tr><td>{escape(m['email'])}</td><td>{escape(plan)}</td>"
            f"<td style='color:{'green' if active else '#c00'}'>{'ACTIVE' if active else 'expired'}</td>"
            f"<td>{ends}</td><td>{'yes' if m['telegram_user_id'] else '-'}</td><td>{remind}</td></tr>"
        )

    return f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
    <body style="font-family: sans-serif; max-width: 820px; margin: 30px auto; line-height: 1.5; padding: 0 12px;">
        <h2>Members</h2>
        <p style="background:#f3f3f3; padding:10px;">
        Background jobs started: {fmt(st.get('started'))}<br>
        Last signal check: {fmt(st.get('last_signal_check'))}<br>
        Last join check: {fmt(st.get('last_member_sync'))}<br>
        Last expiry check: {fmt(st.get('last_prune'))}</p>
        <p><a href="/admin/pending?secret={escape(secret)}">Waiting for payment</a></p>
        <table style="width:100%; border-collapse: collapse;">
            <tr style="text-align:left; border-bottom: 2px solid #ccc;">
                <th>WhatsApp</th><th>Plan</th><th>Status</th><th>Access ends (SAST)</th><th>Joined</th><th>Renewal reminder</th>
            </tr>
            {rows_html}
        </table>
    </body></html>
    """


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
    # Run the signal/expiry jobs inside this same process so they share the
    # website's database. Set RUN_SCHEDULER=false only if you run them elsewhere.
    if os.getenv("RUN_SCHEDULER", "true").lower() == "true":
        import scheduler
        scheduler.start_background_scheduler()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
