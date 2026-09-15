from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import notify
import store
from config import load_config
from telnyx import TelnyxClient


@dataclass
class SyncResult:
    checked: int = 0
    stored: int = 0
    skipped: int = 0
    accounts: int = 0
    errors: list[str] | None = None


def telnyx_api_keys() -> list[str]:
    cfg = load_config()
    return list(cfg.telnyx_api_keys)


def telnyx_clients() -> list[tuple[str, TelnyxClient]]:
    cfg = load_config()
    clients: list[tuple[str, TelnyxClient]] = []
    for idx, key in enumerate(cfg.telnyx_api_keys, start=1):
        label = f"acct{idx}:{key[:10]}...{key[-4:]}"
        clients.append((label, TelnyxClient(key, cfg.telnyx_base_url)))
    return clients


def _lookback_hours() -> int:
    try:
        value = int(os.getenv('TELNYX_SYNC_LOOKBACK_HOURS', '48') or '48')
    except ValueError:
        value = 48
    return max(1, min(value, 24 * 30))


def _record_is_within_window(rec: dict, hours: int) -> bool:
    ts = (
        rec.get('created_at')
        or rec.get('start_time')
        or rec.get('record_date')
        or rec.get('occurred_at')
        or ''
    )
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except ValueError:
        return False
    return dt >= datetime.now(timezone.utc) - timedelta(hours=hours)


def sync_inbound_once(limit: int = 20, *, notify_new: bool = True) -> SyncResult:
    result = SyncResult(errors=[])
    date_range = os.getenv('TELNYX_SYNC_DATE_RANGE', '').strip() or None
    lookback_hours = _lookback_hours()
    store.prune_old_messages(hours=lookback_hours)

    for label, client in telnyx_clients():
        result.accounts += 1
        try:
            records = client.list_messaging_detail_records(
                date_range=date_range,
                direction='inbound',
                limit=limit,
            )
        except Exception as exc:
            result.errors.append(f'{label}: list records: {exc}')
            continue

        for rec in reversed(records):
            if not _record_is_within_window(rec, lookback_hours):
                result.skipped += 1
                continue
            result.checked += 1
            message_id = str(rec.get('id') or '').strip()
            if not message_id:
                result.skipped += 1
                continue

            provider_key = f'telnyx:{label}:{message_id}'
            if store.has_provider_message(provider_key):
                result.skipped += 1
                continue

            try:
                msg: dict = {}
                try:
                    msg = client.get_message(message_id).get('data', {}) or {}
                except Exception:
                    msg = {}

                from_number = ''
                if isinstance(msg.get('from'), dict):
                    from_number = str(msg['from'].get('phone_number') or '')
                to_number = ''
                to_entries = msg.get('to') or []
                if isinstance(to_entries, list) and to_entries:
                    to_number = str(to_entries[0].get('phone_number') or '')
                elif isinstance(to_entries, dict):
                    to_number = str(to_entries.get('phone_number') or '')

                body = str(msg.get('text') or '')
                if not to_number:
                    to_number = str(rec.get('cld') or '')
                if not from_number:
                    from_number = str(rec.get('cli') or '')
                if not body:
                    body = str(rec.get('text') or rec.get('body') or '')
                if not body:
                    body = '(inbound SMS text unavailable from Telnyx API)'

                if not to_number or not from_number:
                    result.errors.append(f'{label}:{message_id}: missing from/to fields')
                    result.skipped += 1
                    continue

                row_id = store.save_message(
                    to_number=to_number,
                    from_number=from_number,
                    body=body,
                    provider_message_id=provider_key,
                )
                if row_id:
                    result.stored += 1
                    if notify_new:
                        notify.notify_owner(
                            '📩 SMS synced\n'
                            f'To: {to_number}\n'
                            f'From: {from_number}\n'
                            f'Telnyx message id: {message_id}\n'
                            f'Account: {label}\n\n'
                            f'{body}'
                        )
                else:
                    result.skipped += 1
            except Exception as exc:
                result.errors.append(f'{label}:{message_id}: {exc}')

    return result
