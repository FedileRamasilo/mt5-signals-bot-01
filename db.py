"""
db.py
Minimal SQLite subscriber tracking: who paid, which plan, when it expires,
plus referral tracking (each subscriber gets a code; referring a paying
friend earns the referrer a free bonus day).
"""

import os
import sqlite3
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.getenv("DATABASE_PATH", "subscribers.db")


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
            referred_by_code TEXT          -- code of whoever referred them, if any
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
) -> datetime:
    conn = get_conn()
    now = datetime.utcnow()
    duration = timedelta(days=1) if plan == "daily" else timedelta(days=30)
    expires_at = now + duration

    own_code = get_or_create_referral_code(email)

    conn.execute(
        """
        INSERT INTO subscribers
            (email, telegram_user_id, plan, payfast_payment_id, expires_at, created_at,
             referral_code, referred_by_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (email, telegram_user_id, plan, payfast_payment_id, expires_at.isoformat(), now.isoformat(),
         own_code, referred_by_code),
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


def get_expired_subscribers() -> list:
    conn = get_conn()
    now = datetime.utcnow().isoformat()
    rows = conn.execute(
        "SELECT * FROM subscribers WHERE expires_at < ? AND telegram_user_id IS NOT NULL",
        (now,),
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

