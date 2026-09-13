from __future__ import annotations

import os

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def notify_owner(text: str) -> None:
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.getenv('TELEGRAM_ALERT_CHAT_ID', '').strip() or os.getenv('OWNER_TELEGRAM_ID', '').strip()
    if not token or not chat_id:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text},
            timeout=10,
        )
    except requests.RequestException:
        pass
