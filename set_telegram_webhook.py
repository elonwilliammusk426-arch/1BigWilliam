from __future__ import annotations

import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def main() -> int:
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    base_url = os.getenv('PUBLIC_BASE_URL', '').strip().rstrip('/')
    if not token:
        print('Missing TELEGRAM_BOT_TOKEN', file=sys.stderr)
        return 1
    if not base_url:
        print('Missing PUBLIC_BASE_URL, example: https://your-app.up.railway.app', file=sys.stderr)
        return 1

    webhook_url = f'{base_url}/telegram/webhook'
    resp = requests.post(
        f'https://api.telegram.org/bot{token}/setWebhook',
        json={'url': webhook_url, 'drop_pending_updates': True},
        timeout=30,
    )
    print(resp.status_code, resp.text)
    return 0 if resp.ok and resp.json().get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
