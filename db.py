"""
db.py
Minimal SQLite subscriber tracking: who paid, which plan, when it expires,
plus referral tracking (each subscriber gets a code; referring a paying
friend earns the referrer a free bonus day).
"""

import os
import logging
import sqlite3
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# If the database location can't be used (for example DATABASE_PATH points at /data but the Railway
# Volume isn't attached yet), don't crash the whole site: fall back to a temporary file and show a loud
# warning on /admin/members. A temporary file is WIPED on every redeploy, so attach the Volume.
storage_warning = None


def _resolve_db_path() -> str:
    global storage_warning
    path = os.getenv("DATABASE_PATH", "subscribers.db")
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        sqlite3.connect(path).close()
        return path
    except Exception as e:
        storage_warning = (f"Could not use DATABASE_PATH={path} ({e}). Running on a TEMPORARY database that is "
                           f"wiped on every redeploy. Attach a Railway Volume at /data to keep your records.")
        logging.error(storage_warning)
        return "subscribers.db"


DB_PATH = _resolve_db_path()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            telegram_user_id INTEGER,
            plan TEXT,                 -- 'daily' or 'monthly'
            payfast_payment_id TEXT,
            expires_at TEXT,
            created_at TEXT,
            referral_code TEXT UNIQUE,     -- this subscriber's own code to share
            referred_by_code TEXT,         -- code of whoever referred them, if any
            amount REAL,                   -- what they actually paid, for revenue tracking
            invite_link TEXT,              -- the one-time Telegram link issued when the personal link is used
            access_token TEXT,             -- secret part of this payment's personal /join/<token> link
            redeemed_at TEXT               -- set the first time the personal link is used
        )
        """
    )
    # Safe migration: adds the 'amount' column if the table already existed
    # without it (e.g. persistent storage from before this change).
    for ddl in ("ALTER TABLE card_payments ADD COLUMN flow TEXT DEFAULT 'link'",
                "ALTER TABLE card_payments ADD COLUMN session_issued INTEGER DEFAULT 0",
                "ALTER TABLE subscribers ADD COLUMN amount REAL",
                "ALTER TABLE subscribers ADD COLUMN invite_link TEXT",
                "ALTER TABLE subscribers ADD COLUMN access_token TEXT",
                "ALTER TABLE subscribers ADD COLUMN redeemed_at TEXT"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact TEXT,
            plan TEXT,
            amount REAL,
            reference TEXT,
            ref_code TEXT,
            status TEXT DEFAULT 'pending',   -- 'pending' until you approve
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS card_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reference TEXT UNIQUE,
            contact TEXT,
            email TEXT,
            plan TEXT,
            amount REAL,
            ref_code TEXT,
            status TEXT DEFAULT 'pending',   -- 'pending' until Paystack confirms, then 'paid'
            access_token TEXT,               -- the personal /join/<token> created for this payment
            created_at TEXT,
            paid_at TEXT,
            flow TEXT DEFAULT 'link',        -- 'link' = WhatsApp/Telegram, 'web' = subscriber website
            session_issued INTEGER DEFAULT 0 -- the automatic first sign-in after paying can be used once
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT UNIQUE,
            email TEXT,
            created_at TEXT,
            expires_at TEXT,
            used_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS member_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sid_hash TEXT UNIQUE,
            email TEXT,
            created_at TEXT,
            last_seen TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS referral_bonuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_email TEXT,
            referred_email TEXT,
            bonus_days INTEGER,
            granted_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            direction TEXT,         -- 'BUY' or 'SELL'
            entry REAL,
            sl REAL,
            tp REAL,
            posted_at TEXT,
            outcome TEXT DEFAULT 'open',   -- 'open', 'win', or 'loss'
            closed_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def generate_referral_code(email: str) -> str:
    base = email.split("@")[0][:6].upper()
    suffix = secrets.token_hex(3).upper()
    return f"{base}-{suffix}"


def get_or_create_referral_code(email: str) -> str:
    """Every subscriber gets a stable referral code tied to their email."""
    conn = get_conn()
    row = conn.execute(
        "SELECT referral_code FROM subscribers WHERE email = ? AND referral_code IS NOT NULL LIMIT 1",
        (email,),
    ).fetchone()
    if row:
        conn.close()
        return row["referral_code"]

    code = generate_referral_code(email)
    conn.close()
    return code


def find_email_by_referral_code(code: str) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT email FROM subscribers WHERE referral_code = ? LIMIT 1", (code,)).fetchone()
    conn.close()
    return row["email"] if row else None


def add_or_extend_subscription(
    email: str,
    plan: str,
    payfast_payment_id: str,
    telegram_user_id: int | None = None,
    referred_by_code: str | None = None,
    amount: float | None = None,
) -> datetime:
    conn = get_conn()
    now = datetime.utcnow()
    duration = timedelta(hours=12) if plan == "daily" else timedelta(days=30)
    expires_at = now + duration

    # Only the first subscription row for this email stores their referral code -
    # renewals leave it NULL, since referral_code must be unique and
    # get_or_create_referral_code() already knows to find the original row.
    existing = conn.execute(
        "SELECT referral_code FROM subscribers WHERE email = ? AND referral_code IS NOT NULL LIMIT 1",
        (email,),
    ).fetchone()
    own_code = existing["referral_code"] if existing else generate_referral_code(email)
    code_for_this_row = None if existing else own_code

    conn.execute(
        """
        INSERT INTO subscribers
            (email, telegram_user_id, plan, payfast_payment_id, expires_at, created_at,
             referral_code, referred_by_code, amount, access_token)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (email, telegram_user_id, plan, payfast_payment_id, expires_at.isoformat(), now.isoformat(),
         code_for_this_row, referred_by_code, amount, secrets.token_urlsafe(9)),
    )
    conn.commit()
    conn.close()

    # If this signup came via a referral, grant the referrer a free bonus day
    if referred_by_code:
        grant_referral_bonus(referred_by_code, referred_email=email)

    return expires_at


def grant_referral_bonus(referrer_code: str, referred_email: str, bonus_days: int = 1):
    referrer_email = find_email_by_referral_code(referrer_code)
    if not referrer_email or referrer_email == referred_email:
        return  # unknown code, or someone tried to refer themselves

    conn = get_conn()
    # Extend the referrer's most recent subscription by bonus_days
    row = conn.execute(
        "SELECT id, expires_at FROM subscribers WHERE email = ? ORDER BY expires_at DESC LIMIT 1",
        (referrer_email,),
    ).fetchone()

    if row:
        current_expiry = datetime.fromisoformat(row["expires_at"])
        # Extend from whichever is later: now, or their current expiry
        base = max(current_expiry, datetime.utcnow())
        new_expiry = base + timedelta(days=bonus_days)
        conn.execute("UPDATE subscribers SET expires_at = ? WHERE id = ?", (new_expiry.isoformat(), row["id"]))

    conn.execute(
        "INSERT INTO referral_bonuses (referrer_email, referred_email, bonus_days, granted_at) VALUES (?, ?, ?, ?)",
        (referrer_email, referred_email, bonus_days, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_referral_stats(email: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) as c, COALESCE(SUM(bonus_days),0) as days FROM referral_bonuses WHERE referrer_email = ?",
        (email,),
    ).fetchone()
    conn.close()
    return {"referral_count": row["c"], "bonus_days_earned": row["days"]}


def attach_invite_link(email: str, invite_link: str):
    """Remember which one-time link was issued for this person's latest payment."""
    conn = get_conn()
    conn.execute(
        "UPDATE subscribers SET invite_link = ? WHERE id = "
        "(SELECT id FROM subscribers WHERE email = ? ORDER BY id DESC LIMIT 1)",
        (invite_link, email),
    )
    conn.commit()
    conn.close()


def link_telegram_user(invite_link: str, telegram_user_id: int) -> str | None:
    """Called when Telegram reports someone joined via one of our links.
    Saves their Telegram ID against that subscriber's email. Returns the email,
    or None if the link isn't one we issued (i.e. a join we can't match to a payment)."""
    conn = get_conn()
    row = conn.execute("SELECT email FROM subscribers WHERE invite_link = ? LIMIT 1", (invite_link,)).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("UPDATE subscribers SET telegram_user_id = ? WHERE email = ?", (telegram_user_id, row["email"]))
    conn.commit()
    conn.close()
    return row["email"]


def get_expired_subscribers() -> list:
    """People whose LATEST payment has run out and who are known to be in the channel.
    Grouped per email so a renewal protects them from being removed by an older, expired row."""
    conn = get_conn()
    now = datetime.utcnow().isoformat()
    rows = conn.execute(
        "SELECT email, MAX(telegram_user_id) AS telegram_user_id, MAX(expires_at) AS last_expiry "
        "FROM subscribers GROUP BY email "
        "HAVING MAX(expires_at) < ? AND MAX(telegram_user_id) IS NOT NULL",
        (now,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_telegram_user(email: str):
    """After removing someone, forget their ID so we don't try again; a new join re-links them."""
    conn = get_conn()
    conn.execute("UPDATE subscribers SET telegram_user_id = NULL WHERE email = ?", (email,))
    conn.commit()
    conn.close()


def recent_signal_exists(symbol: str, direction: str, minutes: int = 30) -> bool:
    """Guards against posting the same signal twice (e.g. after a restart)."""
    conn = get_conn()
    cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM signals WHERE symbol = ? AND direction = ? AND posted_at >= ? LIMIT 1",
        (symbol, direction, cutoff),
    ).fetchone()
    conn.close()
    return row is not None


def get_members_overview() -> list:
    """One row per person: latest plan, when access ends, and whether they've joined the channel."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT s.email AS email, MAX(s.expires_at) AS expires_at, MAX(s.telegram_user_id) AS telegram_user_id, "
        "(SELECT plan FROM subscribers x WHERE x.email = s.email ORDER BY x.id DESC LIMIT 1) AS plan, "
        "(SELECT access_token FROM subscribers x WHERE x.email = s.email ORDER BY x.id DESC LIMIT 1) AS latest_token, "
        "(SELECT redeemed_at FROM subscribers x WHERE x.email = s.email ORDER BY x.id DESC LIMIT 1) AS latest_redeemed "
        "FROM subscribers s GROUP BY s.email ORDER BY MAX(s.expires_at) DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_count() -> int:
    conn = get_conn()
    now = datetime.utcnow().isoformat()
    count = conn.execute(
        "SELECT COUNT(DISTINCT email) as c FROM subscribers WHERE expires_at >= ?", (now,)
    ).fetchone()["c"]
    conn.close()
    return count


def log_signal(symbol: str, direction: str, entry: float, sl: float, tp: float) -> int:
    """Record a signal as posted/open. Returns its row id for later outcome updates."""
    conn = get_conn()
    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        "INSERT INTO signals (symbol, direction, entry, sl, tp, posted_at, outcome) VALUES (?, ?, ?, ?, ?, ?, 'open')",
        (symbol, direction, entry, sl, tp, now),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def get_open_signals() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM signals WHERE outcome = 'open'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_signal(signal_id: int, outcome: str):
    """outcome should be 'win' or 'loss'."""
    conn = get_conn()
    conn.execute(
        "UPDATE signals SET outcome = ?, closed_at = ? WHERE id = ?",
        (outcome, datetime.utcnow().isoformat(), signal_id),
    )
    conn.commit()
    conn.close()


def get_performance_stats() -> dict:
    """Aggregate win/loss stats for the public performance dashboard."""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) as c FROM signals").fetchone()["c"]
    wins = conn.execute("SELECT COUNT(*) as c FROM signals WHERE outcome = 'win'").fetchone()["c"]
    losses = conn.execute("SELECT COUNT(*) as c FROM signals WHERE outcome = 'loss'").fetchone()["c"]
    open_count = conn.execute("SELECT COUNT(*) as c FROM signals WHERE outcome = 'open'").fetchone()["c"]
    closed = wins + losses
    win_rate = round((wins / closed) * 100, 1) if closed > 0 else None

    recent = conn.execute(
        "SELECT symbol, direction, entry, sl, tp, outcome, posted_at FROM signals ORDER BY posted_at DESC LIMIT 20"
    ).fetchall()
    conn.close()

    return {
        "total_signals": total,
        "wins": wins,
        "losses": losses,
        "open": open_count,
        "win_rate": win_rate,
        "recent": [dict(r) for r in recent],
    }


def get_revenue_today() -> dict:
    """Total revenue collected today (UTC), plus a breakdown by plan and payment count."""
    conn = get_conn()
    today_start = datetime.utcnow().strftime("%Y-%m-%d")

    rows = conn.execute(
        "SELECT plan, amount FROM subscribers WHERE created_at >= ? AND amount IS NOT NULL",
        (today_start,),
    ).fetchall()
    conn.close()

    total = sum(r["amount"] for r in rows)
    by_plan = {}
    for r in rows:
        by_plan[r["plan"]] = by_plan.get(r["plan"], 0) + r["amount"]

    return {
        "date": today_start,
        "total_revenue": round(total, 2),
        "payment_count": len(rows),
        "by_plan": {k: round(v, 2) for k, v in by_plan.items()},
    }


def get_revenue_summary(days: int = 7) -> list:
    """Daily revenue totals for the last N days - each as {date, total, count}."""
    conn = get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

    rows = conn.execute(
        "SELECT created_at, amount FROM subscribers WHERE created_at >= ? AND amount IS NOT NULL ORDER BY created_at",
        (cutoff,),
    ).fetchall()
    conn.close()

    by_day = {}
    for r in rows:
        day = r["created_at"][:10]  # YYYY-MM-DD
        if day not in by_day:
            by_day[day] = {"date": day, "total": 0.0, "count": 0}
        by_day[day]["total"] += r["amount"]
        by_day[day]["count"] += 1

    for day in by_day:
        by_day[day]["total"] = round(by_day[day]["total"], 2)

    return sorted(by_day.values(), key=lambda d: d["date"], reverse=True)



# --- Personal access links: /join/<token> -----------------------------------

def get_latest_subscription(contact: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM subscribers WHERE email = ? ORDER BY id DESC LIMIT 1", (contact,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_subscription_by_token(token: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM subscribers WHERE access_token = ? LIMIT 1", (token,)).fetchone()
    conn.close()
    return dict(row) if row else None


def claim_token(token: str) -> bool:
    """Mark the personal link as used. Only the FIRST caller gets True (guards against two simultaneous clicks)."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE subscribers SET redeemed_at = ? WHERE access_token = ? AND redeemed_at IS NULL",
        (datetime.utcnow().isoformat(), token),
    )
    conn.commit()
    ok = cur.rowcount == 1
    conn.close()
    return ok


def release_token(token: str):
    conn = get_conn()
    conn.execute("UPDATE subscribers SET redeemed_at = NULL WHERE access_token = ?", (token,))
    conn.commit()
    conn.close()


def store_invite_link(token: str, invite_link: str):
    conn = get_conn()
    conn.execute("UPDATE subscribers SET invite_link = ? WHERE access_token = ?", (invite_link, token))
    conn.commit()
    conn.close()


# --- People who asked to pay (shown on /admin/pending) ----------------------

def log_payment_request(contact: str, plan: str, ref_code, reference: str, amount: float):
    conn = get_conn()
    cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    dup = conn.execute(
        "SELECT 1 FROM payment_requests WHERE contact = ? AND plan = ? AND status = 'pending' AND created_at >= ? LIMIT 1",
        (contact, plan, cutoff),
    ).fetchone()
    if not dup:
        conn.execute(
            "INSERT INTO payment_requests (contact, plan, amount, reference, ref_code, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (contact, plan, amount, reference, ref_code, datetime.utcnow().isoformat()),
        )
        conn.commit()
    conn.close()


def get_pending_requests() -> list:
    conn = get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=3)).isoformat()
    rows = conn.execute(
        "SELECT * FROM payment_requests WHERE status = 'pending' AND created_at >= ? ORDER BY id DESC", (cutoff,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_requests_approved(contact: str):
    conn = get_conn()
    conn.execute("UPDATE payment_requests SET status = 'approved' WHERE contact = ? AND status = 'pending'", (contact,))
    conn.commit()
    conn.close()


# --- Card payments (Paystack) ------------------------------------------------

def create_card_payment(reference: str, contact: str, email: str, plan: str, amount: float, ref_code, flow: str = "link"):
    conn = get_conn()
    conn.execute(
        "INSERT INTO card_payments (reference, contact, email, plan, amount, ref_code, status, created_at, flow) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (reference, contact, email, plan, amount, ref_code, datetime.utcnow().isoformat(), flow),
    )
    conn.commit()
    conn.close()


def get_card_payment(reference: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM card_payments WHERE reference = ?", (reference,)).fetchone()
    conn.close()
    return dict(row) if row else None


def claim_card_payment(reference: str) -> bool:
    """Only the FIRST caller (return page or webhook) gets True, so a payment is never fulfilled twice."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE card_payments SET status = 'paid', paid_at = ? WHERE reference = ? AND status = 'pending'",
        (datetime.utcnow().isoformat(), reference),
    )
    conn.commit()
    ok = cur.rowcount == 1
    conn.close()
    return ok


def release_card_payment(reference: str):
    """Put a payment back to 'pending' if fulfilling it failed part-way, so it can be retried."""
    conn = get_conn()
    conn.execute("UPDATE card_payments SET status = 'pending', paid_at = NULL WHERE reference = ?", (reference,))
    conn.commit()
    conn.close()


def set_card_payment_token(reference: str, token: str):
    conn = get_conn()
    conn.execute("UPDATE card_payments SET access_token = ? WHERE reference = ?", (token, reference))
    conn.commit()
    conn.close()


def claim_web_session(reference: str) -> bool:
    """The automatic first sign-in after paying works ONCE per payment (so the return URL can't be shared as a login)."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE card_payments SET session_issued = 1 WHERE reference = ? AND COALESCE(session_issued, 0) = 0",
        (reference,),
    )
    conn.commit()
    ok = cur.rowcount == 1
    conn.close()
    return ok


# --- Subscriber website: sign-in links, sessions, dashboard data -------------

def _hash(raw: str) -> str:
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()


def member_exists(email: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM subscribers WHERE email = ? LIMIT 1", (email,)).fetchone()
    conn.close()
    return row is not None


def get_member_access(email: str) -> dict | None:
    """Everything the dashboard needs about one person (None if they've never paid)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(expires_at) AS expires_at, MAX(telegram_user_id) AS telegram_user_id, "
        "(SELECT plan FROM subscribers x WHERE x.email = ? ORDER BY x.id DESC LIMIT 1) AS plan, "
        "(SELECT access_token FROM subscribers x WHERE x.email = ? ORDER BY x.id DESC LIMIT 1) AS latest_token, "
        "(SELECT redeemed_at FROM subscribers x WHERE x.email = ? ORDER BY x.id DESC LIMIT 1) AS latest_redeemed "
        "FROM subscribers WHERE email = ?",
        (email, email, email, email),
    ).fetchone()
    conn.close()
    return dict(row) if row and row["expires_at"] else None


def create_login_token(email: str, minutes: int = 20) -> str:
    """One-time sign-in link token. Only a hash is stored."""
    raw = secrets.token_urlsafe(24)
    now = datetime.utcnow()
    conn = get_conn()
    conn.execute(
        "INSERT INTO login_tokens (token_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_hash(raw), email, now.isoformat(), (now + timedelta(minutes=minutes)).isoformat()),
    )
    conn.commit()
    conn.close()
    return raw


def login_token_valid(raw: str) -> bool:
    """Look without using it (the sign-in page asks for a tap first, so email scanners can't burn the link)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM login_tokens WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
        (_hash(raw), datetime.utcnow().isoformat()),
    ).fetchone()
    conn.close()
    return row is not None


def consume_login_token(raw: str) -> str | None:
    """Use the token. Returns the email, or None if it's unknown, expired or already used."""
    conn = get_conn()
    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        "UPDATE login_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
        (now, _hash(raw), now),
    )
    conn.commit()
    email = None
    if cur.rowcount == 1:
        email = conn.execute("SELECT email FROM login_tokens WHERE token_hash = ?", (_hash(raw),)).fetchone()["email"]
    conn.close()
    return email


def recent_login_token_count(email: str, minutes: int = 10) -> int:
    conn = get_conn()
    cutoff = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()
    n = conn.execute("SELECT COUNT(*) AS c FROM login_tokens WHERE email = ? AND created_at >= ?", (email, cutoff)).fetchone()["c"]
    conn.close()
    return n


def create_member_session(email: str, max_devices: int = 2) -> str:
    """Sign someone in. A login works on at most `max_devices` devices - a newer one signs the oldest out."""
    raw = secrets.token_urlsafe(24)
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute("INSERT INTO member_sessions (sid_hash, email, created_at, last_seen) VALUES (?, ?, ?, ?)",
                 (_hash(raw), email, now, now))
    conn.execute(
        "DELETE FROM member_sessions WHERE email = ? AND id NOT IN "
        "(SELECT id FROM member_sessions WHERE email = ? ORDER BY id DESC LIMIT ?)",
        (email, email, max_devices),
    )
    conn.commit()
    conn.close()
    return raw


def get_member_session(raw: str, max_age_days: int = 30) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT email, created_at FROM member_sessions WHERE sid_hash = ?", (_hash(raw),)).fetchone()
    if not row:
        conn.close()
        return None
    if datetime.fromisoformat(row["created_at"]) < datetime.utcnow() - timedelta(days=max_age_days):
        conn.execute("DELETE FROM member_sessions WHERE sid_hash = ?", (_hash(raw),))
        conn.commit()
        conn.close()
        return None
    conn.execute("UPDATE member_sessions SET last_seen = ? WHERE sid_hash = ?", (datetime.utcnow().isoformat(), _hash(raw)))
    conn.commit()
    conn.close()
    return row["email"]


def delete_member_session(raw: str):
    conn = get_conn()
    conn.execute("DELETE FROM member_sessions WHERE sid_hash = ?", (_hash(raw),))
    conn.commit()
    conn.close()


def get_dashboard_signals(open_limit: int = 10, closed_limit: int = 10) -> dict:
    conn = get_conn()
    open_rows = conn.execute(
        "SELECT * FROM signals WHERE outcome = 'open' ORDER BY posted_at DESC LIMIT ?", (open_limit,)).fetchall()
    closed_rows = conn.execute(
        "SELECT * FROM signals WHERE outcome IN ('win', 'loss') ORDER BY closed_at DESC LIMIT ?", (closed_limit,)).fetchall()
    conn.close()
    return {"open": [dict(r) for r in open_rows], "closed": [dict(r) for r in closed_rows]}
