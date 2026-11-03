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
    try:
        response = requests.get(f'https://api.telegram.org/bot{token}/getUpdates', timeout=20)
    except requests.RequestException as exc:
        # Do not print the exception: its URL may contain the bot token.
        print(f'Telegram request failed: {type(exc).__name__}', file=sys.stderr)
        return 1
    if response.status_code == 409:
        print('Telegram getUpdates is unavailable while a webhook is active (or another poller is running). '
              'With a working webhook, send /whoami or /chatid to the bot instead.', file=sys.stderr)
        return 1
    try:
        data = response.json()
    except ValueError:
        data = None
    if not response.ok or not isinstance(data, dict) or data.get('ok') is not True or not isinstance(data.get('result'), list):
        print(f'Telegram could not retrieve updates (HTTP {response.status_code}).', file=sys.stderr)
        return 1
    seen = set()
    for update in data['result']:
        if not isinstance(update, dict):
            continue
        msg = update.get('message') or update.get('channel_post') or {}
        if not isinstance(msg, dict):
            continue
        chat = msg.get('chat') or {}
        user = msg.get('from') or {}
        if not isinstance(chat, dict) or not isinstance(user, dict):
            continue
        chat_id = chat.get('id')
        if type(chat_id) is not int or not chat_id or chat_id in seen:
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
