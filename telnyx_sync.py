"""Fallback Telnyx inbox sync.

If Telnyx receives SMS but webhook delivery does not reach Railway, this module
can pull recent inbound messages from Telnyx Detail Records + Message API and
store/notify them. It is only for numbers owned by this Telnyx account.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import notify
import store
from config import load_config
from telnyx import TelnyxClient


@dataclass
class SyncResult:
    checked: int = 0
    stored: int = 0
    skipped: int = 0
    errors: list[str] | None = None


def sync_inbound_once(limit: int = 20, *, notify_new: bool = True) -> SyncResult:
    """Pull recent inbound messages from Telnyx and store unseen ones."""
    result = SyncResult(errors=[])
    cfg = load_config()
    client = TelnyxClient(cfg.telnyx_api_key, cfg.telnyx_base_url)
    records = client.list_messaging_detail_records(date_range="today", direction="inbound", limit=limit)

    # Telnyx returns newest first; process oldest first so Telegram order is sane.
    for rec in reversed(records):
        result.checked += 1
        message_id = rec.get("id")
        if not message_id:
            result.skipped += 1
            continue
        if store.has_provider_message(str(message_id)):
            result.skipped += 1
            continue

        try:
            msg = client.get_message(str(message_id)).get("data", {})
            from_number = ""
            if isinstance(msg.get("from"), dict):
                from_number = str(msg["from"].get("phone_number") or "")
            to_number = ""
            to_entries = msg.get("to") or []
            if isinstance(to_entries, list) and to_entries:
                to_number = str(to_entries[0].get("phone_number") or "")
            elif isinstance(to_entries, dict):
                to_number = str(to_entries.get("phone_number") or "")

            body = str(msg.get("text") or "")
            if not to_number:
                to_number = str(rec.get("cld") or "")
            if not from_number:
                from_number = str(rec.get("cli") or "")
            if not body:
                body = "(inbound SMS text unavailable)"

            row_id = store.save_message(
                to_number=to_number,
                from_number=from_number,
                body=body,
                provider_message_id=str(message_id),
            )
            if row_id:
                result.stored += 1
                if notify_new:
                    notify.notify_owner(
                        "📩 SMS received\n"
                        f"To: {to_number}\n"
                        f"From: {from_number}\n"
                        f"Telnyx message id: {message_id}\n\n"
                        f"{body}"
                    )
            else:
                result.skipped += 1
        except Exception as exc:  # keep syncing other messages
            result.errors.append(f"{message_id}: {exc}")

    return result


def start_polling_if_enabled() -> None:
    """Start a background polling fallback when TELNYX_POLLING_ENABLED=true."""
    enabled = os.getenv("TELNYX_POLLING_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return

    interval = int(os.getenv("TELNYX_POLL_INTERVAL_SECONDS", "30"))
    limit = int(os.getenv("TELNYX_POLL_LIMIT", "20"))

    def loop() -> None:
        # Small delay so app boots before first API call.
        time.sleep(5)
        while True:
            try:
                sync_inbound_once(limit=limit, notify_new=True)
            except Exception as exc:
                notify.notify_owner(f"⚠️ Telnyx polling sync error: {exc}")
            time.sleep(max(15, interval))

    thread = threading.Thread(target=loop, name="telnyx-inbound-poller", daemon=True)
    thread.start()
