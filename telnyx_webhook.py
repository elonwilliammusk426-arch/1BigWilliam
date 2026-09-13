from __future__ import annotations

import base64
import os
import time
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def parse_telnyx_inbound_event(event: dict[str, Any]) -> tuple[str, str, str, str | None] | None:
    data = event.get('data', {}) if isinstance(event, dict) else {}
    event_type = data.get('event_type')
    payload = data.get('payload', {}) if isinstance(data, dict) else {}

    if event_type == 'message.received' and isinstance(payload, dict):
        return parse_inbound_payload(payload)

    for candidate in (payload, data, event):
        if not isinstance(candidate, dict):
            continue
        parsed = parse_inbound_payload(candidate)
        if parsed and (
            candidate.get('direction') == 'inbound'
            or candidate.get('record_type') == 'message'
            or candidate.get('from')
        ):
            return parsed

        if candidate.get('record_type') == 'message' and candidate.get('direction') == 'inbound':
            from_number = str(candidate.get('cli') or candidate.get('from') or '')
            to_number = str(candidate.get('cld') or candidate.get('to') or '')
            body = str(candidate.get('text') or candidate.get('body') or candidate.get('message') or '')
            message_id = candidate.get('id')
            if from_number and to_number:
                return to_number, from_number, body, message_id
    return None


def parse_inbound_payload(payload: dict[str, Any]) -> tuple[str, str, str, str | None] | None:
    from_number = _phone(payload.get('from'))
    to_number = _first_to_number(payload.get('to'))
    body = payload.get('text') or payload.get('body') or payload.get('message') or ''
    message_id = payload.get('id')
    if not from_number or not to_number:
        return None
    return to_number, from_number, body, message_id


def _phone(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get('phone_number') or '')
    if isinstance(value, str):
        return value
    return ''


def _first_to_number(value: Any) -> str:
    if isinstance(value, list) and value:
        return _phone(value[0])
    return _phone(value)


def verify_telnyx_signature(raw_body: bytes, headers) -> bool:
    public_key_b64 = os.getenv('TELNYX_PUBLIC_KEY', '').strip()
    tolerance = int(os.getenv('TELNYX_SIGNATURE_TOLERANCE', '300') or '300')
    if not public_key_b64:
        return True

    signature_b64 = headers.get('telnyx-signature-ed25519') or headers.get('Telnyx-Signature-Ed25519')
    timestamp = headers.get('telnyx-timestamp') or headers.get('Telnyx-Timestamp')
    if not signature_b64 or not timestamp:
        return False

    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - timestamp_int) > tolerance:
        return False

    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
        public_key = base64.b64decode(public_key_b64)
        signature = base64.b64decode(signature_b64)
        signed_payload = timestamp.encode('utf-8') + b'|' + raw_body
        VerifyKey(public_key).verify(signed_payload, signature)
        return True
    except (ValueError, BadSignatureError, Exception):
        return False
