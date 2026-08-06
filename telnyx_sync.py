"""Fallback Telnyx inbox sync.

If Telnyx receives SMS but webhook delivery does not reach Railway, this module
can pull recent inbound messages from Telnyx Detail Records + Message API and
store/notify them.

Supports one or more Telnyx accounts:
    TELNYX_API_KEY=primary_key
    TELNYX_EXTRA_API_KEYS=second_key,third_key
or:
    TELNYX_API_KEYS=primary_key,second_key,third_key
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
    accounts: int = 0
    errors: list[str] | None = None


def _split_env_list(value: str) -> list[str]:
    """Split comma/semicolon/newline separated env values."""
    items: list[str] = []
    for chunk in value.replace(";", ",").replace("\n", ",").split(","):
        item = chunk.strip()
        if item and item not in items:
            items.append(item)
    return items


def telnyx_api_keys() -> list[str]:
    """Return all configured Telnyx API keys.

    Backward-compatible:
      - TELNYX_API_KEY is still the primary key.
      - TELNYX_EXTRA_API_KEYS adds more accounts.
      - TELNYX_API_KEYS can hold the full comma-separated list.
    """
    keys: list[str] = []
    for raw in (
        os.getenv("TELNYX_API_KEYS", ""),
        os.getenv("TELNYX_API_KEY", ""),
        os.getenv("TELNYX_EXTRA_API_KEYS", ""),
    ):
        for key in _split_env_list(raw):
            if key not in keys:
                keys.append(key)
    return keys


def telnyx_clients(base_url: str | None = None) -> list[tuple[str, TelnyxClient]]:
    cfg = load_config()
    url = base_url or cfg.telnyx_base_url
    clients: list[tuple[str, TelnyxClient]] = []
    for idx, key in enumerate(telnyx_api_keys(), start=1):
        label = f"acct{idx}:{key[:10]}...{key[-4:]}"
        clients.append((label, TelnyxClient(key, url)))
    return clients


def list_all_owned_numbers() -> tuple[list[str], list[str]]:
    """List owned numbers across all configured Telnyx accounts."""
    numbers: list[str] = []
    errors: list[str] = []
    for label, client in telnyx_clients():
        try:
            for row in client.list_owned_numbers():
                number = row.get("phone_number") or row.get("number")
                if number and str(number) not in numbers:
                    numbers.append(str(number))
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return numbers, errors


def sync_inbound_once(limit: int = 20, *, notify_new: bool = True) -> SyncResult:
    """Pull recent inbound messages from all configured Telnyx accounts."""
    result = SyncResult(errors=[])
    date_range = os.getenv("TELNYX_SYNC_DATE_RANGE", "").strip() or None

    for label, client in telnyx_clients():
        result.accounts += 1
        try:
            records = client.list_messaging_detail_records(
                date_range=date_range, direction="inbound", limit=limit
            )
        except Exception as exc:
            result.errors.append(f"{label}: list records: {exc}")
            continue

        # Telnyx returns newest first; process oldest first so Telegram order is sane.
        for rec in reversed(records):
            result.checked += 1
            message_id = rec.get("id")
            if not message_id:
                result.skipped += 1
                continue

            provider_key = f"telnyx:{label}:{message_id}"
            if store.has_provider_message(provider_key):
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
                    provider_message_id=provider_key,
                )
                if row_id:
                    result.stored += 1
                    if notify_new:
                        notify.notify_owner(
                            "📩 SMS received\n"
                            f"To: {to_number}\n"
                            f"From: {from_number}\n"
                            f"Telnyx message id: {message_id}\n"
                            f"Account: {label}\n\n"
                            f"{body}"
                        )
                else:
                    result.skipped += 1
            except Exception as exc:  # keep syncing other messages
                result.errors.append(f"{label}:{message_id}: {exc}")

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
