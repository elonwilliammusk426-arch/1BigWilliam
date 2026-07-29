"""Single cloud web app for SMS -> Telegram.

Deploy this on a real server/cloud host (Render/Railway/Fly/VPS) instead of
using ngrok. It exposes:

    GET  /health
    POST /inbound/sms          Telnyx inbound SMS webhook
    POST /telegram/webhook     Telegram bot command webhook

This means you do NOT need to run `bot.py` polling in production. Telegram sends
commands to this web app, and Telnyx sends inbound SMS to this same web app.
"""
from __future__ import annotations

import os
from typing import Any

import requests
from flask import Flask, jsonify, request

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

import notify
import store
from config import load_config
from numverify import NumverifyError, format_numverify_result, validate_number
from telnyx import TelnyxClient
from telnyx_sync import start_polling_if_enabled, sync_inbound_once
from telnyx_webhook import parse_telnyx_inbound_event, verify_telnyx_signature

app = Flask(__name__)
store.init_db()
start_polling_if_enabled()

MAX_TELEGRAM_MESSAGE = 3900
HELP_TEXT = (
    "📩 1BigWilliam SMS Inbox\n"
    "Owner-only control panel.\n\n"
    "Inbox:\n"
    "• /latest [limit] — latest messages\n"
    "• /recent <number> [limit] — messages for one number\n"
    "• /numbers — numbers that received SMS\n"
    "• /mynumbers — configured/owned numbers\n\n"
    "Tools:\n"
    "• /available [country] [area] [limit] — search Telnyx numbers\n"
    "• /checknum <number> — validate carrier/line type\n"
    "• /testalert — test Telegram alert\n\n"
    "Setup:\n"
    "• /whoami — your Telegram user id\n"
    "• /chatid — this chat/group id\n\n"
    "Examples:\n"
    "/recent +15306908868 10\n"
    "/available US any 5\n"
    "/checknum +15306908868"
)


def _telegram_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def _owner_id() -> int:
    return int(os.getenv("OWNER_TELEGRAM_ID", "0") or "0")


def send_telegram(chat_id: int | str, text: str) -> None:
    token = _telegram_token()
    if not token or not chat_id:
        return
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:MAX_TELEGRAM_MESSAGE]},
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
        return "No inbound messages yet."
    lines: list[str] = []
    for m in messages:
        lines.append(
            f"#{m.id} [{m.received_at}]\n"
            f"To: {m.to_number}\n"
            f"From: {m.from_number}\n"
            f"{m.body}\n"
        )
    text = "\n".join(lines).strip()
    if len(text) > MAX_TELEGRAM_MESSAGE:
        return text[:MAX_TELEGRAM_MESSAGE] + "\n...truncated"
    return text


def _parse_available_args(args: list[str]) -> tuple[str, str | None, int]:
    country = "US"
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
        if second.lower() not in {"any", "none", "-"}:
            area_code = second
    if len(args) > 2:
        limit = _parse_limit(args[2], default=10)
    return country, area_code, limit


def _format_available_numbers(numbers: list[dict[str, Any]], country: str, area_code: str | None) -> str:
    if not numbers:
        area = f" area {area_code}" if area_code else ""
        return f"No SMS-capable Telnyx numbers found for {country}{area}."

    lines = ["Available SMS-capable Telnyx numbers:"]
    for item in numbers:
        phone = item.get("phone_number", "-")
        cost = item.get("cost_information") or {}
        monthly = cost.get("monthly_cost")
        upfront = cost.get("upfront_cost")
        currency = cost.get("currency") or ""
        regions = item.get("region_information") or []
        region_names = [r.get("region_name") for r in regions if r.get("region_name")]
        region_text = ", ".join(region_names[:3])
        cost_parts = []
        if monthly is not None:
            cost_parts.append(f"monthly {monthly} {currency}".strip())
        if upfront is not None:
            cost_parts.append(f"upfront {upfront} {currency}".strip())
        cost_text = " | " + "; ".join(cost_parts) if cost_parts else ""
        region_suffix = f" | {region_text}" if region_text else ""
        lines.append(f"• {phone}{region_suffix}{cost_text}")

    text = "\n".join(lines)
    if len(text) > MAX_TELEGRAM_MESSAGE:
        return text[:MAX_TELEGRAM_MESSAGE] + "\n...truncated"
    return text


def _configured_numbers() -> list[str]:
    raw_values = [os.getenv("TELNYX_FROM_NUMBER", ""), os.getenv("TELNYX_NUMBERS", "")]
    numbers: list[str] = []
    for raw in raw_values:
        for part in raw.replace(";", ",").split(","):
            number = part.strip()
            if number and number not in numbers:
                numbers.append(number)
    return numbers


def _owned_telnyx_numbers() -> tuple[list[str], str | None]:
    """Return Telnyx-owned numbers if API key works, plus optional error."""
    try:
        cfg = load_config()
        client = TelnyxClient(cfg.telnyx_api_key, cfg.telnyx_base_url)
        rows = client.list_owned_numbers()
        numbers: list[str] = []
        for row in rows:
            number = row.get("phone_number") or row.get("number")
            if number and number not in numbers:
                numbers.append(str(number))
        return numbers, None
    except Exception as exc:
        return [], str(exc)


def _format_my_numbers() -> str:
    configured = _configured_numbers()
    seen = store.distinct_to_numbers()
    owned, api_error = _owned_telnyx_numbers()

    merged: list[str] = []
    for number in [*owned, *configured, *seen]:
        if number and number not in merged:
            merged.append(number)

    if not merged:
        text = "No numbers found yet. Add Telnyx numbers to the same Messaging Profile."
    else:
        lines = ["Your inbox numbers:"]
        for number in merged:
            tags = []
            if number in owned:
                tags.append("Telnyx")
            if number in configured:
                tags.append("configured")
            if number in seen:
                tags.append("received SMS")
            suffix = f" ({', '.join(tags)})" if tags else ""
            lines.append(f"• {number}{suffix}")
        text = "\n".join(lines)

    if api_error:
        text += "\n\nNote: Could not fetch Telnyx account numbers. Showing configured/seen numbers."
    return text[:MAX_TELEGRAM_MESSAGE]


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "sms-telegram-cloud"}), 200


@app.post("/inbound/sms")
def inbound_sms():
    raw_body = request.get_data()
    if not verify_telnyx_signature(raw_body, request.headers):
        return jsonify({"ok": False, "error": "invalid_signature"}), 403

    event = request.get_json(silent=True) or {}
    data = event.get("data", {})
    event_type = data.get("event_type")

    parsed = parse_telnyx_inbound_event(event)
    if not parsed:
        # Telnyx also sends delivery status events; ignore those quietly.
        if event_type in {"message.sent", "message.finalized"}:
            return jsonify({"ok": True, "ignored": event_type}), 200
        notify.notify_owner(
            "⚠️ Telnyx webhook received but could not parse inbound SMS.\n"
            f"Event type: {event_type or 'unknown'}"
        )
        return jsonify({"ok": False, "error": "could_not_parse_payload"}), 400

    to_number, from_number, body, message_id = parsed
    row_id = store.save_message(to_number=to_number, from_number=from_number, body=body)
    notify.notify_owner(
        "📩 SMS received\n"
        f"To: {to_number}\n"
        f"From: {from_number}\n"
        f"Telnyx message id: {message_id or '-'}\n\n"
        f"{body}"
    )
    return jsonify({"ok": True, "stored_id": row_id}), 200


@app.post("/telegram/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    chat_id = chat.get("id")
    user_id = user.get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not text.startswith("/"):
        return jsonify({"ok": True, "ignored": True}), 200

    parts = text.split()
    command = parts[0].split("@", 1)[0].lower()
    args = parts[1:]

    # Setup helpers: available even before OWNER_TELEGRAM_ID is correct.
    if command == "/whoami":
        send_telegram(
            chat_id,
            f"Your Telegram user id: {user_id or '-'}\n"
            f"This chat id: {chat_id}\n"
            f"Chat type: {chat.get('type', '-')}",
        )
        return jsonify({"ok": True}), 200

    if command == "/chatid":
        send_telegram(chat_id, f"This chat id: {chat_id}\nChat type: {chat.get('type', '-')}")
        return jsonify({"ok": True}), 200

    if not _authorized(user_id):
        send_telegram(chat_id, "🚫 Unauthorized.")
        return jsonify({"ok": True, "unauthorized": True}), 200

    if command in {"/start", "/help"}:
        send_telegram(chat_id, HELP_TEXT)

    elif command == "/latest":
        limit = _parse_limit(args[0] if args else None, default=10)
        send_telegram(chat_id, _format_messages(store.recent_all(limit=limit)))

    elif command == "/recent":
        if not args:
            send_telegram(chat_id, "Usage: /recent +15306908868 [limit]")
        else:
            number = args[0]
            limit = _parse_limit(args[1] if len(args) > 1 else None, default=20)
            messages = store.recent_for_number(number, limit=limit)
            if not messages:
                send_telegram(chat_id, f"No messages for {number}.")
            else:
                send_telegram(chat_id, _format_messages(messages))

    elif command == "/numbers":
        numbers = store.distinct_to_numbers()
        if not numbers:
            send_telegram(chat_id, "No numbers with messages yet.")
        else:
            send_telegram(chat_id, "Numbers with inbound SMS:\n" + "\n".join(numbers))

    elif command == "/mynumbers":
        send_telegram(chat_id, _format_my_numbers())

    elif command == "/available":
        country, area_code, limit = _parse_available_args(args)
        try:
            cfg = load_config()
            client = TelnyxClient(cfg.telnyx_api_key, cfg.telnyx_base_url)
            numbers = client.search_available_numbers(
                country_code=country,
                area_code=area_code,
                limit=limit,
                phone_number_type="local",
                features="sms",
            )
            send_telegram(chat_id, _format_available_numbers(numbers, country, area_code))
        except Exception as exc:
            send_telegram(
                chat_id,
                "Could not search available numbers. Check TELNYX_API_KEY.\n"
                f"Error: {exc}",
            )

    elif command == "/checknum":
        if not args:
            send_telegram(chat_id, "Usage: /checknum +15306908868")
        else:
            try:
                data = validate_number(args[0])
                send_telegram(chat_id, format_numverify_result(data))
            except NumverifyError as exc:
                send_telegram(
                    chat_id,
                    "Could not check number. Add NUMVERIFY_API_KEY in Railway Variables.\n"
                    f"Error: {exc}",
                )

    elif command == "/syncsms":
        limit = _parse_limit(args[0] if args else None, default=20)
        try:
            res = sync_inbound_once(limit=limit, notify_new=True)
            send_telegram(
                chat_id,
                f"Sync complete. Checked: {res.checked}, stored new: {res.stored}, skipped: {res.skipped}."
                + (f"\nErrors: {'; '.join(res.errors[:3])}" if res.errors else ""),
            )
        except Exception as exc:
            send_telegram(chat_id, f"Sync failed: {exc}")

    elif command == "/testalert":
        notify.notify_owner("✅ Telegram group alert test successful.")
        send_telegram(chat_id, "Sent a test alert to TELEGRAM_ALERT_CHAT_ID.")

    else:
        send_telegram(chat_id, "Unknown command. Send /help")

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
