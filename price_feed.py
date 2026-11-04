"""
price_feed.py
Pulls recent 15-minute candles for the switched-on markets (see instruments.py) from Twelve Data.

Free plan: about 800 requests a day and 8 a minute. Each market costs one request per check, so this module
(1) pauses briefly between requests to stay under the per-minute limit,
(2) never lets ONE failing market block the others, and
(3) stops straight away if the daily allowance has run out, instead of wasting more requests.
"""

import os
import re
import time
import requests
import pandas as pd
from dotenv import load_dotenv

import instruments

load_dotenv()

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")
BASE_URL = "https://api.twelvedata.com/time_series"
PAUSE_BETWEEN_CALLS = float(os.getenv("PRICE_CALL_PAUSE", "1.0"))   # seconds

# Twelve Data symbol name for every market we know about
SYMBOLS = {key: info["api"] for key, info in instruments.ALL.items()}


def _clean(msg) -> str:
    """Never let the API key travel into logs or the admin page."""
    return re.sub(r"apikey=[^&\s'\"]+", "apikey=***", str(msg))


def fetch_candles(symbol_key: str, interval: str = "15min", outputsize: int = 100) -> pd.DataFrame:
    """Recent candles for one market, oldest -> newest (datetime, open, high, low, close)."""
    if symbol_key not in SYMBOLS:
        raise ValueError(f"Unknown symbol_key {symbol_key}. Use one of {list(SYMBOLS)}")
    params = {
        "symbol": SYMBOLS[symbol_key],
        "interval": interval,
        "outputsize": outputsize,
        "timezone": "UTC",          # so candle times are directly comparable with when a signal was sent
        "apikey": TWELVEDATA_API_KEY,
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error for {symbol_key}: {data}")

    df = pd.DataFrame(data["values"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


def fetch_all(interval: str = "15min", outputsize: int = 100, symbols=None):
    """Fetch every switched-on market. Returns (candles, errors): two dicts keyed by market.
    A market that fails goes into `errors`; the rest still come back."""
    keys = list(symbols) if symbols else instruments.enabled()
    candles, errors = {}, {}
    for i, key in enumerate(keys):
        if i:
            time.sleep(PAUSE_BETWEEN_CALLS)
        try:
            candles[key] = fetch_candles(key, interval, outputsize)
        except Exception as e:
            msg = _clean(e)[:250]
            errors[key] = msg
            if "credit" in msg.lower() or "429" in msg or "limit" in msg.lower():
                for rest in keys[i + 1:]:          # allowance used up: don't waste more requests
                    errors[rest] = msg
                break
    return candles, errors


if __name__ == "__main__":
    # Quick manual test: python price_feed.py   (needs TWELVEDATA_API_KEY)
    got, bad = fetch_all()
    for key, df in got.items():
        print(f"\n{key} - last 3 candles:")
        print(df.tail(3)[["datetime", "open", "high", "low", "close"]])
    for key, msg in bad.items():
        print(f"\n{key} FAILED: {msg}")
