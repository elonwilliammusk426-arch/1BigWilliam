from __future__ import annotations

import re
import uuid
from xml.etree import ElementTree

from flask import Blueprint, Response, current_app, jsonify, request

import audio_store
import voice_config
from telnyx_webhook import verify_telnyx_signature


voice_blueprint = Blueprint('voice', __name__)


def _one(name: str) -> str:
    values = request.form.getlist(name)
    return values[0].strip() if len(values) == 1 else ''


def _authenticated_form() -> tuple[voice_config.VoiceConnection | None, str]:
    raw = request.get_data()
    if request.mimetype != 'application/x-www-form-urlencoded':
        return None, ''
    connection_id = _one('ConnectionId')
    connection = voice_config.connections().get(connection_id)
    if not connection or not connection.public_key():
        return None, ''
    if not verify_telnyx_signature(raw, request.headers, public_keys=[connection.public_key()], require=True):
        return None, ''
    call_sid = _one('CallSid')
    if not re.fullmatch(r'[A-Za-z0-9_:=-]{1,512}', call_sid):
        return None, ''
    return connection, call_sid


def _xml(root: ElementTree.Element) -> Response:
    return Response(ElementTree.tostring(root, encoding='utf-8', xml_declaration=True), mimetype='application/xml')


@voice_blueprint.post('/inbound/voice')
def incoming_voice():
    if not audio_store.enabled('TELNYX_VOICE_ENABLED'):
        return jsonify({'ok': False, 'error': 'disabled'}), 404
    connection, call_sid = _authenticated_form()
    if not connection:
        return jsonify({'ok': False, 'error': 'invalid_signature_or_call'}), 403
    if not voice_config.is_call_configured(connection.connection_id):
        return jsonify({'ok': False, 'error': 'voice_not_configured'}), 503
    to_number = _one('To')
    from_number = _one('From') or 'unknown'
    call_status = _one('CallStatus').lower()
    if (to_number not in voice_config.allowed_numbers() or len(from_number) > 128
            or any(ord(ch) < 32 for ch in from_number)
            or call_status not in {'', 'ringing', 'queued', 'in-progress'}):
        return jsonify({'ok': False, 'error': 'call_not_allowed'}), 403
    try:
        if not audio_store.register_call(connection.connection_id, call_sid, to_number, from_number):
            return jsonify({'ok': False, 'error': 'call_not_allowed'}), 403
    except Exception as exc:
        current_app.logger.error('Voice call could not be stored: %s', type(exc).__name__)
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503

    base = voice_config.base_url()
    root = ElementTree.Element('Response')
    ElementTree.SubElement(root, 'Record', {
        'action': base + '/inbound/voice/end',
        'method': 'POST',
        'maxLength': str(voice_config.call_record_seconds()),
        'playBeep': 'true' if voice_config.play_beep() else 'false',
        'finishOnKey': '#',
        'channels': 'single',
        'format': 'mp3',
        'recordingStatusCallback': base + '/inbound/voice/recording',
        'recordingStatusCallbackMethod': 'POST',
        'recordingStatusCallbackEvent': 'completed',
    })
    # Explicit action asks for Hangup after Record; the trailing verb is a
    # fallback if Telnyx continues inline instead of fetching the action URL.
    ElementTree.SubElement(root, 'Hangup')
    return _xml(root)


@voice_blueprint.post('/inbound/voice/end')
def end_voice():
    # This stateless response has no side effects and accepts no caller data.
    # Always return Hangup, even after disabling calls or if Telnyx does not
    # sign the action fetch: rejecting it could leave a call connected longer.
    root = ElementTree.Element('Response')
    ElementTree.SubElement(root, 'Hangup')
    return _xml(root)


@voice_blueprint.post('/inbound/voice/recording')
def voice_recording():
    if not audio_store.enabled('TELNYX_VOICE_ENABLED'):
        return jsonify({'ok': False, 'error': 'disabled'}), 404
    connection, call_sid = _authenticated_form()
    if not connection:
        return jsonify({'ok': False, 'error': 'invalid_signature_or_call'}), 403
    if _one('RecordingStatus').lower() != 'completed':
        return jsonify({'ok': True, 'ignored': True}), 200
    try:
        recording_id = str(uuid.UUID(_one('RecordingSid')))
    except (ValueError, AttributeError):
        return jsonify({'ok': False, 'error': 'invalid_recording'}), 400
    url = _one('RecordingUrl')
    if len(url) > 8192:
        return jsonify({'ok': False, 'error': 'invalid_recording'}), 400
    try:
        call = audio_store.find_call(connection.connection_id, call_sid)
        if not call:
            return jsonify({'ok': False, 'error': 'unknown_call'}), 403
        created = audio_store.enqueue_recording(
            kind='call', connection_id=connection.connection_id, recording_id=recording_id,
            to_number=call[0], from_number=call[1], url=url,
        )
    except Exception as exc:
        current_app.logger.error('Voice recording could not be queued: %s', type(exc).__name__)
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503
    from audio_delivery import wake
    wake()
    return jsonify({'ok': True, 'duplicate': not created}), 200
