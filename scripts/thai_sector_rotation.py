#!/usr/bin/env python3
"""Rank Thai sector rotation using breadth and relative strength versus benchmark."""

from __future__ import annotations

import argparse
import csv

import pandas as pd


def percentile_scores(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(values.items(), key=lambda item: item[1])
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0}
    return {sector: rank / (len(ordered) - 1) for rank, (sector, _) in enumerate(ordered)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank Thai sector rotation from thai_early_trend output.")
    parser.add_argument("--stock-csv", required=True, help="CSV from thai_early_trend.py --csv.")
    parser.add_argument("--csv", dest="csv_path", default="thai_sector_rotation.csv", help="Output CSV path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    df = pd.read_csv(args.stock_csv)
    if df.empty:
        raise RuntimeError("Stock CSV is empty")

    if "sector" not in df.columns:
        raise RuntimeError("Stock CSV missing `sector` column")

    grouped = df.groupby("sector", dropna=False)
    rows: list[dict[str, object]] = []

    for sector, group in grouped:
        sector_name = str(sector) if str(sector) != "nan" else "Unknown"
        stock_count = len(group)
        pass_count = int(group["pass"].sum()) if "pass" in group.columns else 0
        score80_pct = float((group["setup_score"] >= 80).mean()) * 100 if "setup_score" in group.columns else 0
        price_gt_ema50_pct = float(group["price_gt_ema50"].mean()) * 100 if "price_gt_ema50" in group.columns else 0

        rs5_excess = (group["ret_5d_pct"] - group["benchmark_ret_5d_pct"]).median()
        rs10_excess = (group["ret_10d_pct"] - group["benchmark_ret_10d_pct"]).median()
        rs20_excess = (group["ret_20d_pct"] - group["benchmark_ret_20d_pct"]).median()

        rows.append(
            {
                "sector": sector_name,
                "date": str(group["date"].dropna().iloc[0]) if "date" in group.columns and not group["date"].dropna().empty else "n/a",
                "stock_count": stock_count,
                "pass_count": pass_count,
                "avg_setup_score": round(float(group["setup_score"].mean()), 1) if "setup_score" in group.columns else 0.0,
                "pct_setup_score_80": round(score80_pct, 1),
                "pct_price_gt_ema50": round(price_gt_ema50_pct, 1),
                "rs_5d_vs_benchmark_pct": round(float(rs5_excess), 2),
                "rs_10d_vs_benchmark_pct": round(float(rs10_excess), 2),
                "rs_20d_vs_benchmark_pct": round(float(rs20_excess), 2),
            }
        )

    rs20_rank = percentile_scores({row["sector"]: float(row["rs_20d_vs_benchmark_pct"]) for row in rows})
    rs10_rank = percentile_scores({row["sector"]: float(row["rs_10d_vs_benchmark_pct"]) for row in rows})
    for row in rows:
        sector = str(row["sector"])
        row["sector_rotation_score"] = round(
            40 * rs20_rank[sector]
            + 25 * rs10_rank[sector]
            + 20 * (float(row["pct_setup_score_80"]) / 100)
            + 15 * (float(row["pct_price_gt_ema50"]) / 100),
            1,
        )

    rows.sort(key=lambda row: (-float(row["sector_rotation_score"]), -float(row["rs_20d_vs_benchmark_pct"])))

    fieldnames = [
        "sector",
        "date",
        "sector_rotation_score",
        "stock_count",
        "pass_count",
        "avg_setup_score",
        "pct_setup_score_80",
        "pct_price_gt_ema50",
        "rs_5d_vs_benchmark_pct",
        "rs_10d_vs_benchmark_pct",
        "rs_20d_vs_benchmark_pct",
    ]
    with open(args.csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Data date: {rows[0]['date'] if rows else 'n/a'}")
    print("sector,score,rs_5d_vs_benchmark,rs_10d_vs_benchmark,rs_20d_vs_benchmark,pass_count,score80_pct,price_gt_ema50_pct")
    for row in rows:
        print(
            f"{row['sector']},{row['sector_rotation_score']},{row['rs_5d_vs_benchmark_pct']},"
            f"{row['rs_10d_vs_benchmark_pct']},{row['rs_20d_vs_benchmark_pct']},"
            f"{row['pass_count']},{row['pct_setup_score_80']},{row['pct_price_gt_ema50']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
