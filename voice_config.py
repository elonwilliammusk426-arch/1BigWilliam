from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import audio_store


@dataclass(frozen=True)
class VoiceConnection:
    connection_id: str
    env_prefix: str

    def api_key(self) -> str:
        return os.getenv(self.env_prefix + '_API_KEY', '').strip()

    def public_key(self) -> str:
        return os.getenv(self.env_prefix + '_PUBLIC_KEY', '').strip()


def connections() -> dict[str, VoiceConnection]:
    """Map connection IDs to explicitly named account credentials.

    A positional index into TELNYX_API_KEYS is deliberately not used: key list
    changes must never make calls use another account's credentials.
    """
    result: dict[str, VoiceConnection] = {}
    raw = os.getenv('TELNYX_VOICE_CONNECTIONS', '')
    for part in raw.replace(';', ',').replace('\n', ',').split(','):
        item = part.strip()
        if not item:
            continue
        connection, separator, prefix = item.partition('=')
        connection, prefix = connection.strip(), prefix.strip()
        if (not separator or not re.fullmatch(r'[0-9]{1,32}', connection)
                or not re.fullmatch(r'TELNYX_VOICE_ACCOUNT_[A-Z0-9_]{1,40}', prefix)
                or (connection in result and result[connection].env_prefix != prefix)):
            return {}
        result[connection] = VoiceConnection(connection, prefix)
    return result


def allowed_numbers() -> set[str]:
    raw = os.getenv('TELNYX_VOICE_NUMBERS', '')
    values: set[str] = set()
    for part in raw.replace(';', ',').replace('\n', ',').split(','):
        number = part.strip()
        if not number:
            continue
        if not re.fullmatch(r'\+[1-9][0-9]{6,14}', number):
            return set()
        values.add(number)
    return values


def base_url() -> str:
    value = (os.getenv('VOICE_PUBLIC_BASE_URL', '').strip() or os.getenv('PUBLIC_BASE_URL', '').strip()).rstrip('/')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.path or parsed.query or parsed.fragment):
            return ''
        host = parsed.hostname.lower()
        if host in {'localhost', 'localhost.localdomain'} or host.endswith('.localhost'):
            return ''
        try:
            if not ipaddress.ip_address(host).is_global:
                return ''
        except ValueError:
            if '.' not in host:
                return ''
    except ValueError:
        return ''
    return value


def call_record_seconds() -> int:
    try:
        value = int(os.getenv('TELNYX_VOICE_MAX_SECONDS', '120') or '120')
    except ValueError:
        value = 120
    return max(30, min(value, 300))


def play_beep() -> bool:
    # An explicit false can remove the beep; the operator remains responsible
    # for consent and any notices required in the caller's jurisdiction.
    return os.getenv('TELNYX_VOICE_PLAY_BEEP', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}


def enabled_kinds() -> tuple[str, ...]:
    return tuple(kind for kind, setting in (
        ('sms', 'SMS_VOICE_ENABLED'),
        ('call', 'TELNYX_VOICE_ENABLED'),
        ('import', 'TELNYX_RECORDING_IMPORT_ENABLED'),
    ) if audio_store.enabled(setting))


def is_call_configured(connection_id: str) -> bool:
    connection = connections().get(connection_id)
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    destination = os.getenv('TELEGRAM_ALERT_CHAT_ID', '').strip() or os.getenv('OWNER_TELEGRAM_ID', '').strip()
    # Do not take over incoming calls when the recording has nowhere to go.
    # This checks configuration only; the operator must still test Telegram.
    return bool(connection and connection.api_key() and connection.public_key() and allowed_numbers()
                and base_url() and token and destination)
