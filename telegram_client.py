"""
telegram_client.py
Handles posting signals to the private channel, and inviting / removing
subscribers based on payment status.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_SIGNAL_CHANNEL_ID")
FREE_CHANNEL_ID = os.getenv("TELEGRAM_FREE_CHANNEL_ID")  # public channel for the daily proof signal
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

DISPLAY_NAMES = {
    "BTC": "Bitcoin (BTC/USD)",
    "USDCHF": "USD/CHF",
    "GOLD": "Gold (XAU/USD)",
    "USDZAR": "USD/ZAR",
}


def format_signal_message(signal: dict) -> str:
    name = DISPLAY_NAMES.get(signal["symbol"], signal["symbol"])
    arrow = "\U0001F7E2 BUY" if signal["direction"] == "BUY" else "\U0001F534 SELL"
    return (
        f"*{name}*\n"
        f"{arrow}\n\n"
        f"Entry: `{signal['entry']}`\n"
        f"Stop Loss: `{signal['sl']}`\n"
        f"Take Profit: `{signal['tp']}`\n"
        f"RSI: {signal['rsi']}\n\n"
        f"_Not financial advice. Trade your own risk management._"
    )


def post_signal(signal: dict, channel_id: str | None = None):
    """Posts to the private paid channel by default, or to a specific channel_id if given."""
    text = format_signal_message(signal)
    resp = requests.post(
        f"{API_BASE}/sendMessage",
        json={"chat_id": channel_id or CHANNEL_ID, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    if not resp.ok:
        try:
            detail = resp.json().get("description", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Telegram sendMessage failed (status {resp.status_code}): {detail}\nMessage text was:\n{text}")
    return resp.json()


def format_free_signal_message(signal: dict, subscribe_link: str) -> str:
    """The free daily proof-signal - same quality as paid, with a subtle upsell."""
    name = DISPLAY_NAMES.get(signal["symbol"], signal["symbol"])
    arrow = "\U0001F7E2 BUY" if signal["direction"] == "BUY" else "\U0001F534 SELL"
    return (
        f"\U0001F381 *Today's Free Signal*\n\n"
        f"*{name}*\n"
        f"{arrow}\n\n"
        f"Entry: `{signal['entry']}`\n"
        f"Stop Loss: `{signal['sl']}`\n"
        f"Take Profit: `{signal['tp']}`\n\n"
        f"This is one of several signals we send paid subscribers daily across "
        f"BTC, USD/CHF, Gold, and USD/ZAR.\n"
        f"Subscribe for the full daily feed: {subscribe_link}\n\n"
        f"_Not financial advice. Trade your own risk management._"
    )


def post_free_signal(signal: dict, subscribe_link: str):
    if not FREE_CHANNEL_ID:
        raise RuntimeError("TELEGRAM_FREE_CHANNEL_ID is not set in .env")
    text = format_free_signal_message(signal, subscribe_link)
    resp = requests.post(
        f"{API_BASE}/sendMessage",
        json={"chat_id": FREE_CHANNEL_ID, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    if not resp.ok:
        try:
            detail = resp.json().get("description", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Telegram sendMessage (free channel) failed (status {resp.status_code}): {detail}\nMessage text was:\n{text}")
    return resp.json()


def create_single_use_invite(expire_seconds: int = 43200) -> str:
    """Create a one-time invite link for a newly paid subscriber. Bot must be channel admin."""
    expire_timestamp = int(time.time()) + expire_seconds  # Telegram wants a Unix timestamp, not an offset
    resp = requests.post(
        f"{API_BASE}/createChatInviteLink",
        json={"chat_id": CHANNEL_ID, "member_limit": 1, "expire_date": expire_timestamp},
        timeout=15,
    )
    if not resp.ok:
        # Surface Telegram's actual error description (e.g. "chat not found",
        # "CHAT_ADMIN_REQUIRED") instead of a generic 400 with no explanation.
        try:
            detail = resp.json().get("description", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(
            f"Telegram createChatInviteLink failed (status {resp.status_code}): {detail} "
            f"[chat_id used: {CHANNEL_ID!r}]"
        )
    return resp.json()["result"]["invite_link"]


def remove_subscriber(telegram_user_id: int) -> bool:
    """Kick + immediately unban so an expired subscriber can rejoin after re-paying.
    Returns True only if Telegram confirmed the removal."""
    ban = requests.post(f"{API_BASE}/banChatMember", json={"chat_id": CHANNEL_ID, "user_id": telegram_user_id}, timeout=15)
    if not ban.ok:
        return False
    requests.post(f"{API_BASE}/unbanChatMember", json={"chat_id": CHANNEL_ID, "user_id": telegram_user_id, "only_if_banned": True}, timeout=15)
    return True


# --- Join tracking: learn WHO joined with WHICH one-time link ---------------
_update_offset = None


def parse_member_joins(updates: list, channel_id) -> list:
    """Pure function: pick out 'someone joined our channel' events from Telegram updates."""
    joins = []
    for upd in updates:
        cm = upd.get("chat_member")
        if not cm:
            continue
        if str(cm.get("chat", {}).get("id")) != str(channel_id):
            continue
        new = cm.get("new_chat_member", {})
        old = cm.get("old_chat_member", {})
        if new.get("status") != "member" or old.get("status") not in ("left", "kicked", None):
            continue
        user = new.get("user", {})
        if user.get("is_bot"):
            continue
        link = (cm.get("invite_link") or {}).get("invite_link")
        joins.append({"user_id": user.get("id"), "invite_link": link})
    return joins


def fetch_member_joins() -> list:
    """Poll Telegram for new join events (needs the bot to be a channel admin)."""
    global _update_offset
    body = {"timeout": 0, "allowed_updates": ["chat_member"]}
    if _update_offset is not None:
        body["offset"] = _update_offset
    resp = requests.post(f"{API_BASE}/getUpdates", json=body, timeout=20)
    if not resp.ok:
        try:
            detail = resp.json().get("description", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"Telegram getUpdates failed (status {resp.status_code}): {detail}")
    updates = resp.json().get("result", [])
    if updates:
        _update_offset = max(u["update_id"] for u in updates) + 1
    return parse_member_joins(updates, CHANNEL_ID)
