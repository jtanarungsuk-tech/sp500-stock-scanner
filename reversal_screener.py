#!/usr/bin/env python3
"""
S&P 500 reversal screener.

This tool does not claim a true statistical probability. It computes a
0-100 "Reversal Score" from price, momentum, volume, volatility, trend, and
relative-strength evidence, then prints stocks whose score is at least the
chosen threshold.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import json
import math
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO

import pandas as pd


USER_AGENT = {"User-Agent": "Mozilla/5.0"}
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d&includePrePost=false&events=history"


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    name: str
    sector: str


def yahoo_symbol(symbol: str) -> str:
    # Yahoo has long history for BNY Mellon under the old ticker BK.
    if symbol == "BNY":
        return "BK"
    return symbol.replace(".", "-")


def fetch_url(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def load_sp500() -> list[UniverseMember]:
    html = fetch_url(SP500_URL)
    table = pd.read_html(StringIO(html))[0]
    return [
        UniverseMember(row.Symbol, row.Security, row["GICS Sector"])
        for _, row in table[["Symbol", "Security", "GICS Sector"]].iterrows()
    ]


def load_chart(symbol: str, range_: str) -> pd.DataFrame:
    url = YAHOO_CHART.format(symbol=yahoo_symbol(symbol), range=range_)
    text = fetch_url(url)
    data = json.loads(text)
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(data.get("chart", {}).get("error") or "no chart result")

    quote = result[0]["indicators"]["quote"][0]
    rows = []
    timestamps = result[0].get("timestamp", [])
    for idx, timestamp in enumerate(timestamps):
        try:
            open_ = quote["open"][idx]
            high = quote["high"][idx]
            low = quote["low"][idx]
            close = quote["close"][idx]
            volume = quote["volume"][idx]
        except (IndexError, KeyError):
            continue
        if None in (open_, high, low, close, volume):
            continue
        rows.append(
            {
                "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
            }
        )
    if len(rows) < 240:
        raise RuntimeError(f"too few daily bars: {len(rows)}")
    return pd.DataFrame(rows).set_index("date")


def rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def adx_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def pct(value: float) -> float:
    return value * 100


def score_stock(member: UniverseMember, spy_return_3m: float, range_: str) -> dict[str, object]:
    df = load_chart(member.symbol, range_)
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    rsi14 = rsi_wilder(close)
    adx14 = adx_wilder(high, low, close)
    atr14 = atr_wilder(high, low, close)
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_signal
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_width = (4 * bb_std) / bb_mid

    latest = close.iloc[-1]
    previous = close.iloc[-2]
    high20_prev = high.shift(1).rolling(20).max().iloc[-1]
    high50_prev = high.shift(1).rolling(50).max().iloc[-1]
    low20_prev = low.shift(1).rolling(20).min().iloc[-1]
    vol20 = volume.rolling(20).mean().iloc[-1]
    ret_3m = close.iloc[-1] / close.iloc[-64] - 1
    ret_1m = close.iloc[-1] / close.iloc[-22] - 1
    drawdown_3m = close.iloc[-64:].max()
    drawdown_3m = close.iloc[-1] / drawdown_3m - 1

    volume_ratio = volume.iloc[-1] / vol20 if vol20 else 0
    distance_ema20 = latest / ema20.iloc[-1] - 1
    distance_high20 = latest / high20_prev - 1
    atr_pct = atr14.iloc[-1] / latest

    signals: list[str] = []
    score = 0

    # Trend base: reversal setups should have recovered important moving averages.
    if latest > ema200.iloc[-1]:
        score += 12
        signals.append("price>EMA200")
    if ema50.iloc[-1] > ema200.iloc[-1] or ema50.iloc[-1] > ema50.iloc[-21]:
        score += 10
        signals.append("intermediate trend improving")
    if latest > ema20.iloc[-1] > ema50.iloc[-1]:
        score += 12
        signals.append("price>EMA20>EMA50")

    # Breakout from base or pullback.
    if latest > high20_prev:
        score += 16
        signals.append("20d breakout")
    elif latest > high50_prev:
        score += 20
        signals.append("50d breakout")
    elif latest > previous and latest > ema20.iloc[-1] and close.iloc[-2] <= ema20.iloc[-2]:
        score += 10
        signals.append("reclaimed EMA20")

    # Momentum returning without being overbought.
    if 50 <= rsi14.iloc[-1] <= 70:
        score += 14
        signals.append("RSI 50-70")
    elif 45 <= rsi14.iloc[-1] < 50:
        score += 8
        signals.append("RSI recovering")
    if macd_hist.iloc[-1] > 0 and macd_hist.iloc[-1] > macd_hist.iloc[-4]:
        score += 10
        signals.append("MACD histogram rising")

    # Trend strength and participation.
    if adx14.iloc[-1] > 20:
        score += 8
        signals.append("ADX>20")
    if volume_ratio >= 1.5:
        score += 10
        signals.append("volume expansion")
    elif volume_ratio >= 1.2:
        score += 6
        signals.append("volume above average")

    # Relative strength vs market.
    if ret_3m > spy_return_3m:
        score += 12
        signals.append("RS 3M > SPY")
    elif ret_1m > 0:
        score += 5
        signals.append("positive 1M return")

    # Base contraction: volatility recently calmer than the medium-term regime.
    if bb_width.iloc[-1] < bb_width.iloc[-50:-20].mean():
        score += 6
        signals.append("volatility contraction")

    # Penalize extended moves. We want emerging reversals, not exhausted moves.
    if distance_ema20 > 0.12:
        score -= 12
        signals.append("extended from EMA20")
    if rsi14.iloc[-1] > 75:
        score -= 12
        signals.append("RSI hot")
    if latest <= low20_prev:
        score -= 12
        signals.append("near 20d low")

    return {
        "symbol": member.symbol,
        "name": member.name,
        "sector": member.sector,
        "date": close.index[-1],
        "score": max(0, min(100, round(score))),
        "close": round(latest, 2),
        "rsi14": round(rsi14.iloc[-1], 1),
        "adx14": round(adx14.iloc[-1], 1),
        "ret_3m_pct": round(pct(ret_3m), 1),
        "spy_ret_3m_pct": round(pct(spy_return_3m), 1),
        "volume_ratio": round(volume_ratio, 2),
        "atr_pct": round(pct(atr_pct), 2),
        "distance_ema20_pct": round(pct(distance_ema20), 1),
        "distance_high20_pct": round(pct(distance_high20), 1),
        "drawdown_3m_pct": round(pct(drawdown_3m), 1),
        "signals": "; ".join(signals),
    }


def fetch_with_retry(fn, attempts: int = 4):
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(0.4 * (attempt + 1))
    raise last_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find S&P 500 reversal candidates by score.")
    parser.add_argument("--min-score", type=int, default=80, help="Minimum Reversal Score to print.")
    parser.add_argument("--range", default="3y", help="Yahoo chart range, e.g. 1y, 2y, 3y.")
    parser.add_argument("--workers", type=int, default=12, help="Parallel download workers.")
    parser.add_argument("--limit", type=int, default=0, help="Limit output rows after sorting; 0 means no limit.")
    parser.add_argument("--csv", dest="csv_path", help="Optional path to write the full scored table as CSV.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    members = load_sp500()
    spy = fetch_with_retry(lambda: load_chart("SPY", args.range))
    spy_return_3m = spy["close"].iloc[-1] / spy["close"].iloc[-64] - 1

    rows: list[dict[str, object]] = []
    errors: list[tuple[str, str]] = []

    def run(member: UniverseMember) -> dict[str, object]:
        return fetch_with_retry(lambda: score_stock(member, spy_return_3m, args.range))

    with futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(run, member): member for member in members}
        for future in futures.as_completed(future_map):
            member = future_map[future]
            try:
                rows.append(future.result())
            except Exception as error:  # noqa: BLE001
                errors.append((member.symbol, str(error)))

    rows.sort(key=lambda row: (-int(row["score"]), str(row["symbol"])))
    passing = [row for row in rows if int(row["score"]) >= args.min_score]
    shown = passing[: args.limit] if args.limit else passing

    if args.csv_path:
        fieldnames = [
            "symbol",
            "name",
            "sector",
            "date",
            "score",
            "close",
            "rsi14",
            "adx14",
            "ret_3m_pct",
            "spy_ret_3m_pct",
            "volume_ratio",
            "atr_pct",
            "distance_ema20_pct",
            "distance_high20_pct",
            "drawdown_3m_pct",
            "signals",
        ]
        with open(args.csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    print(f"Universe: {len(members)} S&P 500 listings")
    print(f"SPY 3M return: {pct(spy_return_3m):.2f}%")
    print(f"Min score: {args.min_score}")
    print(f"Passing: {len(passing)}")
    print(f"Errors: {len(errors)}")
    if errors:
        print("Error sample:", "; ".join(f"{symbol}: {message}" for symbol, message in errors[:5]))
    print()
    print("symbol,score,close,rsi14,adx14,ret_3m_pct,volume_ratio,signals")
    for row in shown:
        print(
            f"{row['symbol']},{row['score']},{row['close']},{row['rsi14']},"
            f"{row['adx14']},{row['ret_3m_pct']},{row['volume_ratio']},{row['signals']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
