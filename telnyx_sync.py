from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import alerts
import store
from config import load_config, sync_lookback_hours
from telnyx import MAX_DETAIL_RECORDS, PaginationLimitError, PartialDetailRecordsError, TelnyxClient


logger = logging.getLogger(__name__)
_sync_guard = threading.Lock()
MAX_ACCOUNT_WORKERS = 4


@dataclass
class SyncResult:
    returned: int = 0
    checked: int = 0
    stored: int = 0
    skipped: int = 0
    accounts: int = 0
    missing_ids: int = 0
    missing_timestamps: int = 0
    unavailable_text: int = 0
    limited_accounts: int = 0
    errors: list[str] = field(default_factory=list)


def telnyx_api_keys() -> list[str]:
    cfg = load_config()
    return list(cfg.telnyx_api_keys)


def telnyx_clients() -> list[tuple[str, TelnyxClient]]:
    cfg = load_config()
    return [
        (f'acct{idx}', TelnyxClient(key, cfg.telnyx_base_url))
        for idx, key in enumerate(cfg.telnyx_api_keys, start=1)
    ]


def _lookback_hours() -> int:
    return sync_lookback_hours()


def _record_timestamp(rec: dict[str, Any]) -> datetime | None:
    ts = (
        rec.get('created_at')
        or rec.get('received_at')
        or rec.get('start_time')
        or rec.get('record_date')
        or rec.get('occurred_at')
        or ''
    )
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _record_is_within_window(rec: dict[str, Any], hours: int) -> bool:
    dt = _record_timestamp(rec)
    return bool(dt and dt >= datetime.now(timezone.utc) - timedelta(hours=hours))


def _phone(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get('phone_number')
    return value.strip() if isinstance(value, str) else ''


def _first_recipient(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else None
    return _phone(value)


def _text(*values: Any) -> str:
    return next((value for value in values if isinstance(value, str) and value), '')


def _safe_error(exc: Exception) -> str:
    # Do not include response URLs, API keys, phone numbers or message content.
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f'HTTP {exc.response.status_code}'
    if isinstance(exc, PaginationLimitError):
        return 'pagination safety limit reached'
    if isinstance(exc, requests.Timeout):
        return 'request timed out'
    if isinstance(exc, requests.ConnectionError):
        return 'connection failed'
    return type(exc).__name__


def _add_error(result: SyncResult, error: str) -> None:
    if error not in result.errors:
        result.errors.append(error)


def _account_unavailable(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status in (401, 403, 429) or status >= 500
    return False


def _sync_account(
    label: str, client: TelnyxClient, *, date_range: str, cutoff: datetime,
    limit: int, notify_new: bool,
) -> SyncResult:
    result = SyncResult(accounts=1)
    try:
        records = client.list_messaging_detail_records(
            date_range=date_range,
            direction='inbound',
            limit=limit,
        )
    except PartialDetailRecordsError as exc:
        records = exc.records
        _add_error(result, f'{label}: additional pages unavailable ({_safe_error(exc.cause)})')
    except Exception as exc:
        _add_error(result, f'{label}: list records failed ({_safe_error(exc)})')
        return result

    result.returned += len(records)
    if len(records) >= limit:
        result.limited_accounts += 1
    # Queue newer messages first so a large recovery sync does not delay the
    # latest time-sensitive codes behind older provider lookups.
    account_detail_error: Exception | None = None
    for rec in records:
        if not isinstance(rec, dict):
            result.skipped += 1
            _add_error(result, f'{label}: invalid detail record')
            continue
        direction = rec.get('direction')
        if isinstance(direction, str) and direction.lower() != 'inbound':
            result.skipped += 1
            continue
        received_at = _record_timestamp(rec)
        if received_at is None:
            result.missing_timestamps += 1
            result.skipped += 1
            continue
        if received_at < cutoff:
            result.skipped += 1
            continue
        result.checked += 1
        # Current messaging detail records use uuid as the message ID;
        # older responses used id. Prefer uuid when both are present.
        message_id = str(rec.get('uuid') or rec.get('id') or '').strip()
        if not message_id:
            result.missing_ids += 1
            result.skipped += 1
            continue
        try:
            already_complete = store.has_complete_telnyx_message(message_id)
        except Exception as exc:
            result.skipped += 1
            _add_error(result, f'{label}: could not check stored message ({_safe_error(exc)})')
            continue
        if already_complete:
            result.skipped += 1
            continue

        detail_error: Exception | None = None
        msg: dict[str, Any] = {}
        if not (_text(rec.get('text'), rec.get('body')) and _phone(rec.get('cli')) and _phone(rec.get('cld'))):
            if account_detail_error:
                detail_error = account_detail_error
            else:
                try:
                    response = client.get_message(message_id)
                    msg = response.get('data') if isinstance(response, dict) else None
                    if not isinstance(msg, dict):
                        raise ValueError('Invalid Telnyx message response')
                except Exception as exc:
                    detail_error = exc
                    if _account_unavailable(exc):
                        # Avoid another full timeout for every record, while
                        # still processing any records that contain the body.
                        account_detail_error = exc
                    msg = {}

        if isinstance(msg.get('direction'), str) and msg['direction'].lower() != 'inbound':
            result.skipped += 1
            continue
        from_number = _phone(msg.get('from')) or _phone(rec.get('cli'))
        to_number = _first_recipient(msg.get('to')) or _phone(rec.get('cld'))
        body = _text(msg.get('text'), rec.get('text'), rec.get('body'))
        if not body:
            result.skipped += 1
            if detail_error:
                _add_error(result, f'{label}: message details unavailable ({_safe_error(detail_error)})')
            else:
                result.unavailable_text += 1
            # Do not mark a missing/redacted body as processed: a later
            # webhook or successful lookup may contain the actual SMS.
            continue
        if not to_number or not from_number:
            result.skipped += 1
            _add_error(result, f'{label}: message is missing a sender or recipient')
            continue

        try:
            _, created = store.save_telnyx_message(
                to_number=to_number,
                from_number=from_number,
                body=body,
                message_id=message_id,
                notify=notify_new,
                received_at=received_at,
            )
        except Exception as exc:
            result.skipped += 1
            _add_error(result, f'{label}: could not save message ({_safe_error(exc)})')
            continue
        if created:
            result.stored += 1
            if notify_new:
                alerts.wake_delivery_worker()
        else:
            result.skipped += 1

    return result


def _sync_inbound_once_unlocked(limit: int, notify_new: bool) -> SyncResult:
    result = SyncResult()
    lookback_hours = _lookback_hours()
    # Telnyx supports last_N_days. Include a buffer day across UTC date
    # boundaries, then apply the exact lookback below.
    days = min(31, (lookback_hours + 23) // 24 + 1)
    date_range = os.getenv('TELNYX_SYNC_DATE_RANGE', '').strip() or f'last_{days}_days'
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    store.prune_old_messages(hours=lookback_hours)
    alerts.wake_delivery_worker()
    clients = telnyx_clients()

    def run_account(label: str, client: TelnyxClient) -> SyncResult:
        try:
            return _sync_account(
                label, client, date_range=date_range, cutoff=cutoff, limit=limit, notify_new=notify_new,
            )
        except Exception as exc:
            return SyncResult(accounts=1, errors=[f'{label}: sync failed ({_safe_error(exc)})'])

    if len(clients) <= 1:
        results = [run_account(label, client) for label, client in clients]
    else:
        # Each account owns its HTTP session. Limit concurrency so one slow
        # account does not delay every other one or overwhelm Telnyx/SQLite.
        with ThreadPoolExecutor(max_workers=min(MAX_ACCOUNT_WORKERS, len(clients))) as executor:
            futures = [executor.submit(run_account, label, client) for label, client in clients]
            results = [future.result() for future in futures]

    for account in results:
        for name in ('returned', 'checked', 'stored', 'skipped', 'accounts', 'missing_ids',
                     'missing_timestamps', 'unavailable_text', 'limited_accounts'):
            setattr(result, name, getattr(result, name) + getattr(account, name))
        for error in account.errors:
            _add_error(result, error)
    return result


def sync_inbound_once(limit: int = 20, *, notify_new: bool = True) -> SyncResult:
    # Serialize manual and background syncs within a process. Webhooks can
    # still write concurrently; SQLite deduplicates them transactionally.
    with _sync_guard:
        return _sync_inbound_once_unlocked(max(1, min(int(limit), MAX_DETAIL_RECORDS)), notify_new)


def start_polling_if_enabled() -> None:
    enabled = os.getenv('TELNYX_POLLING_ENABLED', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if not enabled:
        return

    try:
        interval = int(os.getenv('TELNYX_POLL_INTERVAL_SECONDS', '60') or '60')
    except ValueError:
        interval = 60
    try:
        limit = int(os.getenv('TELNYX_POLL_LIMIT', '20') or '20')
    except ValueError:
        limit = 20

    def loop() -> None:
        time.sleep(5)
        while True:
            try:
                if _sync_guard.locked():
                    time.sleep(max(15, interval))
                    continue
                result = sync_inbound_once(limit=limit, notify_new=True)
                if (result.errors or result.missing_ids or result.missing_timestamps
                        or result.unavailable_text or result.limited_accounts):
                    logger.warning(
                        'Background Telnyx sync: errors=%s, missing_ids=%s, missing_timestamps=%s, '
                        'unavailable_text=%s, accounts_at_record_limit=%s',
                        '; '.join(result.errors[:3]) or 'none', result.missing_ids,
                        result.missing_timestamps, result.unavailable_text, result.limited_accounts,
                    )
            except Exception as exc:
                logger.error('Background Telnyx sync failed: %s', _safe_error(exc))
            time.sleep(max(15, interval))

    threading.Thread(target=loop, name='telnyx-poll-sync', daemon=True).start()
