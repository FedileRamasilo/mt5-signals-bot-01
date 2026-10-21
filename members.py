"""
members.py
The subscriber website - for subscribers who don't use WhatsApp or Telegram.

  /signup            choose a plan, pay by card/instant (Paystack) or bank transfer
  /login             enter your email, get a one-tap sign-in link (no passwords)
  /signals           the dashboard: your access time + the signals that are open right now
  /logout

Rules that protect your income:
  * Signals are only shown while the subscriber's paid time is running.
  * A login works on at most MAX_DEVICES devices (default 2) - a newer device signs the oldest out,
    so one paid login can't be handed around to a whole group.
  * Sign-in links work once and expire.
  * Telegram is just an optional extra button on the dashboard.
"""

import os
import logging
import urllib.parse
from datetime import datetime, timedelta
from html import escape

from flask import request, redirect, session

import db
from email_client import send_login_email
from telegram_client import DISPLAY_NAMES

MAX_DEVICES = int(os.getenv("MAX_DEVICES", "2"))


def _price(v):
    """12345.0 -> '12345', 0.88340 -> '0.8834', 77791.5496 -> '77791.5496' (never lose precision)."""
    return f"{float(v):.4f}".rstrip("0").rstrip(".")


def register(app, h):
    """Adds the subscriber-website pages to the Flask app. `h` holds helpers shared with app.py."""
    normalize_contact = h["normalize_contact"]
    sast = h["sast"]
    domain = h["domain"]
    page_style = h["page_style"]
    button_style = h["button_style"]
    paystack_enabled = h["paystack_enabled"]
    start_card_checkout = h["start_card_checkout"]

    HEAD = '<html><head><meta name="viewport" content="width=device-width, initial-scale=1">'

    def current_member():
        sid = session.get("sid")
        return db.get_member_session(sid) if sid else None

    def start_session(email):
        session.permanent = True
        session["sid"] = db.create_member_session(email, MAX_DEVICES)

    # ---------------------------------------------------------------- signup
    @app.route("/signup")
    def signup():
        errors = {"1": "Please enter a valid email address."}
        err = f'<p style="color:#c00;">{errors[request.args.get("error")]}</p>' if request.args.get("error") in errors else ""
        email = escape(request.args.get("email", ""))
        ref = escape(request.args.get("ref", ""))
        card = paystack_enabled()
        card_button = (
            f'<button type="submit" name="method" value="card" style="{button_style} border:0; width:100%; margin-bottom:10px;">'
            f'Pay now - card / instant</button>' if card else
            '<p style="color:#666;">Card payment is not switched on yet - you can pay by bank transfer below.</p>'
        )
        return f"""{HEAD}</head><body style="{page_style}">
            <h2>Get the signals</h2>
            <p>BTC/USD, USD/CHF, Gold and USD/ZAR - live entry, stop-loss and take-profit levels on your own dashboard.
            No app and no Telegram account needed.</p>
            {err}
            <form action="/signup-go" method="get">
                <input type="hidden" name="ref" value="{ref}">
                <p><label>Your email<br>
                <input type="email" name="email" value="{email}" required placeholder="you@example.com"
                       style="width:100%; padding:10px; font-size:1em;"></label></p>
                <p><label>Plan<br>
                <select name="plan" style="width:100%; padding:10px; font-size:1em;">
                    <option value="daily">12 hours - R{h['daily_price']}</option>
                    <option value="monthly">30 days - R{h['monthly_price']}</option>
                </select></label></p>
                {card_button}
                <button type="submit" name="method" value="eft" style="padding:12px 18px; font-size:1em; width:100%;">Pay by bank transfer</button>
            </form>
            <p style="color:#666;font-size:0.9em;">Already a subscriber? <a href="/login">Sign in</a> &middot;
            <a href="/performance">See our track record</a></p>
        </body></html>"""

    @app.route("/signup-go")
    def signup_go():
        email = normalize_contact(request.args.get("email"))
        plan = request.args.get("plan", "daily")
        if not email or "@" not in email:
            return redirect("/signup?error=1")
        if plan not in ("daily", "monthly"):
            plan = "daily"
        if request.args.get("method") == "card" and paystack_enabled():
            params = {"email": email}
            if request.args.get("ref"):
                params["ref"] = request.args.get("ref")
            return redirect(f"/pay-web/{plan}?{urllib.parse.urlencode(params)}")
        return redirect(f"/pay-eft/{plan}?{urllib.parse.urlencode({'contact': email})}")

    @app.route("/pay-web/<plan>")
    def pay_web(plan):
        email = normalize_contact(request.args.get("email"))
        if plan not in ("daily", "monthly"):
            return "Invalid plan", 400
        if not email or "@" not in email:
            return redirect("/signup?error=1")
        if not paystack_enabled():
            return redirect(f"/pay-eft/{plan}?{urllib.parse.urlencode({'contact': email})}")
        return start_card_checkout(contact=email, email=email, plan=plan, ref_code=request.args.get("ref") or None, flow="web")

    # ----------------------------------------------------------------- login
    @app.route("/login", methods=["GET", "POST"])
    def login():
        note = ""
        if request.method == "POST":
            email = normalize_contact(request.form.get("email"))
            if not email or "@" not in email:
                note = '<p style="color:#c00;">Please enter a valid email address.</p>'
            else:
                # Same reply whether or not the email has an account, so nobody can use this to look people up.
                if db.member_exists(email) and db.recent_login_token_count(email, 10) < 3:
                    token = db.create_login_token(email, minutes=20)
                    try:
                        send_login_email(email, f"{domain}/login/{token}")
                    except Exception as e:
                        logging.error(f"Could not send sign-in email to {email}: {e}")
                note = ('<p style="background:#eef7ee; padding:10px;">If that email has an account, we have just sent a '
                        'sign-in link. It works once and lasts 20 minutes. Nothing arrived? Check your spam folder.</p>')
        return f"""{HEAD}</head><body style="{page_style}">
            <h2>Sign in</h2>
            {note}
            <form method="post" action="/login">
                <p><label>Your email<br>
                <input type="email" name="email" required style="width:100%; padding:10px; font-size:1em;"></label></p>
                <button type="submit" style="padding:12px 18px; font-size:1em;">Email me a sign-in link</button>
            </form>
            <p style="color:#666;font-size:0.9em;">No account yet? <a href="/signup">Get access</a></p>
        </body></html>"""

    @app.route("/login/<token>", methods=["GET", "POST"])
    def login_with_token(token):
        if request.method == "POST":
            email = db.consume_login_token(token)
            if not email:
                return redirect("/login?expired=1")
            start_session(email)
            return redirect("/signals")
        # GET only shows a button. The link is used when the button is tapped, so email security
        # scanners that "open" links in advance can't use it up before the subscriber does.
        if not db.login_token_valid(token):
            return f"""{HEAD}</head><body style="{page_style}">
                <h2>This sign-in link has expired</h2>
                <p>Sign-in links work once and last a short time.</p>
                <p><a style="{button_style}" href="/login">Get a new link</a></p></body></html>""", 410
        return f"""{HEAD}</head><body style="{page_style}">
            <h2>Welcome back</h2>
            <form method="post" action="/login/{escape(token)}">
                <button type="submit" style="{button_style} border:0;">Sign in</button>
            </form></body></html>"""

    @app.route("/logout")
    def logout():
        sid = session.get("sid")
        if sid:
            db.delete_member_session(sid)
        session.clear()
        return redirect("/login")

    # ------------------------------------------------------------- dashboard
    def signal_card(s, closed=False):
        name = escape(DISPLAY_NAMES.get(s["symbol"], s["symbol"]))
        buy = s["direction"] == "BUY"
        colour = "#0a7d3b" if buy else "#c0392b"
        if closed:
            win = s["outcome"] == "win"
            result = (f'<span style="color:{"#0a7d3b" if win else "#c0392b"}; font-weight:bold;">'
                      f'{"WIN" if win else "LOSS"}</span>')
            when = f"closed {sast(datetime.fromisoformat(s['closed_at']))}" if s.get("closed_at") else ""
        else:
            result = '<span style="color:#888;">OPEN</span>'
            when = f"posted {sast(datetime.fromisoformat(s['posted_at']))}"
        return f"""
        <div style="border:1px solid #ddd; border-left:6px solid {colour}; border-radius:6px; padding:10px 12px; margin:10px 0;">
            <div><b>{name}</b> &nbsp;<span style="color:{colour}; font-weight:bold;">{escape(s['direction'])}</span>
            <span style="float:right;">{result}</span></div>
            <div style="margin-top:6px;">Entry <b>{_price(s['entry'])}</b> &middot;
            Stop loss <b>{_price(s['sl'])}</b> &middot; Take profit <b>{_price(s['tp'])}</b></div>
            <div style="color:#888; font-size:0.85em; margin-top:4px;">{when} SAST</div>
        </div>"""

    @app.route("/signals")
    def signals_page():
        email = current_member()
        if not email:
            return redirect("/login")
        access = db.get_member_access(email)
        if not access:
            return redirect("/signup")

        now = datetime.utcnow()
        expires = datetime.fromisoformat(access["expires_at"])
        active = expires > now
        top = f'<p style="color:#666;">Signed in as {escape(email)} &middot; <a href="/logout">Sign out</a></p>'

        if not active:
            return f"""{HEAD}</head><body style="{page_style}">
                <h2>Your access has ended</h2>{top}
                <p>Your paid time finished on <b>{sast(expires)} SAST</b>. Renew to see live signals again.</p>
                <p><a style="{button_style}" href="/signup?email={urllib.parse.quote(email)}">Renew now</a></p>
                <p style="color:#666;font-size:0.9em;"><a href="/performance">See our track record</a></p>
            </body></html>"""

        left = expires - now
        hours, minutes = left.days * 24 + left.seconds // 3600, (left.seconds % 3600) // 60
        data = db.get_dashboard_signals()
        open_html = "".join(signal_card(s) for s in data["open"]) or (
            '<p style="color:#666;">No open signals right now. New ones appear here the moment they trigger '
            '(we check every 15 minutes) - this page refreshes by itself.</p>')
        closed_html = "".join(signal_card(s, closed=True) for s in data["closed"])
        closed_block = f"<h3>Recently closed</h3>{closed_html}" if closed_html else ""

        telegram = ""
        if os.getenv("TELEGRAM_SIGNAL_CHANNEL_ID"):
            if access["telegram_user_id"]:
                telegram = '<p style="color:#666;">&#10003; You are also connected to the Telegram alerts channel.</p>'
            elif access["latest_token"] and not access["latest_redeemed"]:
                telegram = (f'<p style="color:#666;">Want alerts on your phone? Telegram is optional: '
                            f'<a href="/join/{escape(access["latest_token"])}">join the alerts channel</a>.</p>')

        return f"""{HEAD}<meta http-equiv="refresh" content="60"></head><body style="{page_style}">
            <h2>Your signals</h2>{top}
            <div style="background:#eef7ee; padding:10px 12px; border-radius:6px;">
                <b>Access active</b> until <b>{sast(expires)} SAST</b> ({hours}h {minutes}m left)
            </div>
            <h3>Open signals</h3>{open_html}
            {closed_block}
            {telegram}
            <p style="color:#888; font-size:0.8em; margin-top:30px;">Updated {sast(now, '%H:%M')} SAST. Market analysis only - not financial advice.
            Trade with money you can afford to lose. Your login works on up to {MAX_DEVICES} devices.</p>
        </body></html>"""

    # ------------------------------------------------------------ admin tool
    @app.route("/admin/login-link")
    def admin_login_link():
        """Make a one-time sign-in link for a subscriber yourself (if their email never arrives)."""
        if not h["admin_secret"] or request.args.get("secret", "") != h["admin_secret"]:
            return "Not authorized - check your ADMIN_SECRET.", 403
        email = normalize_contact(request.args.get("contact") or request.args.get("email"))
        if not email or "@" not in email:
            return "Use the subscriber's email: ?contact=them@example.com", 400
        if not db.member_exists(email):
            return "No subscriber with that email.", 404
        link = f"{domain}/login/{db.create_login_token(email, minutes=1440)}"
        return f"""{HEAD}</head><body style="{page_style}">
            <h2>Sign-in link</h2><p>For <b>{escape(email)}</b> - works once, valid for 24 hours. Send it to them yourself:</p>
            <p style="word-break:break-all; background:#eee; padding:10px;">{escape(link)}</p></body></html>"""
