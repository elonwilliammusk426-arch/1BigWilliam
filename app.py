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
from telnyx import TelnyxClient
from telnyx_webhook import parse_inbound_payload, verify_telnyx_signature

app = Flask(__name__)
store.init_db()

MAX_TELEGRAM_MESSAGE = 3900
HELP_TEXT = (
    "SMS inbox bot (owner only).\n\n"
    "Commands:\n"
    "• /latest [limit] — latest inbound SMS across all numbers\n"
    "• /recent <number> [limit] — messages received on one number\n"
    "• /numbers — list numbers that have received messages\n"
    "• /available [country] [area_code] [limit] — search SMS-capable Telnyx numbers\n"
    "• /testalert — send a test alert to the configured group\n"
    "• /whoami — show your Telegram user id\n"
    "• /chatid — show this chat/group id\n\n"
    "Examples:\n"
    "/recent +13412043006 10\n"
    "/available US 732 10\n"
    "/available US any 20"
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
    if event_type != "message.received":
        return jsonify({"ok": True, "ignored": event_type or "unknown"}), 200

    parsed = parse_inbound_payload(data.get("payload", {}))
    if not parsed:
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
            send_telegram(chat_id, "Usage: /recent +13412043006 [limit]")
        else:
            number = args[0]
            limit = _parse_limit(args[1] if len(args) > 1 else None, default=20)
            messages = store.recent_for_number(number, limit=limit)
            if not messages:
                send_telegram(chat_id, f"No messages for {number}.")
            else:
                send_telegram(chat_id, _format_messages(messages))

    elif command == "/numbers":
        with __import__("sqlite3").connect(store.DB_PATH) as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute("SELECT DISTINCT to_number FROM inbound ORDER BY to_number").fetchall()
        if not rows:
            send_telegram(chat_id, "No numbers with messages yet.")
        else:
            send_telegram(chat_id, "Numbers with inbound SMS:\n" + "\n".join(r["to_number"] for r in rows))

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

    elif command == "/testalert":
        notify.notify_owner("✅ Telegram group alert test successful.")
        send_telegram(chat_id, "Sent a test alert to TELEGRAM_ALERT_CHAT_ID.")

    else:
        send_telegram(chat_id, "Unknown command. Send /help")

    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
