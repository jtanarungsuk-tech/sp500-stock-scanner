#!/usr/bin/env python3
"""Print Telegram chat IDs from recent bot updates."""

from __future__ import annotations

import argparse
import json
import urllib.request


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Get Telegram chat ID from bot updates.")
    parser.add_argument("--token", required=True, help="Telegram bot token from BotFather.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url = f"https://api.telegram.org/bot{args.token}/getUpdates"
    with urllib.request.urlopen(url, timeout=25) as response:
        data = json.loads(response.read().decode("utf-8"))

    seen = set()
    for update in data.get("result", []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or chat_id in seen:
            continue
        seen.add(chat_id)
        title = chat.get("title") or chat.get("username") or chat.get("first_name") or "unknown"
        print(f"{chat_id}\t{title}")

    if not seen:
        print("No chat IDs found. Send a message to your bot in Telegram, then run again.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
