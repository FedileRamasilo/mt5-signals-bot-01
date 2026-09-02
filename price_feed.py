"""
price_feed.py
Pulls recent OHLC candles for BTC/USD, USD/CHF, and XAU/USD (gold) from TwelveData.
Free tier: 800 requests/day, 8/minute - plenty for a scheduled signal bot checking
every 15-30 minutes across 3 symbols.
"""

import os
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")
BASE_URL = "https://api.twelvedata.com/time_series"

# TwelveData symbol names for our three instruments
SYMBOLS = {
    "BTC": "BTC/USD",
    "USDCHF": "USD/CHF",
    "GOLD": "XAU/USD",
}


def fetch_candles(symbol_key: str, interval: str = "15min", outputsize: int = 100) -> pd.DataFrame:
    """
    Fetch recent candles for one of our tracked instruments.
    symbol_key: one of "BTC", "USDCHF", "GOLD"
    interval: TwelveData interval string, e.g. "15min", "1h", "4h"
    Returns a DataFrame sorted oldest -> newest with columns:
    datetime, open, high, low, close, volume
    """
    if symbol_key not in SYMBOLS:
        raise ValueError(f"Unknown symbol_key {symbol_key}. Use one of {list(SYMBOLS)}")

    params = {
        "symbol": SYMBOLS[symbol_key],
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
    }
    resp = requests.get(BASE_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "values" not in data:
        raise RuntimeError(f"TwelveData error for {symbol_key}: {data}")

    df = pd.DataFrame(data["values"])
    df = df.rename(columns={"datetime": "datetime"})
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def fetch_all(interval: str = "15min", outputsize: int = 100) -> dict:
    """Fetch candles for all three tracked instruments in one call."""
    return {key: fetch_candles(key, interval, outputsize) for key in SYMBOLS}


if __name__ == "__main__":
    # Quick manual test - run `python price_feed.py` after setting TWELVEDATA_API_KEY
    for key, df in fetch_all().items():
        print(f"\n{key} — last 3 candles:")
        print(df.tail(3)[["datetime", "open", "high", "low", "close"]])
