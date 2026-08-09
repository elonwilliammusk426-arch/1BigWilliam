"""Set Telegram webhook and command menu to your deployed cloud URL.

Usage after deployment:
    PUBLIC_BASE_URL=https://1bigwilliam.up.railway.app python set_telegram_webhook.py

Or set PUBLIC_BASE_URL in .env temporarily and run it.
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

COMMANDS = [
    {"command": "start", "description": "Show bot menu"},
    {"command": "help", "description": "Show command list"},
    {"command": "latest", "description": "Latest inbound SMS across all numbers"},
    {"command": "recent", "description": "Recent SMS for one number"},
    {"command": "numbers", "description": "Numbers that received SMS"},
    {"command": "mynumbers", "description": "Owned/configured inbox numbers"},
    {"command": "accounts", "description": "Check Telnyx accounts/API keys"},
    {"command": "available", "description": "Search SMS-capable Telnyx numbers"},
    {"command": "checknum", "description": "Validate carrier/line type"},
    {"command": "syncsms", "description": "Pull missed SMS from Telnyx"},
    {"command": "testalert", "description": "Send test alert to group"},
    {"command": "whoami", "description": "Show your Telegram user ID"},
    {"command": "chatid", "description": "Show current chat/group ID"},
]


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not token:
        print("Missing TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 1
    if not base_url:
        print("Missing PUBLIC_BASE_URL, example: https://1bigwilliam.up.railway.app", file=sys.stderr)
        return 1

    webhook_url = f"{base_url}/telegram/webhook"
    webhook_resp = requests.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={"url": webhook_url, "drop_pending_updates": True},
        timeout=30,
    )
    print("setWebhook", webhook_resp.status_code, webhook_resp.text)

    commands_resp = requests.post(
        f"https://api.telegram.org/bot{token}/setMyCommands",
        json={"commands": COMMANDS},
        timeout=30,
    )
    print("setMyCommands", commands_resp.status_code, commands_resp.text)

    return 0 if (
        webhook_resp.ok
        and webhook_resp.json().get("ok")
        and commands_resp.ok
        and commands_resp.json().get("ok")
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
