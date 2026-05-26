#!/usr/bin/env python3
"""S&P 500 end-of-day scanner v1.2.

Outputs:
1) analyze_stocks_all.csv
2) sector_rotation.csv
3) next_day_watchlist.csv
4) summary_report.txt
5) summary_report.md
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any
import urllib.request

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except Exception:  # noqa: BLE001
    yf = None


# =========================
# Configuration
# =========================
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLC", "XLU", "XLRE", "XLB"]
REGIME_SYMBOLS = ["SPY", "QQQ", "IWM", "VIX"]
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
DEFAULT_PERIOD = "1y"
DEFAULT_TOP = 10
USER_AGENT = {"User-Agent": "Mozilla/5.0"}
SECTOR_TO_ETF = {
    "Information Technology": "XLK",
    "Financials": "XLF",
    "Energy": "XLE",
    "Health Care": "XLV",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Communication Services": "XLC",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
}


@dataclass
class Dataset:
    stock_daily: pd.DataFrame  # date,ticker,sector,open,high,low,close,volume
    benchmark_daily: pd.DataFrame  # date,ticker,open,high,low,close,volume


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S&P 500 EOD Scanner v1.2")
    parser.add_argument("--mode", choices=["auto_download", "csv_input"], default="auto_download")
    parser.add_argument("--input-csv", help="Required in csv_input mode")
    parser.add_argument("--period", default=DEFAULT_PERIOD, help="yfinance period, e.g. 1y, 2y")
    parser.add_argument("--output-dir", default=".", help="Output directory")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="Top stocks in summary")
    return parser.parse_args()


def _to_yf_symbol(symbol: str) -> str:
    if symbol == "VIX":
        return "^VIX"
    return symbol.replace(".", "-")


def _from_yf_symbol(symbol: str) -> str:
    if symbol == "^VIX":
        return "VIX"
    return symbol.replace("-", ".")


def _safe_div(a: pd.Series, b: pd.Series, fill: float = np.nan) -> pd.Series:
    out = a / b.replace(0, np.nan)
    return out.replace([np.inf, -np.inf], np.nan).fillna(fill)


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = _safe_div(avg_gain, avg_loss)
    return 100 - (100 / (1 + rs))


def _percentile_rank(series: pd.Series) -> pd.Series:
    if series.empty:
        return series
    return series.rank(method="average", pct=True) * 100


def _load_sp500_universe() -> pd.DataFrame:
    req = urllib.request.Request(SP500_URL, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "ignore")
    table = pd.read_html(StringIO(html))[0]
    return table[["Symbol", "GICS Sector"]].rename(columns={"Symbol": "ticker", "GICS Sector": "sector"})


def _download_yf(tickers: list[str], period: str) -> pd.DataFrame:
    if yf is None:
        raise RuntimeError("yfinance is not available. Please install yfinance or use csv_input mode.")
    data = yf.download(
        tickers=tickers,
        period=period,
        interval="1d",
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=True,
    )
    if data is None or data.empty:
        raise RuntimeError("Failed to download data from yfinance.")
    return data


def _extract_ticker_frame(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    # Multi-index columns: (ticker, field) or (field, ticker) depending on yfinance shape.
    if isinstance(raw.columns, pd.MultiIndex):
        if ticker in raw.columns.get_level_values(0):
            frame = raw[ticker].copy()
        elif ticker in raw.columns.get_level_values(1):
            frame = raw.xs(ticker, axis=1, level=1).copy()
        else:
            return pd.DataFrame()
    else:
        frame = raw.copy() if ticker == raw.columns.name else pd.DataFrame()
    if frame.empty:
        return frame
    rename_map = {c: c.lower() for c in frame.columns}
    frame = frame.rename(columns=rename_map)
    need = ["open", "high", "low", "close", "volume"]
    for col in need:
        if col not in frame.columns:
            return pd.DataFrame()
    frame = frame[need].dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    frame.index = pd.to_datetime(frame.index).tz_localize(None)
    frame = frame.reset_index().rename(columns={"Date": "date", "index": "date"})
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["ticker"] = _from_yf_symbol(ticker)
    return frame


def load_data(mode: str, input_csv: str | None, period: str) -> Dataset:
    if mode == "csv_input":
        if not input_csv:
            raise ValueError("--input-csv is required in csv_input mode")
        df = pd.read_csv(input_csv)
        required = {"date", "ticker", "sector", "open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing required columns in CSV: {sorted(missing)}")
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["ticker"] = df["ticker"].astype(str).str.upper()
        bench = df[df["ticker"].isin(REGIME_SYMBOLS + SECTOR_ETFS)][["date", "ticker", "open", "high", "low", "close", "volume"]].copy()
        stocks = df[~df["ticker"].isin(REGIME_SYMBOLS + SECTOR_ETFS)].copy()
        return Dataset(stock_daily=stocks, benchmark_daily=bench)

    universe = _load_sp500_universe()
    stock_tickers = universe["ticker"].tolist()
    all_symbols = [_to_yf_symbol(t) for t in stock_tickers + SECTOR_ETFS + REGIME_SYMBOLS]
    raw = _download_yf(all_symbols, period=period)

    frames: list[pd.DataFrame] = []
    for sym in all_symbols:
        f = _extract_ticker_frame(raw, sym)
        if not f.empty:
            frames.append(f)
    if not frames:
        raise RuntimeError("No data downloaded from yfinance.")

    all_daily = pd.concat(frames, ignore_index=True)
    all_daily["ticker"] = all_daily["ticker"].astype(str).str.upper()
    sector_map = universe.set_index("ticker")["sector"].to_dict()
    all_daily["sector"] = all_daily["ticker"].map(sector_map)

    bench = all_daily[all_daily["ticker"].isin(REGIME_SYMBOLS + SECTOR_ETFS)][["date", "ticker", "open", "high", "low", "close", "volume"]].copy()
    stocks = all_daily[all_daily["ticker"].isin(stock_tickers)].copy()
    stocks = stocks.dropna(subset=["sector"])
    return Dataset(stock_daily=stocks, benchmark_daily=bench)


def calculate_indicators(stock_daily: pd.DataFrame) -> pd.DataFrame:
    df = stock_daily.copy()
    df = df.sort_values(["ticker", "date"])
    g = df.groupby("ticker", group_keys=False)

    df["daily_return_pct"] = g["close"].pct_change() * 100
    df["ret10"] = g["close"].pct_change(10)
    df["ret20"] = g["close"].pct_change(20)
    df["RSI"] = g["close"].transform(lambda s: _rsi(s, 14))
    df["20DMA"] = g["close"].transform(lambda s: s.rolling(20).mean())
    df["50DMA"] = g["close"].transform(lambda s: s.rolling(50).mean())
    df["200DMA"] = g["close"].transform(lambda s: s.rolling(200).mean())
    df["vol20"] = g["volume"].transform(lambda s: s.rolling(20).mean())
    df["volume_ratio"] = _safe_div(df["volume"], df["vol20"])
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["close_position"] = ((df["close"] - df["low"]) / rng).fillna(0.5).clip(0, 1)

    df["close_quality_label"] = np.select(
        [
            df["close_position"] >= 0.80,
            (df["close_position"] >= 0.50) & (df["close_position"] < 0.80),
            (df["close_position"] >= 0.30) & (df["close_position"] < 0.50),
        ],
        ["strong_close", "neutral_close", "weak_close"],
        default="poor_close",
    )
    return df


def calculate_stock_scores(indicator_df: pd.DataFrame, benchmark_daily: pd.DataFrame) -> pd.DataFrame:
    df = indicator_df.copy()
    bench = benchmark_daily.copy()
    bench = bench.sort_values(["ticker", "date"])
    bench["date"] = pd.to_datetime(bench["date"]).dt.date

    spy = bench[bench["ticker"] == "SPY"][["date", "close"]].copy()
    spy = spy.sort_values("date")
    spy["spy_ret10"] = spy["close"].pct_change(10)
    spy["spy_ret20"] = spy["close"].pct_change(20)
    df = df.merge(spy[["date", "spy_ret10", "spy_ret20"]], on="date", how="left")

    df["RS10"] = (df["ret10"] - df["spy_ret10"]) * 100
    df["RS20"] = (df["ret20"] - df["spy_ret20"]) * 100

    # Explainable base score (without sector bonus).
    score = np.zeros(len(df), dtype=float)
    score += np.where(df["RS10"] > 0, 20, 0)
    score += np.where(df["RS20"] > 0, 15, 0)
    score += np.where(df["close"] > df["20DMA"], 15, 0)
    score += np.where(df["close"] > df["50DMA"], 15, 0)
    score += np.where(df["close"] > df["200DMA"], 10, 0)
    score += np.where((df["RSI"] >= 50) & (df["RSI"] <= 70), 10, 0)
    score += np.where(df["volume_ratio"] >= 1.0, 10, np.where(df["volume_ratio"] >= 0.8, 5, 0))
    df["score_pre_sector"] = score

    # Sector strength bonus by date from cross-sectional sector average score.
    sector_daily = (
        df.groupby(["date", "sector"], as_index=False)["score_pre_sector"]
        .mean()
        .rename(columns={"score_pre_sector": "sector_pre"})
    )
    sector_daily["sector_rank_pct"] = sector_daily.groupby("date")["sector_pre"].rank(pct=True)
    sector_daily["sector_bonus"] = np.select(
        [sector_daily["sector_rank_pct"] >= 0.8, sector_daily["sector_rank_pct"] >= 0.5],
        [10, 5],
        default=0,
    )
    df = df.merge(sector_daily[["date", "sector", "sector_bonus"]], on=["date", "sector"], how="left")
    df["score"] = (df["score_pre_sector"] + df["sector_bonus"].fillna(0)).clip(0, 100)

    cond_strong = (df["score"] >= 80) & (df["RS10"] > 0) & (df["volume_ratio"] >= 0.8)
    df["strong_day_flag"] = cond_strong.astype(int)
    df["strong_days_5d"] = (
        df.sort_values(["ticker", "date"]).groupby("ticker")["strong_day_flag"].transform(lambda s: s.rolling(5).sum())
    )
    df["strong_days_10d"] = (
        df.sort_values(["ticker", "date"]).groupby("ticker")["strong_day_flag"].transform(lambda s: s.rolling(10).sum())
    )
    df["flow_persistence_label"] = np.select(
        [df["strong_days_10d"] >= 7, df["strong_days_10d"] >= 4, df["strong_days_10d"] >= 2],
        ["persistent_accumulation", "developing_strength", "short_term_momentum"],
        default="low_persistence",
    )
    return df


def classify_market_regime(benchmark_daily: pd.DataFrame) -> str:
    try:
        b = benchmark_daily.copy()
        b = b.sort_values(["ticker", "date"])
        regimes = {}
        for sym in ["SPY", "QQQ", "IWM", "VIX"]:
            s = b[b["ticker"] == sym].copy()
            if s.empty:
                return "unknown"
            s["20DMA"] = s["close"].rolling(20).mean()
            s["50DMA"] = s["close"].rolling(50).mean()
            regimes[sym] = s.iloc[-1]

        spy, qqq, vix = regimes["SPY"], regimes["QQQ"], regimes["VIX"]
        if pd.isna(spy["20DMA"]) or pd.isna(spy["50DMA"]) or pd.isna(qqq["20DMA"]) or pd.isna(vix["20DMA"]):
            return "unknown"
        if vix["close"] > vix["20DMA"] * 1.15:
            return "high_volatility"
        if spy["close"] > spy["20DMA"] > spy["50DMA"] and qqq["close"] > qqq["20DMA"] and vix["close"] < vix["20DMA"]:
            return "bull_trend"
        if spy["close"] > spy["50DMA"]:
            return "weak_bull"
        if min(spy["20DMA"], spy["50DMA"]) <= spy["close"] <= max(spy["20DMA"], spy["50DMA"]):
            return "sideways"
        if spy["close"] < spy["50DMA"] and regimes["QQQ"]["close"] < regimes["QQQ"]["50DMA"]:
            return "correction"
        return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def calculate_sector_scores(latest_df: pd.DataFrame) -> pd.DataFrame:
    base = latest_df.copy()
    if "suspicious_rally_flag" not in base.columns:
        base["suspicious_rally_flag"] = False
    if "distribution_flag" not in base.columns:
        base["distribution_flag"] = False

    sec = (
        base.groupby("sector", as_index=False)
        .agg(
            sector_score=("score", "mean"),
            avg_RS10=("RS10", "mean"),
            avg_RS20=("RS20", "mean"),
            avg_close_position=("close_position", "mean"),
            avg_volume_ratio=("volume_ratio", "mean"),
            passed_count=("ticker", "count"),
            pct_strong_close=("close_quality_label", lambda s: (s == "strong_close").mean() * 100),
            pct_suspicious_rally=("suspicious_rally_flag", lambda s: s.mean() * 100),
            pct_distribution=("distribution_flag", lambda s: s.mean() * 100),
        )
        .sort_values("sector_score", ascending=False)
    )
    sec["sector_flow_label"] = np.select(
        [
            (sec["sector_score"] >= 75) & (sec["avg_close_position"] >= 0.65) & (sec["avg_volume_ratio"] >= 1.1),
            (sec["sector_score"] >= 60) & (sec["avg_close_position"] >= 0.55),
            sec["sector_score"] >= 50,
        ],
        ["strong_inflow", "healthy_inflow", "weak_inflow"],
        default="outflow_or_weak",
    )
    return sec


def create_watchlist(scored_df: pd.DataFrame, market_regime: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    latest_date = scored_df["date"].max()
    latest = scored_df[scored_df["date"] == latest_date].copy()

    latest["relative_volume_rank"] = _percentile_rank(latest["volume_ratio"])
    latest["RS10_rank"] = _percentile_rank(latest["RS10"])
    latest["RS20_rank"] = _percentile_rank(latest["RS20"])
    latest["close_position_rank"] = _percentile_rank(latest["close_position"])
    latest["strong_days_10d_rank"] = _percentile_rank(latest["strong_days_10d"])

    sector_df = calculate_sector_scores(latest)
    top3_sector = sector_df["sector"].head(3).tolist()
    latest["sector_top3"] = latest["sector"].isin(top3_sector)

    high20 = (
        scored_df.sort_values(["ticker", "date"])
        .groupby("ticker")["close"]
        .transform(lambda s: s.shift(1).rolling(20).max())
    )
    latest = latest.merge(
        scored_df.loc[scored_df["date"] == latest_date, ["ticker"]].assign(prev_20d_high=high20[scored_df["date"] == latest_date].values),
        on="ticker",
        how="left",
    )
    latest["is_new_20d_high"] = latest["close"] >= latest["prev_20d_high"]

    latest["breakout_quality"] = np.select(
        [
            latest["is_new_20d_high"] & (latest["volume_ratio"] >= 1.5) & (latest["close_position"] >= 0.80) & latest["sector_top3"],
            latest["is_new_20d_high"] & (latest["volume_ratio"] >= 1.0) & (latest["close_position"] >= 0.65),
            latest["is_new_20d_high"],
        ],
        ["A", "B", "C"],
        default="None",
    )

    latest["money_flow_label"] = np.select(
        [
            (latest["score"] >= 90) & (latest["volume_ratio"] >= 1.5) & (latest["close_position"] >= 0.80) & (latest["strong_days_10d"] >= 4),
            (latest["score"] >= 80) & (latest["volume_ratio"] >= 1.0) & (latest["close_position"] >= 0.65),
            (latest["score"] >= 80) & (latest["volume_ratio"] < 1.0),
            (latest["close_position"] < 0.30) & (latest["volume_ratio"] >= 1.5),
        ],
        [
            "strong_institutional_flow",
            "healthy_flow",
            "trend_without_volume_confirmation",
            "distribution_risk",
        ],
        default="weak_or_neutral",
    )

    latest["suspicious_rally_flag"] = (
        (latest["score"] >= 80)
        & (latest["RS10"] > 3)
        & ((latest["volume_ratio"] < 0.9) | (latest["close_position"] < 0.50))
    )
    latest["distribution_flag"] = (
        (latest["daily_return_pct"] < -2)
        & (latest["volume_ratio"] >= 1.5)
        & (latest["close_position"] < 0.30)
    )

    latest["final_watchlist_score"] = (
        latest["score"] * 0.35
        + latest["RS10_rank"] * 0.15
        + latest["RS20_rank"] * 0.15
        + latest["relative_volume_rank"] * 0.10
        + latest["close_position_rank"] * 0.15
        + latest["strong_days_10d_rank"] * 0.10
    ).clip(0, 100)

    latest["next_day_group"] = np.select(
        [
            latest["money_flow_label"].isin(["strong_institutional_flow", "healthy_flow"]) & (latest["close_position"] >= 0.75) & latest["sector_top3"],
            (latest["score"] >= 80) & (latest["strong_days_10d"] >= 4) & latest["close_position"].between(0.40, 0.75) & (latest["volume_ratio"] < 1.2),
            latest["suspicious_rally_flag"] | (latest["money_flow_label"] == "trend_without_volume_confirmation"),
            latest["distribution_flag"] | (latest["money_flow_label"] == "distribution_risk"),
        ],
        ["continuation_candidate", "pullback_watch", "avoid_chasing", "distribution_risk"],
        default="neutral_watch",
    )

    latest["market_regime"] = market_regime
    latest["passed_filter"] = (
        (latest["score"] >= 80)
        | (latest["final_watchlist_score"] >= 80)
        | latest["money_flow_label"].isin(["strong_institutional_flow", "healthy_flow"])
    )

    all_cols = [
        "date",
        "ticker",
        "sector",
        "close",
        "daily_return_pct",
        "RS10",
        "RS20",
        "volume_ratio",
        "relative_volume_rank",
        "RSI",
        "score",
        "final_watchlist_score",
        "close_position",
        "close_quality_label",
        "strong_days_5d",
        "strong_days_10d",
        "flow_persistence_label",
        "breakout_quality",
        "money_flow_label",
        "suspicious_rally_flag",
        "distribution_flag",
        "market_regime",
        "next_day_group",
    ]
    all_df = latest[all_cols].copy().sort_values(["final_watchlist_score", "score"], ascending=False)

    watch_cols = [
        "ticker",
        "sector",
        "score",
        "final_watchlist_score",
        "RS10",
        "RS20",
        "volume_ratio",
        "RSI",
        "close_position",
        "strong_days_10d",
        "breakout_quality",
        "money_flow_label",
        "next_day_group",
    ]
    watch_df = all_df[all_df["ticker"].isin(latest[latest["passed_filter"]]["ticker"])][watch_cols].copy()
    return all_df, sector_df, watch_df


def _market_regime_thai(regime: str) -> str:
    mapping = {
        "bull_trend": "ตลาดเป็นขาขึ้นชัดเจน ภาพรวมเสี่ยงต่ำกว่าปกติ",
        "weak_bull": "ตลาดยังเป็นบวก แต่แรงขึ้นไม่กระจายทุกกลุ่ม",
        "sideways": "ตลาดแกว่งตัว รอเลือกหุ้นรายตัวที่แข็งจริง",
        "correction": "ตลาดอยู่ช่วงพักฐาน ควรเน้นการคุมความเสี่ยง",
        "high_volatility": "ตลาดผันผวนสูง ควรลดการไล่ราคา",
        "unknown": "ข้อมูลตลาดไม่ครบพอสำหรับจัด regime",
    }
    return mapping.get(regime, "ข้อมูลตลาดไม่ครบพอสำหรับจัด regime")


def generate_thai_summary(
    all_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    watch_df: pd.DataFrame,
    market_regime: str,
    top_n: int,
) -> str:
    latest_date = str(all_df["date"].max())
    now_th = datetime.now().strftime("%Y-%m-%d %H:%M")
    passed_count = int((all_df["score"] >= 80).sum())
    total_count = int(len(all_df))

    lines = [
        f"S&P 500 วิเคราะห์หุ้น {latest_date}",
        f"เวลารันไทย: {now_th}",
        f"หุ้นผ่านสูตร: {passed_count} / {total_count}",
        "",
        "กลุ่มนำ:",
    ]

    for i, row in sector_df.head(5).reset_index(drop=True).iterrows():
        etf = SECTOR_TO_ETF.get(row["sector"], "N/A")
        lines.append(
            f"{i+1}. {row['sector']} ({etf}) | score {row['sector_score']:.1f} | "
            f"RS10 {row['avg_RS10']:+.2f}% | RS20 {row['avg_RS20']:+.2f}% | ผ่าน {int(row['passed_count'])} ตัว"
        )
        lines.append("")

    lines += ["", "หุ้นเด่น:"]
    top = all_df.sort_values(["final_watchlist_score", "score"], ascending=False).head(top_n).reset_index(drop=True)
    for i, row in top.iterrows():
        lines.append(
            f"{i+1}. {row['ticker']} | {row['sector']} | score {int(round(row['score']))} | "
            f"RS10 {row['RS10']:+.2f}% | vol {row['volume_ratio']:.2f}x | RSI {row['RSI']:.1f}"
        )
        lines.append("")

    lines += [
        "",
        "หมายเหตุ:",
        "เป็น watchlist จากข้อมูลราคา/วอลุ่ม ไม่ใช่คำแนะนำลงทุน",
    ]
    return "\n".join(lines)


def export_outputs(
    output_dir: Path,
    all_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    watch_df: pd.DataFrame,
    summary: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_df.to_csv(output_dir / "analyze_stocks_all.csv", index=False)
    sector_df.to_csv(output_dir / "sector_rotation.csv", index=False)
    watch_df.to_csv(output_dir / "next_day_watchlist.csv", index=False)
    (output_dir / "summary_report.txt").write_text(summary, encoding="utf-8")
    (output_dir / "summary_report.md").write_text(summary, encoding="utf-8")


def main() -> None:
    args = parse_args()
    data = load_data(mode=args.mode, input_csv=args.input_csv, period=args.period)

    ind = calculate_indicators(data.stock_daily)
    scored = calculate_stock_scores(indicator_df=ind, benchmark_daily=data.benchmark_daily)
    regime = classify_market_regime(data.benchmark_daily)
    all_df, sector_df, watch_df = create_watchlist(scored_df=scored, market_regime=regime)
    summary = generate_thai_summary(
        all_df=all_df,
        sector_df=sector_df,
        watch_df=watch_df,
        market_regime=regime,
        top_n=args.top,
    )
    export_outputs(Path(args.output_dir), all_df, sector_df, watch_df, summary)
    print(summary)


if __name__ == "__main__":
    main()
