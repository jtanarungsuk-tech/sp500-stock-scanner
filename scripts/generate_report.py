#!/usr/bin/env python3
"""Create a Thai market scan report and optionally send it to Telegram."""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv


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


def mask_token(token: str | None) -> str:
    if not token:
        return "None"
    if len(token) <= 10:
        return token[:2] + "..." + token[-2:]
    return token[:6] + "..." + token[-4:]


def to_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def calc_close_position(row: pd.Series) -> float | None:
    direct = to_float(row.get("close_position"))
    if direct is not None:
        return max(0.0, min(1.0, direct))
    high = to_float(row.get("high"))
    low = to_float(row.get("low"))
    close = to_float(row.get("close"))
    if high is None or low is None or close is None:
        return None
    if high == low:
        return 0.5
    return max(0.0, min(1.0, (close - low) / (high - low)))


def close_quality(close_position: float | None) -> str:
    if close_position is None:
        return "ไม่มีข้อมูล close_position"
    if close_position >= 0.80:
        return "ปิดแข็งมาก"
    if close_position >= 0.60:
        return "ปิดดี"
    if close_position >= 0.40:
        return "ปิดกลาง ๆ"
    return "ปิดอ่อน"


def volume_confirmation(volume_ratio: float | None) -> str:
    if volume_ratio is None:
        return "ไม่มีข้อมูลวอลุ่ม"
    if volume_ratio >= 1.5:
        return "วอลุ่มยืนยันชัด"
    if volume_ratio >= 1.0:
        return "วอลุ่มปกติ/พอใช้"
    return "วอลุ่มยังไม่ยืนยัน"


def calc_persistence_map(all_stocks: pd.DataFrame) -> dict[str, str]:
    if all_stocks.empty or "symbol" not in all_stocks.columns:
        return {}
    required = {"date", "setup_score", "ret_10d_pct", "spy_ret_10d_pct"}
    if not required.issubset(set(all_stocks.columns)):
        return {}

    df = all_stocks.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    out: dict[str, str] = {}
    for symbol, grp in df.groupby("symbol"):
        grp = grp.sort_values("date").tail(10)
        if len(grp) < 10:
            out[str(symbol)] = "ข้อมูลย้อนหลังไม่พอ"
            continue
        rs10 = pd.to_numeric(grp["ret_10d_pct"], errors="coerce") - pd.to_numeric(grp["spy_ret_10d_pct"], errors="coerce")
        score = pd.to_numeric(grp["setup_score"], errors="coerce")
        cnt = int(((rs10 > 0) & (score >= 80)).sum())
        if cnt >= 7:
            out[str(symbol)] = "แข็งต่อเนื่องหลายวัน"
        elif cnt >= 4:
            out[str(symbol)] = "เริ่มแข็งต่อเนื่อง"
        elif cnt >= 2:
            out[str(symbol)] = "แข็งระยะสั้น"
        else:
            out[str(symbol)] = "ยังไม่ต่อเนื่อง"
    return out


def accumulation_signal(
    score: float | None,
    rs10: float | None,
    close_pos: float | None,
    vol_ratio: float | None,
) -> str:
    if score is None or rs10 is None or vol_ratio is None:
        return "ยังไม่ใช่สัญญาณเงินไหลเข้าเด่น"
    if close_pos is not None and score >= 90 and rs10 > 5 and close_pos >= 0.75 and vol_ratio >= 1.0:
        return "น่าจะมีแรงสะสมจริง"
    if close_pos is not None and score >= 80 and rs10 > 3 and close_pos >= 0.60 and vol_ratio < 1.0:
        return "มีแรงซื้อ แต่ยังไม่ชัดว่าเป็นเงินใหญ่"
    if score >= 80 and rs10 > 3 and vol_ratio < 1.0:
        return "ขึ้นดี แต่ต้องระวังวอลุ่มไม่ยืนยัน"
    return "ยังไม่ใช่สัญญาณเงินไหลเข้าเด่น"


def money_flow_label(signal: str) -> str:
    if signal == "น่าจะมีแรงสะสมจริง":
        return "Strong Inflow"
    if signal == "มีแรงซื้อ แต่ยังไม่ชัดว่าเป็นเงินใหญ่":
        return "Moderate Inflow"
    if signal == "ขึ้นดี แต่ต้องระวังวอลุ่มไม่ยืนยัน":
        return "Weak Confirmation"
    return "Neutral"


def flow_strength_icon(flow_label: str, close_pos: float | None, vol_ratio: float | None) -> str:
    if close_pos is not None and close_pos <= 0.30 and vol_ratio is not None and vol_ratio >= 1.5:
        return "❌❌❌"
    if flow_label == "Strong Inflow":
        return "🔥🔥🔥"
    if flow_label == "Moderate Inflow":
        return "🟢"
    if flow_label == "Weak Confirmation":
        return "🟡"
    return "⚠️"


def close_strength_icon(close_pos: float | None) -> str:
    if close_pos is None:
        return "⚠️"
    if close_pos > 0.85:
        return "🔥🔥🔥"
    if close_pos > 0.65:
        return "🟢"
    if close_pos > 0.45:
        return "🟡"
    if close_pos > 0.30:
        return "⚠️"
    return "❌❌❌"


def volume_strength_icon(vol_ratio: float | None) -> str:
    if vol_ratio is None:
        return "⚠️"
    if vol_ratio > 1.8:
        return "🔥🔥🔥"
    if vol_ratio > 1.2:
        return "🟢"
    if vol_ratio > 0.9:
        return "🟡"
    if vol_ratio > 0.7:
        return "⚠️"
    return "❌❌❌"


def trend_strength_icon(strong_days: float | None) -> str:
    if strong_days is None:
        return "❌❌❌"
    s = int(strong_days)
    if s >= 8:
        return "🔥🔥🔥"
    if s >= 5:
        return "🟢"
    if s >= 3:
        return "🟡"
    if s >= 1:
        return "⚠️"
    return "❌❌❌"


def main_status_label(
    flow_icon: str, close_icon: str, vol_icon: str, trend_icon: str, flow_label: str
) -> str:
    if flow_label == "Distribution Risk" or [flow_icon, close_icon, vol_icon, trend_icon].count("❌❌❌") >= 2:
        return "❌ Weak"
    if flow_icon == "🔥🔥🔥" and close_icon == "🔥🔥🔥" and trend_icon == "🔥🔥🔥":
        return "🔥 Strong"
    if trend_icon in {"🔥🔥🔥", "🟢"} and close_icon in {"⚠️", "❌❌❌"}:
        return "⚠️ Mixed"
    if trend_icon in {"🔥🔥🔥", "🟢"} and vol_icon in {"⚠️", "❌❌❌"}:
        return "⚠️ Mixed"
    if trend_icon in {"🔥🔥🔥", "🟢", "🟡"} and flow_icon != "❌❌❌":
        return "🟢 Good"
    return "⚠️ Mixed"


def analyst_summary_lines(
    rs10: float | None, close_icon: str, vol_icon: str, trend_icon: str, flow_icon: str
) -> list[str]:
    lines: list[str] = []
    if rs10 is not None and rs10 > 0:
        lines.append("หุ้นยังแข็งกว่าตลาดและ trend ยังดี")
    elif trend_icon in {"🔥🔥🔥", "🟢"}:
        lines.append("trend ยังดูดี")
    else:
        lines.append("แนวโน้มยังไม่ชัด")

    if close_icon in {"⚠️", "❌❌❌"}:
        lines.append("แต่เริ่มมีแรงขายระยะสั้น")
    elif flow_icon in {"⚠️", "🟡"}:
        lines.append("แรงซื้อยังไม่แน่น")
    else:
        lines.append("แรงซื้อยังพอประคองได้")

    if vol_icon in {"⚠️", "❌❌❌"}:
        lines.append("วอลุ่มยังไม่ค่อยสนับสนุน")
    elif vol_icon == "🟡":
        lines.append("วอลุ่มยังพอใช้")
    else:
        lines.append("วอลุ่มช่วยยืนยันการเคลื่อนไหว")
    return lines


def build_report(stock_csv: str, passing_csv: str, sector_csv: str, top: int) -> str:
    all_stocks = pd.read_csv(stock_csv)
    passing = pd.read_csv(passing_csv)
    sectors = pd.read_csv(sector_csv)
    persistence_map = calc_persistence_map(all_stocks)

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
            f"{idx + 1}. {row['sector']} ({row['etf']}) | "
            f"score {fmt_num(row['sector_rotation_score'])} | "
            f"RS10 {fmt_pct(row['rs_10d_vs_spy_pct'])} | "
            f"RS20 {fmt_pct(row['rs_20d_vs_spy_pct'])} | "
            f"ผ่าน {int(row['pass_count'])} ตัว"
        )
        lines.append("")

    lines += ["", "หุ้นเด่น:"]
    if passing.empty:
        lines.append("ไม่มีหุ้นผ่านครบสูตรวันนี้")
    else:
        for idx, row in passing.head(top).iterrows():
            symbol = str(row["symbol"])
            sector = str(row["sector"])
            score = to_float(row.get("setup_score"))
            rs10 = to_float(row.get("ret_10d_pct"))
            vol_ratio = to_float(row.get("volume_ratio"))
            rsi = to_float(row.get("rsi14"))
            close_pos = calc_close_position(row)
            close_pos_text = f"{close_pos:.2f}" if close_pos is not None else "ไม่มีข้อมูล close_position"
            vol_confirm = volume_confirmation(vol_ratio)
            close_q = close_quality(close_pos)
            strong_days = to_float(row.get("strong_days_10d"))
            if strong_days is not None:
                s = int(strong_days)
                if s >= 7:
                    persist = "แข็งต่อเนื่องหลายวัน"
                elif s >= 4:
                    persist = "เริ่มแข็งต่อเนื่อง"
                elif s >= 2:
                    persist = "แข็งระยะสั้น"
                else:
                    persist = "ยังไม่ต่อเนื่อง"
            else:
                persist = persistence_map.get(symbol, "ข้อมูลย้อนหลังไม่พอ")
            acc_signal = accumulation_signal(score, rs10, close_pos, vol_ratio)
            flow_label = money_flow_label(acc_signal)
            score_text = f"{int(score)}" if score is not None else "n/a"
            rs10_text = fmt_pct(rs10) if rs10 is not None else "n/a"
            vol_text = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
            rsi_text = f"{rsi:.1f}" if rsi is not None else "n/a"
            close_pos_text = f"{close_pos:.2f}" if close_pos is not None else "ไม่มีข้อมูล close_position"
            flow_icon = flow_strength_icon(flow_label, close_pos, vol_ratio)
            close_icon = close_strength_icon(close_pos)
            vol_icon = volume_strength_icon(vol_ratio)
            trend_icon = trend_strength_icon(strong_days)
            lines.append(f"{idx + 1}. {symbol} | {sector} | {main_status_label(flow_icon, close_icon, vol_icon, trend_icon, flow_label)}")
            lines.append("")
            lines.append(f"เงินเข้า   {flow_icon}")
            lines.append(f"ปิดราคา   {close_icon}")
            lines.append(f"วอลุ่ม     {vol_icon}")
            lines.append(f"แนวโน้ม   {trend_icon}")
            lines.append("")
            lines.append(f"score {score_text} | RS10 {rs10_text} | RSI {rsi_text}")
            lines.append("")
            lines.append("สรุป:")
            for text_line in analyst_summary_lines(rs10, close_icon, vol_icon, trend_icon, flow_icon):
                lines.append(text_line)
            lines.append("")

    lines += [
        "",
        "หมายเหตุ: เป็น watchlist จากข้อมูลราคา/วอลุ่ม ไม่ใช่คำแนะนำลงทุน",
    ]
    return "\n".join(lines)


def send_telegram(text: str, token: str, chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    max_len = 3500
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        add_len = len(line) + 1
        if current and current_len + add_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = add_len
        else:
            current.append(line)
            current_len += add_len
    if current:
        chunks.append("\n".join(current))

    for chunk in chunks:
        body = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=25) as response:
            response.read()


def send_telegram_document(path: str, token: str, chat_id: str, send_name: str | None = None) -> None:
    boundary = f"----stockscanner{uuid.uuid4().hex}"
    filename = send_name or os.path.basename(path)
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
    load_dotenv()
    args = parse_args()
    report = build_report(args.stock_csv, args.passing_csv, args.sector_csv, args.top)
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(report + "\n")
    print(report)

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token:
            raise ValueError("Missing TELEGRAM_BOT_TOKEN")
        if not chat_id:
            raise ValueError("Missing TELEGRAM_CHAT_ID")
        print("Telegram token:", mask_token(token))
        send_telegram(report, token, chat_id)
        attach_prefix = datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y%m%d")
        stock_df = pd.read_csv(args.stock_csv)
        if not stock_df.empty and "date" in stock_df.columns:
            raw_date = str(stock_df["date"].dropna().iloc[0])
            digits = re.sub(r"\D", "", raw_date)
            if len(digits) >= 8:
                attach_prefix = digits[:8]
        for csv_path in args.telegram_csv:
            attach_name = f"{attach_prefix}_{os.path.basename(csv_path)}"
            send_telegram_document(csv_path, token, chat_id, send_name=attach_name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
