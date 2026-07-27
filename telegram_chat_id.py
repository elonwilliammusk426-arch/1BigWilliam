"""Helper to find your Telegram user id and private group chat id.

Usage:
    TELEGRAM_BOT_TOKEN=123:ABC python telegram_chat_id.py

Then send a message in your private group, for example:
    /id

This script prints recent updates and their chat IDs.
"""
from __future__ import annotations

import os
import sys

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env or environment first.", file=sys.stderr)
        return 1

    resp = requests.get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        print(data)
        return 1

    updates = data.get("result", [])
    if not updates:
        print(
            "No updates yet. Send /start to the bot in private chat and send /id "
            "inside the private group, then run this again."
        )
        return 0

    seen = set()
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat", {})
        user = msg.get("from", {})
        chat_id = chat.get("id")
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        print("---")
        print(f"chat_id: {chat_id}")
        print(f"chat_type: {chat.get('type')}")
        print(f"chat_title: {chat.get('title') or chat.get('username') or ''}")
        if user:
            print(f"from_user_id: {user.get('id')}")
            print(f"from_username: {user.get('username') or ''}")

    print("---")
    print("Use your personal from_user_id as OWNER_TELEGRAM_ID.")
    print("Use the private group chat_id as TELEGRAM_ALERT_CHAT_ID.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
