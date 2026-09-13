from __future__ import annotations

import os
from typing import Any

import requests
from flask import Flask, jsonify, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import notify
import store
from config import load_config
from telnyx import TelnyxClient
from telnyx_sync import sync_inbound_once
from telnyx_webhook import parse_telnyx_inbound_event, verify_telnyx_signature

app = Flask(__name__)
store.init_db()

MAX_TELEGRAM_MESSAGE = 3900
HELP_TEXT = (
    '📩 Telnyx → Telegram bridge\n'
    'Owner-only inbox.\n\n'
    'Commands:\n'
    '• /latest [limit] — latest inbound SMS\n'
    '• /recent <number> [limit] — messages for one number\n'
    '• /numbers — numbers that received SMS\n'
    '• /mynumbers — configured Telnyx numbers\n'
    '• /available [country] [area] [limit] — search Telnyx SMS numbers\n'
    '• /syncsms [limit] — pull recent inbound SMS from your Telnyx accounts\n'
    '• /testalert — send a Telegram test alert\n'
    '• /whoami — your Telegram user id\n'
    '• /chatid — this chat/group id\n\n'
    'Example:\n'
    '/recent +12015550123 10'
)


def _telegram_token() -> str:
    return os.getenv('TELEGRAM_BOT_TOKEN', '').strip()


def _owner_id() -> int:
    return int(os.getenv('OWNER_TELEGRAM_ID', '0') or '0')


def _configured_numbers() -> list[str]:
    values = []
    for raw in [os.getenv('TELNYX_FROM_NUMBER', ''), os.getenv('TELNYX_NUMBERS', '')]:
        for part in raw.replace(';', ',').split(','):
            number = part.strip()
            if number and number not in values:
                values.append(number)
    return values


def _telegram_chunks(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ''
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ''
            for i in range(0, len(line), limit):
                chunks.append(line[i:i+limit])
            continue
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def send_telegram(chat_id: int | str, text: str) -> None:
    token = _telegram_token()
    if not token or not chat_id:
        return
    for chunk in _telegram_chunks(text):
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': chunk},
            timeout=15,
        )


def _authorized(user_id: int | None) -> bool:
    return bool(user_id and user_id == _owner_id())


def _parse_limit(value: str | None, default: int, maximum: int = 50) -> int:
    if not value:
        return default
    try:
        limit = int(value)
    except ValueError:
        return default
    return max(1, min(limit, maximum))


def _format_messages(messages: list[store.InboundMessage]) -> str:
    if not messages:
        return 'No inbound messages yet.'
    lines: list[str] = []
    for m in messages:
        lines.append(
            f'#{m.id} [{m.received_at}]\n'
            f'To: {m.to_number}\n'
            f'From: {m.from_number}\n'
            f'{m.body}\n'
        )
    text = '\n'.join(lines).strip()
    if len(text) > MAX_TELEGRAM_MESSAGE:
        return text[:MAX_TELEGRAM_MESSAGE] + '\n...truncated'
    return text


def _parse_available_args(args: list[str]) -> tuple[str, str | None, int]:
    country = 'US'
    area_code: str | None = None
    limit = 10
    if not args:
        return country, area_code, limit
    first = args[0].strip()
    if first.isdigit() and len(first) <= 4:
        area_code = first
        if len(args) > 1:
            limit = _parse_limit(args[1], default=10)
        return country, area_code, limit
    country = first.upper()
    if len(args) > 1:
        second = args[1].strip()
        if second.lower() not in {'any', 'none', '-'}:
            area_code = second
    if len(args) > 2:
        limit = _parse_limit(args[2], default=10)
    return country, area_code, limit


def _format_available_numbers(numbers: list[dict[str, Any]], country: str, area_code: str | None) -> str:
    if not numbers:
        area = f' area {area_code}' if area_code else ''
        return f'No SMS-capable Telnyx numbers found for {country}{area}.'
    lines = ['Available SMS-capable Telnyx numbers:']
    for item in numbers:
        phone = item.get('phone_number', '-')
        cost = item.get('cost_information') or {}
        monthly = cost.get('monthly_cost')
        upfront = cost.get('upfront_cost')
        currency = cost.get('currency') or ''
        cost_parts = []
        if monthly is not None:
            cost_parts.append(f'monthly {monthly} {currency}'.strip())
        if upfront is not None:
            cost_parts.append(f'upfront {upfront} {currency}'.strip())
        cost_text = ' | ' + '; '.join(cost_parts) if cost_parts else ''
        lines.append(f'• {phone}{cost_text}')
    text = '\n'.join(lines)
    if len(text) > MAX_TELEGRAM_MESSAGE:
        return text[:MAX_TELEGRAM_MESSAGE] + '\n...truncated'
    return text


@app.get('/')
def root():
    return jsonify({'ok': True, 'service': 'telnyx-telegram-bridge', 'health': '/health'}), 200


@app.get('/health')
def health():
    return jsonify({'ok': True, 'service': 'telnyx-telegram-bridge'}), 200


@app.post('/inbound/sms')
def inbound_sms():
    raw_body = request.get_data()
    if not verify_telnyx_signature(raw_body, request.headers):
        return jsonify({'ok': False, 'error': 'invalid_signature'}), 403

    event = request.get_json(silent=True) or {}
    data = event.get('data', {})
    event_type = data.get('event_type')

    parsed = parse_telnyx_inbound_event(event)
    if not parsed:
        if event_type in {'message.sent', 'message.finalized'}:
            return jsonify({'ok': True, 'ignored': event_type}), 200
        notify.notify_owner(
            '⚠️ Telnyx webhook received but inbound SMS could not be parsed.\n'
            f'Event type: {event_type or "unknown"}'
        )
        return jsonify({'ok': False, 'error': 'could_not_parse_payload'}), 400

    to_number, from_number, body, message_id = parsed
    row_id = store.save_message(to_number=to_number, from_number=from_number, body=body)
    notify.notify_owner(
        '📩 SMS received\n'
        f'To: {to_number}\n'
        f'From: {from_number}\n'
        f'Telnyx message id: {message_id or "-"}\n\n'
        f'{body}'
    )
    return jsonify({'ok': True, 'stored_id': row_id}), 200


@app.post('/telegram/webhook')
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get('message') or update.get('edited_message') or {}
    chat = message.get('chat') or {}
    user = message.get('from') or {}
    chat_id = chat.get('id')
    user_id = user.get('id')
    text = (message.get('text') or '').strip()

    if not chat_id or not text.startswith('/'):
        return jsonify({'ok': True, 'ignored': True}), 200

    parts = text.split()
    command = parts[0].split('@', 1)[0].lower()
    args = parts[1:]

    if command == '/whoami':
        send_telegram(chat_id, f'Your Telegram user id: {user_id or "-"}\nThis chat id: {chat_id}\nChat type: {chat.get("type", "-")}')
        return jsonify({'ok': True}), 200

    if command == '/chatid':
        send_telegram(chat_id, f'This chat id: {chat_id}\nChat type: {chat.get("type", "-")}')
        return jsonify({'ok': True}), 200

    if not _authorized(user_id):
        send_telegram(chat_id, '🚫 Unauthorized.')
        return jsonify({'ok': True, 'unauthorized': True}), 200

    if command in {'/start', '/help'}:
        send_telegram(chat_id, HELP_TEXT)
    elif command == '/latest':
        limit = _parse_limit(args[0] if args else None, default=10)
        send_telegram(chat_id, _format_messages(store.recent_all(limit=limit)))
    elif command == '/recent':
        if not args:
            send_telegram(chat_id, 'Usage: /recent +12015550123 [limit]')
        else:
            number = args[0]
            limit = _parse_limit(args[1] if len(args) > 1 else None, default=20)
            send_telegram(chat_id, _format_messages(store.recent_for_number(number, limit=limit)))
    elif command == '/numbers':
        numbers = store.distinct_to_numbers()
        send_telegram(chat_id, 'Numbers with inbound SMS:\n' + '\n'.join(numbers) if numbers else 'No numbers with messages yet.')
    elif command == '/mynumbers':
        numbers = _configured_numbers()
        send_telegram(chat_id, 'Configured Telnyx numbers:\n' + '\n'.join(numbers) if numbers else 'No configured numbers yet.')
    elif command == '/available':
        country, area_code, limit = _parse_available_args(args)
        try:
            cfg = load_config()
            client = TelnyxClient(cfg.telnyx_api_key, cfg.telnyx_base_url)
            numbers = client.search_available_numbers(country_code=country, area_code=area_code, limit=limit)
            send_telegram(chat_id, _format_available_numbers(numbers, country, area_code))
        except Exception as exc:
            send_telegram(chat_id, f'Could not search available numbers. Error: {exc}')
    elif command == '/syncsms':
        limit = _parse_limit(args[0] if args else None, default=20, maximum=100)
        try:
            res = sync_inbound_once(limit=limit, notify_new=True)
            summary = (
                f'Sync complete. Accounts: {res.accounts}, checked: {res.checked}, '
                f'stored new: {res.stored}, skipped: {res.skipped}.'
            )
            if res.errors:
                summary += '\nErrors: ' + '; '.join(res.errors[:3])
            send_telegram(chat_id, summary)
        except Exception as exc:
            send_telegram(chat_id, f'Sync failed: {exc}')
    elif command == '/testalert':
        notify.notify_owner('✅ Telegram alert test successful.')
        send_telegram(chat_id, 'Sent a test alert to TELEGRAM_ALERT_CHAT_ID.')
    else:
        send_telegram(chat_id, 'Unknown command. Send /help')

    return jsonify({'ok': True}), 200


if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port)
