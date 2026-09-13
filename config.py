from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    telnyx_api_key: str
    telnyx_api_keys: tuple[str, ...]
    telnyx_base_url: str
    telnyx_from_number: str
    telnyx_numbers: tuple[str, ...]
    telnyx_public_key: str
    telnyx_public_keys: tuple[str, ...]
    telnyx_signature_tolerance: int
    telegram_bot_token: str
    owner_telegram_id: str
    telegram_alert_chat_id: str


def _split_csv(raw: str) -> tuple[str, ...]:
    items: list[str] = []
    for part in raw.replace(';', ',').replace('\n', ',').split(','):
        value = part.strip()
        if value and value not in items:
            items.append(value)
    return tuple(items)


def load_config(env_file: str = '.env') -> Config:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
    except ImportError:
        pass

    api_keys = _split_csv(','.join(filter(None, [
        os.getenv('TELNYX_API_KEYS', ''),
        os.getenv('TELNYX_API_KEY', ''),
        os.getenv('TELNYX_EXTRA_API_KEYS', ''),
    ])))
    if not api_keys:
        raise RuntimeError('Missing TELNYX_API_KEY')

    from_number = os.getenv('TELNYX_FROM_NUMBER', '').strip()
    all_numbers = _split_csv(','.join(filter(None, [from_number, os.getenv('TELNYX_NUMBERS', '')])))
    public_keys = _split_csv(','.join(filter(None, [
        os.getenv('TELNYX_PUBLIC_KEYS', ''),
        os.getenv('TELNYX_PUBLIC_KEY', ''),
        os.getenv('TELNYX_EXTRA_PUBLIC_KEYS', ''),
    ])))

    return Config(
        telnyx_api_key=api_keys[0],
        telnyx_api_keys=api_keys,
        telnyx_base_url=os.getenv('TELNYX_BASE_URL', 'https://api.telnyx.com/v2').rstrip('/'),
        telnyx_from_number=from_number,
        telnyx_numbers=all_numbers,
        telnyx_public_key=public_keys[0] if public_keys else '',
        telnyx_public_keys=public_keys,
        telnyx_signature_tolerance=int(os.getenv('TELNYX_SIGNATURE_TOLERANCE', '300') or '300'),
        telegram_bot_token=os.getenv('TELEGRAM_BOT_TOKEN', '').strip(),
        owner_telegram_id=os.getenv('OWNER_TELEGRAM_ID', '').strip(),
        telegram_alert_chat_id=(os.getenv('TELEGRAM_ALERT_CHAT_ID', '').strip() or os.getenv('OWNER_TELEGRAM_ID', '').strip()),
    )
