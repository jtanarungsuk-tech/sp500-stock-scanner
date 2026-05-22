#!/usr/bin/env python3
"""Early-trend S&P 500 screener for stocks setting up near breakout."""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import json
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
    data = json.loads(fetch_url(url))
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(data.get("chart", {}).get("error") or "no chart result")

    quote = result[0]["indicators"]["quote"][0]
    rows = []
    for idx, timestamp in enumerate(result[0].get("timestamp", [])):
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
    if len(rows) < 260:
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


def atr_wilder(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
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


def fetch_with_retry(fn, attempts: int = 4):
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(0.4 * (attempt + 1))
    raise last_error


def screen_stock(member: UniverseMember, spy_5d_return: float, spy_10d_return: float, range_: str) -> dict[str, object]:
    df = load_chart(member.symbol, range_)
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema200 = close.ewm(span=200, adjust=False).mean()
    rsi14 = rsi_wilder(close)
    atr10 = atr_wilder(high, low, close, 10)
    atr50 = atr_wilder(high, low, close, 50)

    latest = close.iloc[-1]
    high20_prev = high.shift(1).rolling(20).max().iloc[-1]
    volume_avg20 = volume.rolling(20).mean().iloc[-1]
    ret_5d = close.iloc[-1] / close.iloc[-6] - 1
    ret_10d = close.iloc[-1] / close.iloc[-11] - 1
    atr10_pct = atr10 / close
    atr50_pct = atr50 / close

    trend_ok = ema50.iloc[-1] > ema200.iloc[-1] or ema50.iloc[-1] > ema50.iloc[-11]
    atr_contracting_now = atr10_pct.iloc[-1] < atr50_pct.iloc[-1]
    atr_contracting_recent = (atr10_pct.iloc[-10:] < atr50_pct.iloc[-10:]).any()
    rs_ok = ret_5d > spy_5d_return or ret_10d > spy_10d_return

    checks = {
        "price_gt_ema50": latest > ema50.iloc[-1],
        "price_gt_ema200": latest > ema200.iloc[-1],
        "ema50_gt_ema200_or_slope_up": trend_ok,
        "rsi14_50_to_70": 50 <= rsi14.iloc[-1] <= 70,
        "price_lte_ema20_x_1_08": latest <= ema20.iloc[-1] * 1.08,
        "close_gte_97pct_20d_high": latest >= high20_prev * 0.97,
        "atr10_lt_atr50_now_or_recent": atr_contracting_now or atr_contracting_recent,
        "volume_gte_avg20": volume.iloc[-1] >= volume_avg20,
        "rs_5d_or_10d_gt_spy": rs_ok,
    }

    return {
        "symbol": member.symbol,
        "name": member.name,
        "sector": member.sector,
        "date": close.index[-1],
        "pass": all(checks.values()),
        "close": round(latest, 2),
        "ema20": round(ema20.iloc[-1], 2),
        "ema50": round(ema50.iloc[-1], 2),
        "ema200": round(ema200.iloc[-1], 2),
        "ema50_slope_10d_pct": round(pct(ema50.iloc[-1] / ema50.iloc[-11] - 1), 2),
        "high20_prev": round(high20_prev, 2),
        "distance_to_20d_high_pct": round(pct(latest / high20_prev - 1), 2),
        "volume": int(volume.iloc[-1]),
        "volume_avg20": int(volume_avg20),
        "volume_ratio": round(volume.iloc[-1] / volume_avg20, 2),
        "atr10_pct": round(pct(atr10_pct.iloc[-1]), 2),
        "atr50_pct": round(pct(atr50_pct.iloc[-1]), 2),
        "atr_contracting_days_last10": int((atr10_pct.iloc[-10:] < atr50_pct.iloc[-10:]).sum()),
        "rsi14": round(rsi14.iloc[-1], 1),
        "ret_5d_pct": round(pct(ret_5d), 2),
        "spy_ret_5d_pct": round(pct(spy_5d_return), 2),
        "ret_10d_pct": round(pct(ret_10d), 2),
        "spy_ret_10d_pct": round(pct(spy_10d_return), 2),
        "distance_ema20_pct": round(pct(latest / ema20.iloc[-1] - 1), 2),
        **checks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find S&P 500 early-trend setup candidates.")
    parser.add_argument("--range", default="3y", help="Yahoo chart range, e.g. 1y, 2y, 3y.")
    parser.add_argument("--workers", type=int, default=12, help="Parallel download workers.")
    parser.add_argument("--limit", type=int, default=0, help="Limit output rows; 0 means no limit.")
    parser.add_argument("--csv", dest="csv_path", help="Optional path to write all rows as CSV.")
    parser.add_argument("--passing-csv", dest="passing_csv_path", help="Optional path to write passing rows as CSV.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    members = load_sp500()
    spy = fetch_with_retry(lambda: load_chart("SPY", args.range))
    spy_5d_return = spy["close"].iloc[-1] / spy["close"].iloc[-6] - 1
    spy_10d_return = spy["close"].iloc[-1] / spy["close"].iloc[-11] - 1

    rows: list[dict[str, object]] = []
    errors: list[tuple[str, str]] = []

    def run(member: UniverseMember) -> dict[str, object]:
        return fetch_with_retry(lambda: screen_stock(member, spy_5d_return, spy_10d_return, args.range))

    with futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {executor.submit(run, member): member for member in members}
        for future in futures.as_completed(future_map):
            member = future_map[future]
            try:
                rows.append(future.result())
            except Exception as error:  # noqa: BLE001
                errors.append((member.symbol, str(error)))

    fieldnames = [
        "symbol",
        "name",
        "sector",
        "date",
        "pass",
        "close",
        "ema20",
        "ema50",
        "ema200",
        "ema50_slope_10d_pct",
        "high20_prev",
        "distance_to_20d_high_pct",
        "volume",
        "volume_avg20",
        "volume_ratio",
        "atr10_pct",
        "atr50_pct",
        "atr_contracting_days_last10",
        "rsi14",
        "ret_5d_pct",
        "spy_ret_5d_pct",
        "ret_10d_pct",
        "spy_ret_10d_pct",
        "distance_ema20_pct",
        "price_gt_ema50",
        "price_gt_ema200",
        "ema50_gt_ema200_or_slope_up",
        "rsi14_50_to_70",
        "price_lte_ema20_x_1_08",
        "close_gte_97pct_20d_high",
        "atr10_lt_atr50_now_or_recent",
        "volume_gte_avg20",
        "rs_5d_or_10d_gt_spy",
    ]

    rows.sort(key=lambda row: (-float(row["ret_10d_pct"]), str(row["symbol"])))
    passing = [row for row in rows if row["pass"]]
    shown = passing[: args.limit] if args.limit else passing

    if args.csv_path:
        with open(args.csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    if args.passing_csv_path:
        with open(args.passing_csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(passing)

    print(f"Universe: {len(members)} S&P 500 listings")
    print(f"Data date: {spy.index[-1]}")
    print(f"SPY 5D return: {pct(spy_5d_return):.2f}%")
    print(f"SPY 10D return: {pct(spy_10d_return):.2f}%")
    print(f"Passing: {len(passing)}")
    print(f"Errors: {len(errors)}")
    if errors:
        print("Error sample:", "; ".join(f"{symbol}: {message}" for symbol, message in errors[:5]))
    print()
    print("symbol,name,sector,close,volume_ratio,rsi14,atr10_pct,atr50_pct,ret_5d_pct,ret_10d_pct,distance_to_20d_high_pct,distance_ema20_pct")
    for row in shown:
        print(
            f"{row['symbol']},{row['name']},{row['sector']},{row['close']},"
            f"{row['volume_ratio']},{row['rsi14']},{row['atr10_pct']},"
            f"{row['atr50_pct']},{row['ret_5d_pct']},{row['ret_10d_pct']},"
            f"{row['distance_to_20d_high_pct']},{row['distance_ema20_pct']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
