from __future__ import annotations

import logging
import os

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


logger = logging.getLogger(__name__)
MAX_TELEGRAM_MESSAGE = 3900


def _chunks(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> list[str]:
    """Keep Telegram messages below its size limit, without dropping text."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ''
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ''
            chunks.extend(line[i:i + limit] for i in range(0, len(line), limit))
        elif len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def send_telegram(chat_id: int | str, text: str) -> bool:
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token or not chat_id:
        logger.warning('Telegram delivery is not configured: check the bot token and destination chat ID')
        return False

    for chunk in _chunks(text):
        try:
            response = requests.post(
                f'https://api.telegram.org/bot{token}/sendMessage',
                json={'chat_id': chat_id, 'text': chunk},
                timeout=10,
            )
        except requests.RequestException as exc:
            # Request exceptions can contain the URL, which contains the bot token.
            logger.warning('Telegram delivery failed: %s', type(exc).__name__)
            return False

        if not response.ok:
            logger.warning('Telegram rejected delivery: HTTP %s', response.status_code)
            return False
        try:
            payload = response.json()
        except ValueError:
            logger.warning('Telegram returned an invalid response')
            return False
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            logger.warning('Telegram did not confirm delivery')
            return False
    return True


def notify_owner(text: str) -> bool:
    chat_id = os.getenv('TELEGRAM_ALERT_CHAT_ID', '').strip() or os.getenv('OWNER_TELEGRAM_ID', '').strip()
    return send_telegram(chat_id, text)
