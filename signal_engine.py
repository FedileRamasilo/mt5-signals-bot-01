"""
signal_engine.py
Rules-based strategy: EMA(9)/EMA(21) crossover confirmed by RSI(14),
with SL/TP computed from recent volatility (ATR-style range).

This is a starting point, not a proven money-making strategy - you should
backtest and tune it (or swap in your own rules) before relying on it.
"""

import pandas as pd
import numpy as np


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=21, adjust=False).mean()

    # RSI(14)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # Simple ATR-like range for SL/TP sizing
    df["tr"] = (df["high"] - df["low"]).abs()
    df["atr"] = df["tr"].rolling(14).mean()

    return df


def generate_signal(symbol_key: str, df: pd.DataFrame) -> dict | None:
    """
    Returns a signal dict if the latest closed candle triggers a setup, else None.
    A signal fires on an EMA crossover in the direction confirmed by RSI momentum.
    """
    df = add_indicators(df)
    if len(df) < 25:
        return None  # not enough data yet

    prev, last = df.iloc[-2], df.iloc[-1]

    crossed_up = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
    crossed_down = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]

    direction = None
    if crossed_up and last["rsi"] > 50:
        direction = "BUY"
    elif crossed_down and last["rsi"] < 50:
        direction = "SELL"

    if direction is None:
        return None

    entry = last["close"]
    atr = last["atr"] if not np.isnan(last["atr"]) else entry * 0.005

    if direction == "BUY":
        sl = entry - 1.5 * atr
        tp = entry + 2.5 * atr
    else:
        sl = entry + 1.5 * atr
        tp = entry - 2.5 * atr

    return {
        "symbol": symbol_key,
        "direction": direction,
        "entry": round(entry, 4),
        "sl": round(sl, 4),
        "tp": round(tp, 4),
        "rsi": round(last["rsi"], 1),
        "timestamp": str(last["datetime"]),
    }
