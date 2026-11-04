"""
instruments.py
The markets the bot can cover, and which of them are switched on.

Switch markets on/off with the INSTRUMENTS variable in Railway (a comma-separated list of the keys below), e.g.
    INSTRUMENTS=BTC,ETH,GOLD,XAG,EURUSD,USDCHF,USDZAR
Every market uses about 96 data requests a day (one every 15 minutes). The free Twelve Data plan allows
about 800 a day, so 7 markets fit comfortably and 9 do not - add more only after upgrading the data plan.
"""

import os
import logging

ALL = {
    "BTC":    {"api": "BTC/USD", "name": "Bitcoin (BTC/USD)",  "short": "Bitcoin"},
    "ETH":    {"api": "ETH/USD", "name": "Ethereum (ETH/USD)", "short": "Ethereum"},
    "GOLD":   {"api": "XAU/USD", "name": "Gold (XAU/USD)",     "short": "Gold"},
    "XAG":    {"api": "XAG/USD", "name": "Silver (XAG/USD)",   "short": "Silver"},
    "EURUSD": {"api": "EUR/USD", "name": "EUR/USD",            "short": "EUR/USD"},
    "GBPUSD": {"api": "GBP/USD", "name": "GBP/USD",            "short": "GBP/USD"},
    "USDJPY": {"api": "USD/JPY", "name": "USD/JPY",            "short": "USD/JPY"},
    "USDCHF": {"api": "USD/CHF", "name": "USD/CHF",            "short": "USD/CHF"},
    "USDZAR": {"api": "USD/ZAR", "name": "USD/ZAR",            "short": "USD/ZAR"},
}

DEFAULT_ENABLED = "BTC,ETH,GOLD,XAG,EURUSD,USDCHF,USDZAR"
CHECKS_PER_DAY = 96          # one check every 15 minutes


def enabled() -> list:
    """The switched-on markets, in order. Unknown names are ignored (with a warning)."""
    raw = os.getenv("INSTRUMENTS", "").strip() or DEFAULT_ENABLED
    keys = []
    for k in (x.strip().upper() for x in raw.split(",")):
        if not k or k in keys:
            continue
        if k in ALL:
            keys.append(k)
        else:
            logging.warning(f"INSTRUMENTS contains an unknown market '{k}' - ignored. Known: {', '.join(ALL)}")
    return keys or DEFAULT_ENABLED.split(",")


def market_list_text() -> str:
    """'Bitcoin, Ethereum, Gold and USD/ZAR' - used in customer-facing text so it always matches what is really covered."""
    names = [ALL[k]["short"] for k in enabled()]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def daily_requests() -> int:
    return len(enabled()) * CHECKS_PER_DAY
