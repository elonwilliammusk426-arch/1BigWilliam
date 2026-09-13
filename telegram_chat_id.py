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
    if not token:
        print('Set TELEGRAM_BOT_TOKEN first.', file=sys.stderr)
        return 1
    resp = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get('ok'):
        print(data)
        return 1
    seen = set()
    for upd in data.get('result', []):
        msg = upd.get('message') or upd.get('channel_post') or {}
        chat = msg.get('chat', {})
        user = msg.get('from', {})
        chat_id = chat.get('id')
        if not chat_id or chat_id in seen:
            continue
        seen.add(chat_id)
        print('---')
        print('chat_id:', chat_id)
        print('chat_type:', chat.get('type'))
        print('chat_title:', chat.get('title') or chat.get('username') or '')
        if user:
            print('from_user_id:', user.get('id'))
            print('from_username:', user.get('username') or '')
    print('---')
    print('Use from_user_id as OWNER_TELEGRAM_ID and the private group chat_id as TELEGRAM_ALERT_CHAT_ID.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
