"""
scheduler.py
Runs alongside app.py (or as a separate process/worker) to:
  1. Check for new trade signals every 15 minutes and post them to Telegram
  2. Remove subscribers whose access has expired

Run with: python scheduler.py
"""

import time
import logging
import os
import random
from datetime import date, datetime
from apscheduler.schedulers.blocking import BlockingScheduler

from price_feed import fetch_all, fetch_candles
from signal_engine import generate_signal
from telegram_client import post_signal, post_free_signal, remove_subscriber
from db import init_db, get_expired_subscribers, log_signal, get_open_signals, close_signal

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.StreamHandler(),                          # still prints to console/Railway logs
        logging.FileHandler("logs/scheduler.log"),         # also saves to a file
    ],
)
log = logging.getLogger(__name__)

PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN", "https://your-domain.example.com")
_last_free_signal_date = None


def check_signals():
    global _last_free_signal_date
    log.info("Checking for signals...")
    try:
        candles = fetch_all(interval="15min", outputsize=100)
    except Exception as e:
        log.error(f"Failed to fetch prices: {e}")
        return

    todays_signals = []

    for symbol_key, df in candles.items():
        try:
            signal = generate_signal(symbol_key, df)
            if signal:
                log.info(f"Signal found: {signal}")
                post_signal(signal)
                log_signal(
                    symbol=signal["symbol"],
                    direction=signal["direction"],
                    entry=signal["entry"],
                    sl=signal["sl"],
                    tp=signal["tp"],
                )
                todays_signals.append(signal)
            else:
                log.info(f"No signal for {symbol_key} this check.")
        except Exception as e:
            log.error(f"Error generating/posting signal for {symbol_key}: {e}")

    # Free daily proof-signal: post one real, triggered signal per day to the
    # public channel to build trust before people pay. Picks randomly among
    # today's triggered signals so it's not always the same instrument.
    today = date.today()
    if todays_signals and _last_free_signal_date != today:
        try:
            chosen = random.choice(todays_signals)
            subscribe_link = f"{PUBLIC_DOMAIN}/pay/daily?email=YOUR_EMAIL"
            post_free_signal(chosen, subscribe_link)
            _last_free_signal_date = today
            log.info(f"Posted free daily proof-signal: {chosen['symbol']}")
        except Exception as e:
            log.error(f"Failed to post free daily signal: {e}")


def prune_expired_subscribers():
    log.info("Checking for expired subscribers...")
    for sub in get_expired_subscribers():
        try:
            remove_subscriber(sub["telegram_user_id"])
            log.info(f"Removed expired subscriber: {sub['email']}")
        except Exception as e:
            log.error(f"Failed to remove subscriber {sub['email']}: {e}")


def check_open_signal_outcomes():
    """
    For every still-open signal, check the latest price and mark it a win if
    price has touched the take-profit level, or a loss if it's touched the
    stop-loss - this is what powers the public performance dashboard, so the
    win rate you show subscribers is always real, not cherry-picked.
    """
    open_signals = get_open_signals()
    if not open_signals:
        return

    log.info(f"Checking outcomes for {len(open_signals)} open signal(s)...")
    # Group by symbol so we only fetch each symbol's price once
    by_symbol = {}
    for s in open_signals:
        by_symbol.setdefault(s["symbol"], []).append(s)

    for symbol_key, signals_for_symbol in by_symbol.items():
        try:
            df = fetch_candles(symbol_key, interval="15min", outputsize=5)
            latest_high = df["high"].max()
            latest_low = df["low"].min()
        except Exception as e:
            log.error(f"Failed to fetch price for outcome check on {symbol_key}: {e}")
            continue

        for s in signals_for_symbol:
            direction = s["direction"]
            sl, tp = s["sl"], s["tp"]
            outcome = None

            if direction == "BUY":
                if latest_high >= tp:
                    outcome = "win"
                elif latest_low <= sl:
                    outcome = "loss"
            else:  # SELL
                if latest_low <= tp:
                    outcome = "win"
                elif latest_high >= sl:
                    outcome = "loss"

            if outcome:
                close_signal(s["id"], outcome)
                log.info(f"Signal #{s['id']} ({symbol_key} {direction}) closed as {outcome.upper()}")


if __name__ == "__main__":
    init_db()
    scheduler = BlockingScheduler()
    # next_run_time=datetime.now() makes the first check run immediately on
    # startup instead of waiting 15 minutes - important for testing, and
    # avoids the bug where passing next_run_time=None actually PAUSES the
    # job in APScheduler (it never runs at all, silently).
    scheduler.add_job(check_signals, "interval", minutes=15, next_run_time=datetime.now())
    scheduler.add_job(check_open_signal_outcomes, "interval", minutes=15, next_run_time=datetime.now())
    scheduler.add_job(prune_expired_subscribers, "interval", hours=1, next_run_time=datetime.now())
    log.info("Scheduler started. Checking signals every 15 minutes.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
