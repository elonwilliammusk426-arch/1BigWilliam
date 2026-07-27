"""Send Telegram alerts when SMS lands on one of your numbers.

`TELEGRAM_ALERT_CHAT_ID` can be your private user chat id or a private group id.
If it is not set, we fall back to `OWNER_TELEGRAM_ID`.
"""
from __future__ import annotations

import os

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def notify_owner(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "") or os.getenv(
        "OWNER_TELEGRAM_ID", ""
    )
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
    except requests.RequestException:
        # Best-effort notification; never block the webhook on it.
        pass
