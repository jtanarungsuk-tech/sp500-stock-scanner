#!/usr/bin/env python3
"""Screen Thai stocks for early-trend technical setups."""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import json
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from io import StringIO

import pandas as pd
from pypdf import PdfReader


USER_AGENT = {"User-Agent": "Mozilla/5.0"}
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d&includePrePost=false&events=history"
SET50_WIKI_URL = "https://en.wikipedia.org/wiki/SET50_Index"
SET100_WIKI_URL = "https://en.wikipedia.org/wiki/SET100_Index"
SET_CONSTITUENTS_PAGE_URL = "https://www.set.or.th/th/market/information/securities-list/constituents-list-set50-set100"
DEFAULT_BENCHMARK = "^SET.BK"


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    name: str
    sector: str


def fetch_url(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def fetch_bytes(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def yahoo_symbol(symbol: str) -> str:
    raw = symbol.strip().upper()
    if raw.startswith("^"):
        return raw
    if raw.endswith(".BK"):
        return raw
    return f"{raw.replace('.', '-')}.BK"


def load_wiki_universe(url: str) -> list[UniverseMember]:
    html = fetch_url(url)
    table = pd.read_html(StringIO(html))[0]
    columns = ["Symbol", "Securities Name", "Sector"]
    missing = [col for col in columns if col not in table.columns]
    if missing:
        raise RuntimeError(f"SET50 table missing columns: {missing}")
    members = []
    for _, row in table[columns].iterrows():
        symbol = str(row["Symbol"]).strip()
        if not symbol or symbol == "nan":
            continue
        members.append(
            UniverseMember(
                symbol=symbol,
                name=str(row["Securities Name"]).strip(),
                sector=str(row["Sector"]).strip(),
            )
        )
    if not members:
        raise RuntimeError("Universe from source is empty")
    return members


def load_set50_universe() -> list[UniverseMember]:
    return load_wiki_universe(SET50_WIKI_URL)


def load_set100_universe() -> list[UniverseMember]:
    try:
        return load_set100_universe_from_set_page()
    except Exception:
        return load_wiki_universe(SET100_WIKI_URL)


def load_set100_universe_from_set_page() -> list[UniverseMember]:
    html = fetch_url(SET_CONSTITUENTS_PAGE_URL)
    pdf_urls = sorted(
        set(
            re.findall(
                r"https://media\.set\.or\.th/set/Documents/[^\"]*SET50[^\"]*100[^\"]*\.pdf",
                html,
            )
        )
    )
    if not pdf_urls:
        raise RuntimeError("No SET50/SET100 constituent PDF link found on SET page")
    latest_pdf_url = pdf_urls[-1]
    pdf_bytes = fetch_bytes(latest_pdf_url, timeout=35)

    sectors = [
        "Information & Communication Technology",
        "Transportation & Logistics",
        "Property Development",
        "Energy & Utilities",
        "Health Care Services",
        "Finance & Securities",
        "Electronic Components",
        "Tourism & Leisure",
        "Construction Materials",
        "Petrochemicals & Chemicals",
        "Personal Products & Pharmaceuticals",
        "Professional Services",
        "Construction Services",
        "Food & Beverage",
        "Media & Publishing",
        "Fashion",
        "Agribusiness",
        "Banking",
        "Insurance",
        "Commerce",
        "Packaging",
    ]
    sectors = sorted(sectors, key=len, reverse=True)

    reader = PdfReader(BytesIO(pdf_bytes))
    candidates: list[tuple[int, str, str, str]] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        for raw_line in text.splitlines():
            line = " ".join(raw_line.strip().split())
            if not line:
                continue
            m = re.match(r"^(\d{1,3})\s+([A-Z0-9\.-]{1,12})\s+(.+)$", line)
            if not m:
                continue
            idx = int(m.group(1))
            symbol = m.group(2).strip()
            rest = m.group(3).strip()
            if idx < 1 or idx > 120:
                continue
            if symbol in {"SET", "SET50", "SET100"}:
                continue
            for sector in sectors:
                if rest.endswith(sector):
                    name = rest[: -len(sector)].strip()
                    if name:
                        candidates.append((idx, symbol, name, sector))
                    break

    if not candidates:
        raise RuntimeError("Could not parse SET100 constituents from SET PDF")

    # Choose the longest continuous run 1..N in encounter order.
    best: list[tuple[int, str, str, str]] = []
    current: list[tuple[int, str, str, str]] = []
    expected = 1
    for row in candidates:
        idx = row[0]
        if idx == 1:
            if len(current) > len(best):
                best = current
            current = [row]
            expected = 2
            continue
        if current and idx == expected:
            current.append(row)
            expected += 1
        elif current:
            if len(current) > len(best):
                best = current
            current = []
            expected = 1
    if len(current) > len(best):
        best = current

    if len(best) < 80:
        raise RuntimeError(f"Parsed SET100 block is too short: {len(best)}")

    return [
        UniverseMember(symbol=symbol, name=name, sector=sector)
        for _, symbol, name, sector in best
    ]


def load_universe_csv(path: str) -> list[UniverseMember]:
    df = pd.read_csv(path)
    required = ["symbol"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise RuntimeError(f"Universe CSV missing columns: {missing}")
    members = []
    for _, row in df.iterrows():
        symbol = str(row["symbol"]).strip().upper()
        if not symbol or symbol == "NAN":
            continue
        members.append(
            UniverseMember(
                symbol=symbol,
                name=str(row.get("name", symbol)).strip(),
                sector=str(row.get("sector", "Unknown")).strip(),
            )
        )
    if not members:
        raise RuntimeError("Universe CSV produced no symbols")
    return members


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


def fetch_with_retry(fn, attempts: int = 5):
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(0.5 * (attempt + 1))
    raise last_error


def screen_stock(
    member: UniverseMember,
    benchmark_close: pd.Series,
    range_: str,
    min_turnover_million: float,
    min_price: float,
) -> dict[str, object]:
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
    turnover_avg20_m = ((close * volume).rolling(20).mean().iloc[-1]) / 1_000_000

    ret_5d = close.iloc[-1] / close.iloc[-6] - 1
    ret_10d = close.iloc[-1] / close.iloc[-11] - 1
    ret_20d = close.iloc[-1] / close.iloc[-21] - 1
    prev_ret_20d = close.iloc[-21] / close.iloc[-41] - 1

    bmk_ret_5d = benchmark_close.iloc[-1] / benchmark_close.iloc[-6] - 1
    bmk_ret_10d = benchmark_close.iloc[-1] / benchmark_close.iloc[-11] - 1
    bmk_ret_20d = benchmark_close.iloc[-1] / benchmark_close.iloc[-21] - 1
    bmk_prev_ret_20d = benchmark_close.iloc[-21] / benchmark_close.iloc[-41] - 1

    excess_20d = ret_20d - bmk_ret_20d
    prev_excess_20d = prev_ret_20d - bmk_prev_ret_20d
    rs_20d_improving = excess_20d > prev_excess_20d

    atr10_pct = atr10.iloc[-1] / latest
    atr50_pct = atr50.iloc[-1] / latest
    trend_ok = ema50.iloc[-1] > ema200.iloc[-1] or ema50.iloc[-1] > ema50.iloc[-11]
    rs_ok = ret_10d > bmk_ret_10d or (ret_5d > bmk_ret_5d and rs_20d_improving)

    checks = {
        "price_gt_ema50": latest > ema50.iloc[-1],
        "price_gt_ema200": latest > ema200.iloc[-1],
        "ema50_gt_ema200_or_slope10d_up": trend_ok,
        "close_gt_ema20": latest > ema20.iloc[-1],
        "rsi14_45_to_70": 45 <= rsi14.iloc[-1] <= 70,
        "price_lte_ema20_x_1_08": latest <= ema20.iloc[-1] * 1.08,
        "rs10_gt_benchmark_or_rs5_gt_benchmark_and_rs20_improving": rs_ok,
        "close_gte_95pct_20d_high": latest >= high20_prev * 0.95,
        "volume_gte_0_8x_avg20": volume.iloc[-1] >= 0.8 * volume_avg20,
        "atr10_pct_lte_atr50_pct_x_1_10": atr10_pct <= atr50_pct * 1.10,
        "turnover_avg20_gte_min_million": turnover_avg20_m >= min_turnover_million,
        "price_gte_min_price": latest >= min_price,
    }
    setup_score = round(sum(100 / len(checks) for passed in checks.values() if passed), 1)

    # Build 10-day persistence from daily conditions.
    ret_5d_series = close.pct_change(5)
    ret_10d_series = close.pct_change(10)
    ret_20d_series = close.pct_change(20)
    prev_ret_20d_series = close.shift(20) / close.shift(40) - 1

    bmk_ret_5d_series = benchmark_close.pct_change(5).reindex(close.index)
    bmk_ret_10d_series = benchmark_close.pct_change(10).reindex(close.index)
    bmk_ret_20d_series = benchmark_close.pct_change(20).reindex(close.index)
    bmk_prev_ret_20d_series = benchmark_close.shift(20) / benchmark_close.shift(40) - 1
    bmk_prev_ret_20d_series = bmk_prev_ret_20d_series.reindex(close.index)

    excess_20d_series = ret_20d_series - bmk_ret_20d_series
    prev_excess_20d_series = prev_ret_20d_series - bmk_prev_ret_20d_series
    rs20_improving_series = excess_20d_series > prev_excess_20d_series
    trend_ok_series = (ema50 > ema200) | (ema50 > ema50.shift(10))
    rs_ok_series = (ret_10d_series > bmk_ret_10d_series) | (
        (ret_5d_series > bmk_ret_5d_series) & rs20_improving_series
    )
    high20_prev_series = high.shift(1).rolling(20).max()
    volume_avg20_series = volume.rolling(20).mean()
    turnover_avg20_m_series = ((close * volume).rolling(20).mean()) / 1_000_000

    checks_daily = pd.DataFrame(
        {
            "price_gt_ema50": close > ema50,
            "price_gt_ema200": close > ema200,
            "ema50_gt_ema200_or_slope10d_up": trend_ok_series,
            "close_gt_ema20": close > ema20,
            "rsi14_45_to_70": (rsi14 >= 45) & (rsi14 <= 70),
            "price_lte_ema20_x_1_08": close <= ema20 * 1.08,
            "rs10_gt_benchmark_or_rs5_gt_benchmark_and_rs20_improving": rs_ok_series,
            "close_gte_95pct_20d_high": close >= high20_prev_series * 0.95,
            "volume_gte_0_8x_avg20": volume >= 0.8 * volume_avg20_series,
            "atr10_pct_lte_atr50_pct_x_1_10": (atr10 / close) <= ((atr50 / close) * 1.10),
            "turnover_avg20_gte_min_million": turnover_avg20_m_series >= min_turnover_million,
            "price_gte_min_price": close >= min_price,
        }
    ).fillna(False)
    setup_score_daily = checks_daily.sum(axis=1) * (100.0 / len(checks))
    rs10_daily_pct = (ret_10d_series - bmk_ret_10d_series) * 100
    persistence_mask = (rs10_daily_pct > 0) & (setup_score_daily >= 80)
    persistence_window = persistence_mask.tail(10)
    strong_days_10d = int(persistence_window.sum()) if len(persistence_window) == 10 else None

    latest_high = float(high.iloc[-1])
    latest_low = float(low.iloc[-1])
    if latest_high == latest_low:
        close_position = 0.5
    else:
        close_position = max(0.0, min(1.0, (latest - latest_low) / (latest_high - latest_low)))

    return {
        "symbol": member.symbol,
        "name": member.name,
        "sector": member.sector,
        "date": close.index[-1],
        "pass": all(checks.values()),
        "setup_score": setup_score,
        "close": round(latest, 2),
        "high": round(latest_high, 2),
        "low": round(latest_low, 2),
        "close_position": round(close_position, 2),
        "strong_days_10d": strong_days_10d if strong_days_10d is not None else "",
        "ema20": round(ema20.iloc[-1], 2),
        "ema50": round(ema50.iloc[-1], 2),
        "ema200": round(ema200.iloc[-1], 2),
        "ema50_slope_10d_pct": round(pct(ema50.iloc[-1] / ema50.iloc[-11] - 1), 2),
        "high20_prev": round(high20_prev, 2),
        "distance_to_20d_high_pct": round(pct(latest / high20_prev - 1), 2),
        "volume": int(volume.iloc[-1]),
        "volume_avg20": int(volume_avg20),
        "volume_ratio": round(volume.iloc[-1] / volume_avg20, 2),
        "turnover_avg20_million_thb": round(turnover_avg20_m, 2),
        "atr10_pct": round(pct(atr10_pct), 2),
        "atr50_pct": round(pct(atr50_pct), 2),
        "rsi14": round(rsi14.iloc[-1], 1),
        "ret_5d_pct": round(pct(ret_5d), 2),
        "benchmark_ret_5d_pct": round(pct(bmk_ret_5d), 2),
        "ret_10d_pct": round(pct(ret_10d), 2),
        "benchmark_ret_10d_pct": round(pct(bmk_ret_10d), 2),
        "ret_20d_pct": round(pct(ret_20d), 2),
        "benchmark_ret_20d_pct": round(pct(bmk_ret_20d), 2),
        "rs20_excess_pct": round(pct(excess_20d), 2),
        "prev_rs20_excess_pct": round(pct(prev_excess_20d), 2),
        "rs20_improving": rs_20d_improving,
        "distance_ema20_pct": round(pct(latest / ema20.iloc[-1] - 1), 2),
        **checks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find Thai early-trend setup candidates.")
    parser.add_argument("--range", default="3y", help="Yahoo chart range, e.g. 1y, 2y, 3y.")
    parser.add_argument("--workers", type=int, default=10, help="Parallel download workers.")
    parser.add_argument("--limit", type=int, default=0, help="Limit printed passing rows; 0 means no limit.")
    parser.add_argument("--csv", dest="csv_path", default="thai_analyze_all.csv", help="Path to write all rows as CSV.")
    parser.add_argument(
        "--passing-csv",
        dest="passing_csv_path",
        default="thai_analyze_passing.csv",
        help="Path to write passing rows as CSV.",
    )
    parser.add_argument(
        "--universe",
        choices=["set50", "set100", "csv"],
        default="set100",
        help="Universe source: SET50/SET100 from public page or CSV file.",
    )
    parser.add_argument("--universe-csv", default="", help="CSV path for --universe csv (needs `symbol`; optional `name`, `sector`).")
    parser.add_argument("--benchmark", default=DEFAULT_BENCHMARK, help="Benchmark symbol for RS comparison. Default: ^SET.BK")
    parser.add_argument("--min-turnover-m", type=float, default=20.0, help="Minimum average turnover 20D in million THB.")
    parser.add_argument("--min-price", type=float, default=2.0, help="Minimum close price in THB.")
    return parser.parse_args()


def load_universe(args: argparse.Namespace) -> list[UniverseMember]:
    if args.universe == "set50":
        return load_set50_universe()
    if args.universe == "set100":
        return load_set100_universe()
    if not args.universe_csv:
        raise RuntimeError("--universe-csv is required when --universe csv")
    return load_universe_csv(args.universe_csv)


def main() -> int:
    args = parse_args()
    members = load_universe(args)
    benchmark = fetch_with_retry(lambda: load_chart(args.benchmark, args.range))

    rows: list[dict[str, object]] = []
    errors: list[tuple[str, str]] = []

    def run(member: UniverseMember) -> dict[str, object]:
        return fetch_with_retry(
            lambda: screen_stock(member, benchmark["close"], args.range, args.min_turnover_m, args.min_price)
        )

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
        "setup_score",
        "close",
        "high",
        "low",
        "close_position",
        "strong_days_10d",
        "ema20",
        "ema50",
        "ema200",
        "ema50_slope_10d_pct",
        "high20_prev",
        "distance_to_20d_high_pct",
        "volume",
        "volume_avg20",
        "volume_ratio",
        "turnover_avg20_million_thb",
        "atr10_pct",
        "atr50_pct",
        "rsi14",
        "ret_5d_pct",
        "benchmark_ret_5d_pct",
        "ret_10d_pct",
        "benchmark_ret_10d_pct",
        "ret_20d_pct",
        "benchmark_ret_20d_pct",
        "rs20_excess_pct",
        "prev_rs20_excess_pct",
        "rs20_improving",
        "distance_ema20_pct",
        "price_gt_ema50",
        "price_gt_ema200",
        "ema50_gt_ema200_or_slope10d_up",
        "close_gt_ema20",
        "rsi14_45_to_70",
        "price_lte_ema20_x_1_08",
        "rs10_gt_benchmark_or_rs5_gt_benchmark_and_rs20_improving",
        "close_gte_95pct_20d_high",
        "volume_gte_0_8x_avg20",
        "atr10_pct_lte_atr50_pct_x_1_10",
        "turnover_avg20_gte_min_million",
        "price_gte_min_price",
    ]

    rows.sort(key=lambda row: (-float(row["setup_score"]), -float(row["ret_10d_pct"]), str(row["symbol"])))
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

    bmk_close = benchmark["close"]
    bmk_5d_return = bmk_close.iloc[-1] / bmk_close.iloc[-6] - 1
    bmk_10d_return = bmk_close.iloc[-1] / bmk_close.iloc[-11] - 1
    bmk_20d_return = bmk_close.iloc[-1] / bmk_close.iloc[-21] - 1

    print(f"Universe: {len(members)} Thai stocks ({args.universe})")
    print(f"Benchmark: {args.benchmark}")
    print(f"Data date: {benchmark.index[-1]}")
    print(f"Benchmark 5D return: {pct(bmk_5d_return):.2f}%")
    print(f"Benchmark 10D return: {pct(bmk_10d_return):.2f}%")
    print(f"Benchmark 20D return: {pct(bmk_20d_return):.2f}%")
    print(f"Passing: {len(passing)}")
    print(f"Errors: {len(errors)}")
    if errors:
        print("Error sample:", "; ".join(f"{symbol}: {message}" for symbol, message in errors[:5]))
    print()
    print("symbol,score,name,sector,close,turnover_avg20_m,volume_ratio,rsi14,atr10_pct,atr50_pct,ret_5d_pct,ret_10d_pct,ret_20d_pct")
    for row in shown:
        print(
            f"{row['symbol']},{row['setup_score']},{row['name']},{row['sector']},{row['close']},"
            f"{row['turnover_avg20_million_thb']},{row['volume_ratio']},{row['rsi14']},"
            f"{row['atr10_pct']},{row['atr50_pct']},{row['ret_5d_pct']},"
            f"{row['ret_10d_pct']},{row['ret_20d_pct']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
