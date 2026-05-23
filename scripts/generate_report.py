#!/usr/bin/env python3
"""Create a Thai market scan report and optionally send it to Telegram."""

from __future__ import annotations

import argparse
import mimetypes
import os
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd


def fmt_pct(value: object) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def fmt_num(value: object) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "n/a"


def build_report(stock_csv: str, passing_csv: str, sector_csv: str, top: int) -> str:
    all_stocks = pd.read_csv(stock_csv)
    passing = pd.read_csv(passing_csv)
    sectors = pd.read_csv(sector_csv)

    data_date = str(all_stocks["date"].dropna().iloc[0]) if not all_stocks.empty else "n/a"
    run_time = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"S&P 500 วิเคราะห์หุ้น {data_date}",
        f"เวลารันไทย: {run_time}",
        f"หุ้นผ่านสูตร: {len(passing)} / {len(all_stocks)}",
        "",
        "กลุ่มนำ:",
    ]

    for idx, row in sectors.head(5).iterrows():
        lines.append(
            f"{idx + 1}. {row['sector']} ({row['etf']}) "
            f"score {fmt_num(row['sector_rotation_score'])}, "
            f"RS10 {fmt_pct(row['rs_10d_vs_spy_pct'])}, "
            f"RS20 {fmt_pct(row['rs_20d_vs_spy_pct'])}, "
            f"ผ่าน {int(row['pass_count'])} ตัว"
        )

    lines += ["", "หุ้นเด่น:"]
    if passing.empty:
        lines.append("ไม่มีหุ้นผ่านครบสูตรวันนี้")
    else:
        for idx, row in passing.head(top).iterrows():
            lines.append(
                f"{idx + 1}. {row['symbol']} | {row['sector']} | "
                f"score {int(row['setup_score'])} | "
                f"RS10 {fmt_pct(row['ret_10d_pct'])} | "
                f"vol {float(row['volume_ratio']):.2f}x | "
                f"RSI {float(row['rsi14']):.1f}"
            )

    lines += [
        "",
        "หมายเหตุ: เป็น watchlist จากข้อมูลราคา/วอลุ่ม ไม่ใช่คำแนะนำลงทุน",
    ]
    return "\n".join(lines)


def send_telegram(text: str, token: str, chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=25) as response:
        response.read()


def send_telegram_document(path: str, token: str, chat_id: str) -> None:
    boundary = f"----stockscanner{uuid.uuid4().hex}"
    filename = os.path.basename(path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    with open(path, "rb") as handle:
        file_bytes = handle.read()

    parts = [
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
            f"{chat_id}\r\n"
        ).encode("utf-8"),
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8"),
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        response.read()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate stock scan report.")
    parser.add_argument("--stock-csv", default="analyze_stocks_all.csv")
    parser.add_argument("--passing-csv", default="analyze_stocks_passing.csv")
    parser.add_argument("--sector-csv", default="sector_rotation.csv")
    parser.add_argument("--output", default="summary.txt")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--telegram-csv", action="append", default=[], help="CSV file to send to Telegram as a document. Can be repeated.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.stock_csv, args.passing_csv, args.sector_csv, args.top)
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(report + "\n")
    print(report)

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        send_telegram(report, token, chat_id)
        for csv_path in args.telegram_csv:
            send_telegram_document(csv_path, token, chat_id)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
