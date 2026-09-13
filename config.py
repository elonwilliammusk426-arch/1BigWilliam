from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    telnyx_api_key: str
    telnyx_base_url: str
    telnyx_from_number: str
    telnyx_numbers: tuple[str, ...]
    telnyx_public_key: str
    telnyx_signature_tolerance: int
    telegram_bot_token: str
    owner_telegram_id: str
    telegram_alert_chat_id: str


def _split_numbers(raw: str) -> tuple[str, ...]:
    items: list[str] = []
    for part in raw.replace(';', ',').split(','):
        number = part.strip()
        if number and number not in items:
            items.append(number)
    return tuple(items)


def load_config(env_file: str = '.env') -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except ImportError:
        pass

    api_key = os.getenv('TELNYX_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('Missing TELNYX_API_KEY')

    from_number = os.getenv('TELNYX_FROM_NUMBER', '').strip()
    all_numbers = _split_numbers(','.join(filter(None, [from_number, os.getenv('TELNYX_NUMBERS', '')])))

    return Config(
        telnyx_api_key=api_key,
        telnyx_base_url=os.getenv('TELNYX_BASE_URL', 'https://api.telnyx.com/v2').rstrip('/'),
        telnyx_from_number=from_number,
        telnyx_numbers=all_numbers,
        telnyx_public_key=os.getenv('TELNYX_PUBLIC_KEY', '').strip(),
        telnyx_signature_tolerance=int(os.getenv('TELNYX_SIGNATURE_TOLERANCE', '300') or '300'),
        telegram_bot_token=os.getenv('TELEGRAM_BOT_TOKEN', '').strip(),
        owner_telegram_id=os.getenv('OWNER_TELEGRAM_ID', '').strip(),
        telegram_alert_chat_id=(os.getenv('TELEGRAM_ALERT_CHAT_ID', '').strip() or os.getenv('OWNER_TELEGRAM_ID', '').strip()),
    )
