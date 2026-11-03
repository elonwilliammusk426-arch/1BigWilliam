from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import alerts
import notify
import store
import telegram_security
from config import configured_telnyx_api_keys, load_config, sync_lookback_hours
from telnyx import MAX_DETAIL_RECORDS, TelnyxClient
from telnyx_sync import _safe_error, start_polling_if_enabled, sync_inbound_once
from telnyx_webhook import (
    _configured_public_keys, inbound_received_at, parse_telnyx_inbound_event, signature_required,
    verify_telnyx_signature,
)

app = Flask(__name__)
# Avoid accepting unbounded request bodies from either public webhook.
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
if not telegram_security.configured_secret():
    if telegram_security.secret_required():
        app.logger.error('Telegram webhook secret is required but not configured; requests will be rejected')
    else:
        app.logger.warning('Telegram webhook authentication is disabled; configure TELEGRAM_WEBHOOK_SECRET')
elif not telegram_security.valid_secret(telegram_security.configured_secret()):
    app.logger.error('Invalid TELEGRAM_WEBHOOK_SECRET; Telegram webhook requests will be rejected')
if not _configured_public_keys():
    if signature_required():
        app.logger.error('Telnyx signature verification is required but no public key is configured')
    else:
        app.logger.warning('Telnyx signature verification is disabled; configure a public key')
store.init_db()
alerts.start_delivery_worker()
start_polling_if_enabled()

_sync_lock = threading.Lock()

MAX_TELEGRAM_MESSAGE = notify.MAX_TELEGRAM_MESSAGE
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
    '• /status — configuration and pending-alert diagnostics\n'
    '• /whoami — your Telegram user id\n'
    '• /chatid — this chat/group id\n\n'
    'Example:\n'
    '/recent +12015550123 10'
)


def _lookback_hours() -> int:
    return sync_lookback_hours()


def _configured_numbers() -> list[str]:
    values = []
    for raw in [os.getenv('TELNYX_FROM_NUMBER', ''), os.getenv('TELNYX_NUMBERS', '')]:
        for part in raw.replace(';', ',').split(','):
            number = part.strip()
            if number and number not in values:
                values.append(number)
    return values


def send_telegram(chat_id: int | str, text: str) -> bool:
    return notify.send_telegram(chat_id, text)


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
        return f'No inbound messages in the last {_lookback_hours()} hours.'
    lines: list[str] = []
    for m in messages:
        body = m.body or '(Message text unavailable; it may be redacted or media-only.)'
        lines.append(
            f'#{m.id} [{m.received_at}]\n'
            f'To: {m.to_number}\n'
            f'From: {m.from_number}\n'
            f'{body}\n'
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


def _syncsms_summary(res) -> str:
    text = (
        f'Sync finished for the last {_lookback_hours()} hours. Accounts: {res.accounts}, '
        f'records returned: {res.returned}, checked: {res.checked}, '
        f'stored/updated: {res.stored}, skipped: {res.skipped}, '
        f'alerts pending: {store.pending_notification_count()}.'
    )
    if res.missing_ids:
        text += f'\nRecords missing an id/uuid: {res.missing_ids}.'
    if res.missing_timestamps:
        text += f'\nRecords missing a usable timestamp: {res.missing_timestamps}.'
    if res.unavailable_text:
        text += (f'\nMessage text unavailable: {res.unavailable_text}. '
                 'Check Telnyx redaction or whether the message is media-only.')
    if res.limited_accounts:
        text += (f'\nAccounts that reached the requested record limit: {res.limited_accounts}. '
                 f'Older records may be omitted; you can request up to {MAX_DETAIL_RECORDS} per account.')
    if res.errors:
        text += '\nErrors: ' + '; '.join(res.errors[:3])
    if not res.returned and not res.errors:
        text += '\nTelnyx returned no inbound records for the configured accounts and date range.'
    return text


def _status_text() -> str:
    secret = telegram_security.configured_secret()
    if not secret:
        telegram_auth = ('required but missing; requests rejected' if telegram_security.secret_required()
                         else 'disabled (legacy mode)')
    elif not telegram_security.valid_secret(secret):
        telegram_auth = 'invalid secret; requests rejected'
    else:
        telegram_auth = 'configured (webhook must be registered with the same secret)'

    if _configured_public_keys():
        telnyx_auth = 'configured'
        try:
            if int(os.getenv('TELNYX_SIGNATURE_TOLERANCE', '300') or '300') <= 0:
                raise ValueError
        except ValueError:
            telnyx_auth = 'invalid signature tolerance; requests rejected'
    elif signature_required():
        telnyx_auth = 'required but public key missing; requests rejected'
    else:
        telnyx_auth = 'disabled (legacy mode)'

    polling = os.getenv('TELNYX_POLLING_ENABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    destination = bool(os.getenv('TELEGRAM_ALERT_CHAT_ID', '').strip() or telegram_security.owner_id())
    db_path = 'custom path (verify a persistent volume is mounted)' if os.getenv('INBOUND_DB_PATH', '').strip() else 'default local path (may be ephemeral)'
    return (
        'Bridge status (configuration only; not a live provider check):\n'
        f'Telnyx accounts configured: {len(configured_telnyx_api_keys())}\n'
        f'Telnyx webhook signature: {telnyx_auth}\n'
        f'Telegram webhook secret: {telegram_auth}\n'
        f'Telegram bot token: {"configured" if os.getenv("TELEGRAM_BOT_TOKEN", "").strip() else "missing"}\n'
        f'Owner ID: {"configured" if telegram_security.owner_id() else "missing or invalid"}\n'
        f'Alert destination: {"configured" if destination else "missing"}\n'
        f'Polling: {"enabled" if polling else "disabled"}\n'
        f'Pending alerts: {store.pending_notification_count()}\n'
        f'Database: {db_path}'
    )


def _start_background_syncsms(chat_id: int | str, limit: int) -> bool:
    if not _sync_lock.acquire(blocking=False):
        return False

    def worker() -> None:
        try:
            res = sync_inbound_once(limit=limit, notify_new=True)
            send_telegram(chat_id, _syncsms_summary(res))
        except Exception as exc:
            reason = 'Missing TELNYX_API_KEY' if str(exc) == 'Missing TELNYX_API_KEY' else type(exc).__name__
            send_telegram(chat_id, f'Sync failed: {reason}. Check Railway deploy logs.')
        finally:
            _sync_lock.release()

    try:
        threading.Thread(target=worker, name='syncsms-worker', daemon=True).start()
    except Exception:
        _sync_lock.release()
        raise
    return True


@app.get('/')
def root():
    return jsonify({'ok': True, 'service': 'telnyx-telegram-bridge', 'health': '/health', 'sync_window_hours': _lookback_hours()}), 200


@app.get('/health')
def health():
    return jsonify({'ok': True, 'service': 'telnyx-telegram-bridge', 'sync_window_hours': _lookback_hours()}), 200


@app.post('/inbound/sms')
def inbound_sms():
    raw_body = request.get_data()
    if not verify_telnyx_signature(raw_body, request.headers):
        app.logger.warning('Telnyx inbound webhook rejected: invalid signature')
        return jsonify({'ok': False, 'error': 'invalid_signature'}), 403

    event = request.get_json(silent=True) or {}
    data = event.get('data', {}) if isinstance(event, dict) else {}
    event_type = data.get('event_type') if isinstance(data, dict) else None
    if event_type and event_type != 'message.received':
        return jsonify({'ok': True, 'ignored': True}), 200

    parsed = parse_telnyx_inbound_event(event)
    if not parsed:
        app.logger.warning('Telnyx inbound webhook could not be parsed')
        return jsonify({'ok': False, 'error': 'could_not_parse_payload'}), 400

    store.prune_old_messages(hours=_lookback_hours())
    to_number, from_number, body, message_id = parsed
    row_id, created = store.save_telnyx_message(
        to_number=to_number, from_number=from_number, body=body,
        message_id=message_id, notify=True, received_at=inbound_received_at(event),
        # Even an old provider event is a new delivery to the bridge. Retain it
        # from arrival so it can be alerted instead of being pruned immediately.
        retention_at=datetime.now(timezone.utc),
    )
    alerts.wake_delivery_worker()
    # Acknowledge quickly after the SMS and its alert have been stored. Telegram
    # delivery happens asynchronously and is retried if it fails.
    return jsonify({'ok': True, 'stored_id': row_id, 'duplicate': not created}), 200


@app.post('/telegram/webhook')
def telegram_webhook():
    if not telegram_security.verify_webhook(request.headers):
        app.logger.warning('Telegram webhook rejected: invalid secret')
        return jsonify({'ok': False, 'error': 'invalid_secret'}), 403

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        return jsonify({'ok': True, 'ignored': True}), 200
    message = update.get('message') or update.get('edited_message') or {}
    if not isinstance(message, dict):
        return jsonify({'ok': True, 'ignored': True}), 200
    chat = message.get('chat') or {}
    user = message.get('from') or {}
    if not isinstance(chat, dict) or not isinstance(user, dict):
        return jsonify({'ok': True, 'ignored': True}), 200
    chat_id = chat.get('id')
    user_id = user.get('id')
    text = message.get('text')
    if type(chat_id) is not int or not chat_id or not isinstance(text, str):
        return jsonify({'ok': True, 'ignored': True}), 200
    text = text.strip()
    if not text.startswith('/'):
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

    if not telegram_security.authorized(user_id, chat_id):
        # Do not send replies to arbitrary chats supplied by a forged update.
        if chat_id in telegram_security.allowed_command_chats():
            send_telegram(chat_id, '🚫 Unauthorized.')
        return jsonify({'ok': True, 'unauthorized': True}), 200

    store.prune_old_messages(hours=_lookback_hours())

    if command in {'/start', '/help'}:
        send_telegram(chat_id, HELP_TEXT + f'\n\nWindow: only the last {_lookback_hours()} hours are shown/synced.')
    elif command == '/latest':
        limit = _parse_limit(args[0] if args else None, default=10)
        send_telegram(chat_id, _format_messages(store.recent_all(limit=limit, max_age_hours=_lookback_hours())))
    elif command == '/recent':
        if not args:
            send_telegram(chat_id, 'Usage: /recent +12015550123 [limit]')
        else:
            number = args[0]
            limit = _parse_limit(args[1] if len(args) > 1 else None, default=20)
            send_telegram(chat_id, _format_messages(store.recent_for_number(number, limit=limit, max_age_hours=_lookback_hours())))
    elif command == '/numbers':
        numbers = store.distinct_to_numbers(max_age_hours=_lookback_hours())
        send_telegram(chat_id, 'Numbers with inbound SMS in the current window:\n' + '\n'.join(numbers) if numbers else f'No numbers with messages in the last {_lookback_hours()} hours.')
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
            send_telegram(chat_id, f'Could not search available numbers. Error: {_safe_error(exc)}')
    elif command == '/syncsms':
        limit = _parse_limit(args[0] if args else None, default=20, maximum=MAX_DETAIL_RECORDS)
        started = _start_background_syncsms(chat_id, limit)
        if started:
            send_telegram(chat_id, f'Sync started (or queued behind polling) for up to {limit} records per account. I will send the result when it finishes.')
        else:
            send_telegram(chat_id, 'A sync is already running. Wait for the result message, then try again if needed.')
    elif command == '/status':
        send_telegram(chat_id, _status_text())
    elif command == '/testalert':
        sent = notify.notify_owner('✅ Telegram alert test successful.')
        if sent:
            send_telegram(chat_id, 'Telegram accepted the test alert for the configured alert chat.')
        else:
            send_telegram(chat_id, 'Test alert failed. Check TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID, bot access to that chat, and Railway deploy logs.')
    else:
        send_telegram(chat_id, 'Unknown command. Send /help')

    return jsonify({'ok': True}), 200


if __name__ == '__main__':
    port = int(os.getenv('PORT', '5000'))
    app.run(host='0.0.0.0', port=port)
