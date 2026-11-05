from __future__ import annotations

import logging
import threading

import notify
import store
from config import sync_lookback_hours


logger = logging.getLogger(__name__)
RETRY_INTERVAL_SECONDS = 30
BATCH_SIZE = 100
MAX_FAILURES_PER_BATCH = 3
_wake = threading.Event()
_start_lock = threading.Lock()
_worker: threading.Thread | None = None


def _format_alert(message: store.InboundMessage) -> str:
    provider_id = message.provider_message_id or ''
    message_id = provider_id.rsplit(':', 1)[-1] if provider_id else '-'
    if store.body_is_incomplete(message.body):
        body = ('[Message text unavailable] Telnyx did not provide text. It may be redacted, '
                'media-only, or temporarily unavailable. Later syncs can retry the text lookup.')
    else:
        body = message.body
    return (
        '📩 SMS received\n'
        f'To: {message.to_number}\n'
        f'From: {message.from_number}\n'
        f'Received: {message.received_at}\n'
        f'Telnyx message id: {message_id}\n\n'
        f'{body}'
    )


def deliver_pending(limit: int = BATCH_SIZE) -> int:
    """Send queued alerts once; leave failures pending for a later retry."""
    # Retention also applies when polling is disabled and no new webhook or
    # Telegram command has arrived to trigger pruning.
    store.prune_old_messages(hours=sync_lookback_hours())
    delivered_count = 0
    failures = 0
    for _ in range(limit):
        claim = store.claim_next_notification()
        if claim is None:
            break
        try:
            delivered = notify.notify_owner(_format_alert(claim.message))
        except Exception as exc:
            # Never log the SMS body, chat ID, token, or exception URL.
            logger.error('Telegram alert failed: %s', type(exc).__name__)
            delivered = False
        if not store.finish_notification(claim, delivered=delivered, retry_seconds=RETRY_INTERVAL_SECONDS):
            logger.warning('Could not finalize a Telegram alert lease')
            break
        if delivered:
            delivered_count += 1
        else:
            # A problematic message should not block newer ones. Bound failures
            # in one pass to avoid hammering Telegram during an account outage.
            failures += 1
            if failures >= MAX_FAILURES_PER_BATCH:
                break
    return delivered_count


def wake_delivery_worker() -> None:
    _wake.set()


def delivery_worker_running() -> bool:
    return _worker is not None and _worker.is_alive()


def _delivery_loop() -> None:
    while True:
        _wake.clear()
        try:
            delivered = deliver_pending()
            if delivered > BATCH_SIZE - MAX_FAILURES_PER_BATCH:
                # A full batch was processed without hitting the failure cap.
                continue
        except Exception as exc:
            logger.error('Telegram alert worker error: %s', type(exc).__name__)
        _wake.wait(RETRY_INTERVAL_SECONDS)


def start_delivery_worker() -> None:
    global _worker
    with _start_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_delivery_loop, name='telegram-alert-delivery', daemon=True)
        _worker.start()
