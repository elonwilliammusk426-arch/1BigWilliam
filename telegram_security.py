from __future__ import annotations

import hmac
import os
import re


_SECRET_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,256}$')


def configured_secret() -> str:
    return os.getenv('TELEGRAM_WEBHOOK_SECRET', '')


def valid_secret(secret: str) -> bool:
    return bool(_SECRET_PATTERN.fullmatch(secret))


def secret_required() -> bool:
    value = os.getenv('TELEGRAM_REQUIRE_SECRET', '').strip().lower()
    # Unknown non-empty values fail closed instead of disabling the check.
    return value not in {'', '0', 'false', 'no', 'off'}


def verify_webhook(headers) -> bool:
    """Verify Telegram's secret header when one is configured.

    An empty secret keeps legacy deployments working unless explicitly required.
    Production deployments should configure a secret and register the webhook.
    """
    secret = configured_secret()
    if not secret:
        return not secret_required()
    if not valid_secret(secret):
        return False
    supplied = headers.get('X-Telegram-Bot-Api-Secret-Token', '')
    if not isinstance(supplied, str):
        return False
    return hmac.compare_digest(secret.encode('ascii'), supplied.encode('utf-8'))


def owner_id() -> int:
    try:
        value = int(os.getenv('OWNER_TELEGRAM_ID', '').strip())
    except ValueError:
        return 0
    return value if value > 0 else 0


def allowed_command_chats() -> set[int]:
    """Default to the owner's private chat and configured alert chat."""
    allowed = {owner_id()} - {0}
    raw = ','.join((os.getenv('TELEGRAM_ALERT_CHAT_ID', ''), os.getenv('TELEGRAM_COMMAND_CHAT_IDS', '')))
    for part in raw.replace(';', ',').replace('\n', ',').split(','):
        try:
            chat_id = int(part.strip())
        except ValueError:
            continue
        if chat_id:
            allowed.add(chat_id)
    return allowed


def authorized(user_id: object, chat_id: object) -> bool:
    # Telegram sends numeric IDs. Reject booleans and strings in forged JSON.
    return (
        type(user_id) is int
        and type(chat_id) is int
        and user_id == owner_id()
        and user_id != 0
        and chat_id in allowed_command_chats()
    )
