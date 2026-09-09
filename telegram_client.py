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
}


def format_signal_message(signal: dict) -> str:
    name = DISPLAY_NAMES.get(signal["symbol"], signal["symbol"])
    arrow = "ðŸŸ¢ BUY" if signal["direction"] == "BUY" else "ðŸ”´ SELL"
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
    resp.raise_for_status()
    return resp.json()


def format_free_signal_message(signal: dict, subscribe_link: str) -> str:
    """The free daily proof-signal - same quality as paid, with a subtle upsell."""
    name = DISPLAY_NAMES.get(signal["symbol"], signal["symbol"])
    arrow = "ðŸŸ¢ BUY" if signal["direction"] == "BUY" else "ðŸ”´ SELL"
    return (
        f"ðŸŽ *Today's Free Signal*\n\n"
        f"*{name}*\n"
        f"{arrow}\n\n"
        f"Entry: `{signal['entry']}`\n"
        f"Stop Loss: `{signal['sl']}`\n"
        f"Take Profit: `{signal['tp']}`\n\n"
        f"This is one of several signals we send paid subscribers daily across "
        f"BTC, USD/CHF, and Gold.\n"
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
    resp.raise_for_status()
    return resp.json()


def create_single_use_invite(expire_seconds: int = 86400) -> str:
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


def remove_subscriber(telegram_user_id: int):
    """Kick + immediately unban so an expired subscriber can rejoin after re-paying."""
    requests.post(f"{API_BASE}/banChatMember", json={"chat_id": CHANNEL_ID, "user_id": telegram_user_id}, timeout=15)
    requests.post(f"{API_BASE}/unbanChatMember", json={"chat_id": CHANNEL_ID, "user_id": telegram_user_id}, timeout=15)
