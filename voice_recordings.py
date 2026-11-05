from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import requests

import audio_store
import voice_config


API_BASE = 'https://api.telnyx.com/v2'
MAX_RECORDING_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 45


class AudioError(Exception):
    code = 'audio_unavailable'
    permanent = False


class RecordingUnavailable(AudioError):
    code = 'recording_unavailable'


class UnsupportedRecording(AudioError):
    code = 'unsupported_recording'
    permanent = True


class RecordingTooLarge(AudioError):
    code = 'recording_too_large'
    permanent = True


class RecordingIdentityError(AudioError):
    code = 'recording_identity_mismatch'
    permanent = True


def _recording_uuid(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return ''


def _url_kind(url: str, recording_id: str) -> str:
    """Accept only Telnyx's documented recording bucket or exact API path.

    No redirect or provider credential is ever sent to an arbitrary URL.
    Custom S3/GCS/Azure destinations require separate integration and are not
    implicitly trusted.
    """
    if not isinstance(url, str) or len(url) > 8192:
        return ''
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment):
            return ''
        host = parsed.hostname.lower()
        path = unquote(parsed.path)
    except (ValueError, UnicodeError):
        return ''
    if '\\' in path or '\x00' in path or any(part in {'.', '..'} for part in path.split('/')):
        return ''
    if host == 'api.telnyx.com' and path in {
        f'/v2/recordings/{recording_id}.mp3', f'/v2/recordings/{recording_id}.wav',
    }:
        return 'telnyx'
    if re.fullmatch(r's3(?:[.-][a-z0-9-]+)?\.amazonaws\.com', host):
        if path.startswith('/telephony-recorder-prod/'):
            return 's3'
    if re.fullmatch(r'telephony-recorder-prod\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com', host):
        if path.startswith('/') and len(path) > 1:
            return 's3'
    return ''


def _download(url: str, recording_id: str, api_key: str) -> bytes:
    kind = _url_kind(url, recording_id)
    if not kind:
        raise UnsupportedRecording()
    # Signed S3 URLs need no bearer token; send the key only to the exact
    # Telnyx API host and never follow redirects automatically.
    headers = {'Authorization': f'Bearer {api_key}'} if kind == 'telnyx' else {}
    deadline = time.monotonic() + MAX_DOWNLOAD_SECONDS
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=(5, 15), allow_redirects=False)
        try:
            if not response.ok or 300 <= response.status_code < 400:
                raise RecordingUnavailable()
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > MAX_RECORDING_BYTES:
                raise RecordingTooLarge()
            parts: list[bytes] = []
            size = 0
            for part in response.iter_content(chunk_size=64 * 1024):
                if time.monotonic() > deadline:
                    raise RecordingUnavailable()
                if not part:
                    continue
                size += len(part)
                if size > MAX_RECORDING_BYTES:
                    raise RecordingTooLarge()
                parts.append(part)
            data = b''.join(parts)
        finally:
            response.close()
    except requests.RequestException as exc:
        raise RecordingUnavailable() from exc
    if not data:
        raise RecordingUnavailable()
    return data


def _audio_kind(data: bytes) -> str:
    if data.startswith(b'ID3') or (len(data) >= 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0):
        return 'mp3'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        return 'wav'
    raise UnsupportedRecording()


def _metadata(connection: voice_config.VoiceConnection, recording_id: str) -> dict:
    try:
        response = requests.get(
            f'{API_BASE}/recordings/{recording_id}',
            headers={'Authorization': f'Bearer {connection.api_key()}'},
            timeout=(5, 15), allow_redirects=False,
        )
        if not response.ok or 300 <= response.status_code < 400:
            raise RecordingUnavailable()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RecordingUnavailable() from exc
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RecordingUnavailable()
    if (_recording_uuid(data.get('id')) != recording_id
            or str(data.get('connection_id') or '') != connection.connection_id):
        raise RecordingIdentityError()
    return data


def _candidate_url(recording: dict) -> str:
    urls = recording.get('download_urls')
    if not isinstance(urls, dict):
        return ''
    for name in ('mp3', 'wav'):
        value = urls.get(name)
        if isinstance(value, str) and value:
            return value
    return ''


def obtain_recording(claim: audio_store.AudioClaim) -> tuple[bytes, str]:
    connection = voice_config.connections().get(claim.connection_id or '')
    recording_id = _recording_uuid(claim.recording_id)
    if not connection or not connection.api_key() or not recording_id:
        raise RecordingUnavailable()
    if claim.to_number not in voice_config.allowed_numbers():
        raise RecordingIdentityError()
    if claim.recording_url:
        try:
            data = _download(claim.recording_url, recording_id, connection.api_key())
            return data, _audio_kind(data)
        except AudioError:
            # Signed URLs expire after roughly ten minutes. Ask the owning
            # account for a fresh one (or another supported format).
            pass
    metadata = _metadata(connection, recording_id)
    metadata_to = metadata.get('to')
    if metadata_to is not None and str(metadata_to) != claim.to_number:
        raise RecordingIdentityError()
    url = _candidate_url(metadata)
    if not url:
        raise RecordingUnavailable()
    data = _download(url, recording_id, connection.api_key())
    return data, _audio_kind(data)


@dataclass
class ImportResult:
    checked: int = 0
    queued: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)


def _import_error(result: ImportResult, connection: voice_config.VoiceConnection, reason: str) -> None:
    result.errors += 1
    # The prefix is an operator-chosen environment-variable name, not a key.
    # Never include exception text, URLs, phone numbers or credentials.
    if len(result.error_details) < 20:
        result.error_details.append(f'{connection.env_prefix}: {reason}')


def import_recordings(limit: int = 5) -> ImportResult:
    """Explicitly import one bounded page of programmable call recordings.

    Native Telnyx voicemail is not known to be exposed through this API.
    """
    limit = max(1, min(int(limit), 10))
    result = ImportResult()
    numbers = voice_config.allowed_numbers()
    configured = voice_config.connections()
    if not numbers or not configured:
        result.errors += 1
        return result
    for connection in configured.values():
        if result.queued >= limit:
            break
        if not connection.api_key():
            _import_error(result, connection, 'missing API key')
            continue
        try:
            response = requests.get(
                f'{API_BASE}/recordings',
                headers={'Authorization': f'Bearer {connection.api_key()}'},
                params={'filter[connection_id]': connection.connection_id,
                        'page[number]': 1, 'page[size]': min(50, max(10, limit * 3))},
                timeout=(5, 15), allow_redirects=False,
            )
            if not response.ok or 300 <= response.status_code < 400:
                status = response.status_code
                _import_error(result, connection, f'HTTP {status}' if type(status) is int and 100 <= status <= 599 else 'request failed')
                continue
            payload = response.json()
            items = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(items, list):
                _import_error(result, connection, 'invalid provider response')
                continue
        except requests.Timeout:
            _import_error(result, connection, 'request timed out')
            continue
        except (requests.RequestException, ValueError):
            _import_error(result, connection, 'request failed')
            continue
        for item in items:
            if result.queued >= limit:
                break
            result.checked += 1
            if not isinstance(item, dict):
                result.skipped += 1
                continue
            recording_id = _recording_uuid(item.get('id'))
            to_number = item.get('to')
            from_number = item.get('from')
            if (not recording_id or str(item.get('connection_id') or '') != connection.connection_id
                    or not isinstance(to_number, str) or to_number not in numbers
                    or item.get('status') not in (None, 'completed')):
                result.skipped += 1
                continue
            # Callers may be SIP identities. Bound all metadata retained in DB.
            if not isinstance(from_number, str) or len(from_number) > 128 or any(ord(c) < 32 for c in from_number):
                from_number = 'unknown'
            try:
                created = audio_store.enqueue_recording(
                    kind='import', connection_id=connection.connection_id, recording_id=recording_id,
                    to_number=to_number, from_number=from_number or 'unknown', url=_candidate_url(item),
                )
            except Exception:
                _import_error(result, connection, 'could not queue recording')
                continue
            if created:
                result.queued += 1
                from audio_delivery import wake
                wake()
            else:
                result.skipped += 1
    return result
