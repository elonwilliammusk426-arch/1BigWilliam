"""Flask webhook receiver for Telnyx inbound SMS/MMS events.

Receives messages sent to YOUR OWN Telnyx numbers, stores them in SQLite, and
optionally forwards a live alert to your owner-only Telegram chat.

Configure this URL on your Telnyx Messaging Profile, for example:
    https://your-domain.example/inbound/sms
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from flask import Flask, jsonify, request

import notify
import store

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    pass

app = Flask(__name__)
store.init_db()

SIGNATURE_TOLERANCE_SECONDS = int(os.getenv("TELNYX_SIGNATURE_TOLERANCE", "300"))


@app.post("/inbound/sms")
def inbound_sms():
    raw_body = request.get_data()
    if not verify_telnyx_signature(raw_body, request.headers):
        return jsonify({"ok": False, "error": "invalid_signature"}), 403

    event = request.get_json(silent=True) or {}
    data = event.get("data", {})
    event_type = data.get("event_type")

    # Telnyx will also send delivery status events. We only store inbound SMS.
    if event_type != "message.received":
        return jsonify({"ok": True, "ignored": event_type or "unknown"}), 200

    payload = data.get("payload", {})
    parsed = parse_inbound_payload(payload)
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


@app.get("/health")
def health():
    return jsonify({"ok": True, "provider": "telnyx"}), 200


def parse_inbound_payload(payload: dict[str, Any]) -> tuple[str, str, str, str | None] | None:
    """Return (to_number, from_number, body, message_id) from Telnyx payload."""
    from_number = _phone(payload.get("from"))
    to_number = _first_to_number(payload.get("to"))
    body = payload.get("text") or ""
    message_id = payload.get("id")

    if not from_number or not to_number:
        return None
    return to_number, from_number, body, message_id


def _phone(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("phone_number") or "")
    if isinstance(value, str):
        return value
    return ""


def _first_to_number(value: Any) -> str:
    if isinstance(value, list) and value:
        return _phone(value[0])
    return _phone(value)


def verify_telnyx_signature(raw_body: bytes, headers) -> bool:
    """Verify Telnyx Ed25519 webhook signature when TELNYX_PUBLIC_KEY is set.

    For local development, if TELNYX_PUBLIC_KEY is empty we accept the webhook.
    In production, set TELNYX_PUBLIC_KEY from the Telnyx portal so forged POSTs
    cannot be inserted into your inbox.
    """
    public_key_b64 = os.getenv("TELNYX_PUBLIC_KEY", "").strip()
    if not public_key_b64:
        return True

    signature_b64 = headers.get("telnyx-signature-ed25519") or headers.get(
        "Telnyx-Signature-Ed25519"
    )
    timestamp = headers.get("telnyx-timestamp") or headers.get("Telnyx-Timestamp")
    if not signature_b64 or not timestamp:
        return False

    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - timestamp_int) > SIGNATURE_TOLERANCE_SECONDS:
        return False

    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey

        public_key = base64.b64decode(public_key_b64)
        signature = base64.b64decode(signature_b64)
        signed_payload = timestamp.encode("utf-8") + b"|" + raw_body
        VerifyKey(public_key).verify(signed_payload, signature)
        return True
    except (ValueError, BadSignatureError, Exception):
        return False


def _pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
