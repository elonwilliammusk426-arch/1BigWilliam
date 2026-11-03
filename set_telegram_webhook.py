from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlsplit

import requests

from telegram_security import configured_secret, secret_required, valid_secret

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _valid_base_url(value: str) -> bool:
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        # Accessing .port also validates its syntax and range.
        _ = parsed.port
        return bool(
            parsed.scheme == 'https' and parsed.hostname
            and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Register the Telegram webhook without discarding pending updates.')
    parser.add_argument(
        '--drop-pending-updates', action='store_true',
        help='Discard all queued Telegram updates (irreversible; disabled by default).',
    )
    args = parser.parse_args(argv)
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    base_url = os.getenv('PUBLIC_BASE_URL', '').strip().rstrip('/')
    secret = configured_secret()
    if not token:
        print('Missing TELEGRAM_BOT_TOKEN', file=sys.stderr)
        return 1
    if not _valid_base_url(base_url):
        print('PUBLIC_BASE_URL must be a valid HTTPS URL without credentials, query, or fragment.', file=sys.stderr)
        return 1
    if secret and not valid_secret(secret):
        print('TELEGRAM_WEBHOOK_SECRET must be 1–256 ASCII letters, digits, underscores or hyphens.', file=sys.stderr)
        return 1
    if not secret:
        if secret_required():
            print('TELEGRAM_WEBHOOK_SECRET is required but missing; webhook not changed.', file=sys.stderr)
            return 1
        print('Warning: Telegram webhook authentication is disabled. Configure TELEGRAM_WEBHOOK_SECRET.', file=sys.stderr)

    payload = {'url': f'{base_url}/telegram/webhook', 'drop_pending_updates': args.drop_pending_updates}
    if secret:
        payload['secret_token'] = secret
    try:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/setWebhook', json=payload, timeout=30,
        )
    except requests.RequestException as exc:
        # Exceptions can include the bot-token-bearing request URL.
        print(f'Telegram webhook registration failed: {type(exc).__name__}', file=sys.stderr)
        return 1
    try:
        result = response.json()
    except ValueError:
        result = None
    if not response.ok or not isinstance(result, dict) or result.get('ok') is not True:
        print(f'Telegram webhook registration failed (HTTP {response.status_code}).', file=sys.stderr)
        return 1
    print('Telegram webhook registered. Pending updates were ' + ('discarded.' if args.drop_pending_updates else 'preserved.'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
