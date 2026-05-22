#!/usr/bin/env python3
"""Rank S&P 500 sector rotation using sector ETFs and stock breadth."""

from __future__ import annotations

import argparse
import csv
import json
import urllib.request
from datetime import datetime, timezone

import pandas as pd


USER_AGENT = {"User-Agent": "Mozilla/5.0"}
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d&includePrePost=false&events=history"

SECTOR_ETFS = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Information Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


def fetch_url(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def load_chart(symbol: str, range_: str) -> pd.DataFrame:
    data = json.loads(fetch_url(YAHOO_CHART.format(symbol=symbol, range=range_)))
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
    if len(rows) < 80:
        raise RuntimeError(f"too few daily bars: {len(rows)}")
    return pd.DataFrame(rows).set_index("date")


def pct(value: float) -> float:
    return value * 100


def percentile_scores(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: item[1])
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    return {sector: rank / (len(ordered) - 1) for rank, (sector, _) in enumerate(ordered)}


def sector_trend(symbol: str, spy_close: pd.Series, range_: str) -> dict[str, object]:
    df = load_chart(symbol, range_)
    close = df["close"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    ret_5d = close.iloc[-1] / close.iloc[-6] - 1
    ret_10d = close.iloc[-1] / close.iloc[-11] - 1
    ret_20d = close.iloc[-1] / close.iloc[-21] - 1
    spy_ret_5d = spy_close.iloc[-1] / spy_close.iloc[-6] - 1
    spy_ret_10d = spy_close.iloc[-1] / spy_close.iloc[-11] - 1
    spy_ret_20d = spy_close.iloc[-1] / spy_close.iloc[-21] - 1

    return {
        "date": close.index[-1],
        "close": round(close.iloc[-1], 2),
        "ret_5d_pct": round(pct(ret_5d), 2),
        "ret_10d_pct": round(pct(ret_10d), 2),
        "ret_20d_pct": round(pct(ret_20d), 2),
        "rs_5d_vs_spy_pct": round(pct(ret_5d - spy_ret_5d), 2),
        "rs_10d_vs_spy_pct": round(pct(ret_10d - spy_ret_10d), 2),
        "rs_20d_vs_spy_pct": round(pct(ret_20d - spy_ret_20d), 2),
        "price_gt_ema20": close.iloc[-1] > ema20.iloc[-1],
        "price_gt_ema50": close.iloc[-1] > ema50.iloc[-1],
        "ema20_gt_ema50_or_slope_up": ema20.iloc[-1] > ema50.iloc[-1] or ema20.iloc[-1] > ema20.iloc[-11],
    }


def breadth_rows(stock_csv: str) -> dict[str, dict[str, object]]:
    df = pd.read_csv(stock_csv)
    rows = {}
    for sector, group in df.groupby("sector"):
        count = len(group)
        rows[sector] = {
            "stock_count": count,
            "pass_count": int(group["pass"].sum()),
            "score_80_count": int((group["setup_score"] >= 80).sum()),
            "avg_setup_score": round(float(group["setup_score"].mean()), 1),
            "pct_setup_score_80": round(100 * float((group["setup_score"] >= 80).mean()), 1),
            "pct_price_gt_ema50": round(100 * float(group["price_gt_ema50"].mean()), 1),
            "pct_rs_ok": round(100 * float(group["rs10_gt_spy_or_rs5_gt_spy_and_rs20_improving"].mean()), 1),
        }
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank current S&P 500 sector rotation.")
    parser.add_argument("--stock-csv", required=True, help="CSV from sp500_early_trend.py --csv.")
    parser.add_argument("--range", default="1y", help="Yahoo chart range for sector ETFs.")
    parser.add_argument("--csv", dest="csv_path", help="Optional path to write sector ranking CSV.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spy = load_chart("SPY", args.range)
    spy_close = spy["close"]
    breadth = breadth_rows(args.stock_csv)

    rows: list[dict[str, object]] = []
    etf_data = {}
    for sector, etf in SECTOR_ETFS.items():
        trend = sector_trend(etf, spy_close, args.range)
        etf_data[sector] = trend
        b = breadth.get(sector, {})
        rows.append({"sector": sector, "etf": etf, **trend, **b})

    rs20_rank = percentile_scores({row["sector"]: float(row["rs_20d_vs_spy_pct"]) for row in rows})
    rs10_rank = percentile_scores({row["sector"]: float(row["rs_10d_vs_spy_pct"]) for row in rows})
    for row in rows:
        sector = str(row["sector"])
        row["sector_rotation_score"] = round(
            40 * rs20_rank[sector]
            + 25 * rs10_rank[sector]
            + 20 * (float(row.get("pct_setup_score_80", 0)) / 100)
            + 15 * (float(row.get("pct_price_gt_ema50", 0)) / 100),
            1,
        )

    rows.sort(key=lambda row: (-float(row["sector_rotation_score"]), -float(row["rs_20d_vs_spy_pct"])))

    fieldnames = [
        "sector",
        "etf",
        "sector_rotation_score",
        "date",
        "close",
        "ret_5d_pct",
        "ret_10d_pct",
        "ret_20d_pct",
        "rs_5d_vs_spy_pct",
        "rs_10d_vs_spy_pct",
        "rs_20d_vs_spy_pct",
        "price_gt_ema20",
        "price_gt_ema50",
        "ema20_gt_ema50_or_slope_up",
        "stock_count",
        "pass_count",
        "score_80_count",
        "avg_setup_score",
        "pct_setup_score_80",
        "pct_price_gt_ema50",
        "pct_rs_ok",
    ]

    if args.csv_path:
        with open(args.csv_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    print(f"Data date: {spy.index[-1]}")
    print("sector,etf,score,rs_5d_vs_spy,rs_10d_vs_spy,rs_20d_vs_spy,pass_count,score80_pct,price_gt_ema50_pct,avg_setup_score")
    for row in rows:
        print(
            f"{row['sector']},{row['etf']},{row['sector_rotation_score']},"
            f"{row['rs_5d_vs_spy_pct']},{row['rs_10d_vs_spy_pct']},{row['rs_20d_vs_spy_pct']},"
            f"{row.get('pass_count', 0)},{row.get('pct_setup_score_80', 0)},"
            f"{row.get('pct_price_gt_ema50', 0)},{row.get('avg_setup_score', 0)}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
