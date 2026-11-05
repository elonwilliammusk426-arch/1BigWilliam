from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

import audio_store
import notify
import store
import voice_config
import voice_recordings
import voice_synth
from config import sync_lookback_hours


logger = logging.getLogger(__name__)
RETRY_SECONDS = 30
BATCH_SIZE = 20
MAX_FAILURES_PER_BATCH = 3
_wake = threading.Event()
_start_lock = threading.Lock()
_worker: threading.Thread | None = None


class SMSAudioTooLong(voice_recordings.AudioError):
    code = 'sms_too_long'
    permanent = True


class SpeechUnavailable(voice_recordings.AudioError):
    code = 'speech_unavailable'


def _child_environment() -> dict[str, str]:
    # Avoid exposing bot/API keys to the native speech subprocess.
    allowed = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH', 'SMS_VOICE_LANGUAGE')
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _speech_audio(body: str | None) -> bytes:
    if not body or store.body_is_incomplete(body):
        raise SpeechUnavailable()
    if len(body) > voice_synth.MAX_TEXT_CHARS:
        # Do not silently truncate an SMS: the code might be at the end.
        raise SMSAudioTooLong()
    try:
        child = subprocess.run(
            [sys.executable, str(Path(voice_synth.__file__).resolve())],
            input=body.encode('utf-8', errors='replace'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=45, check=False, env=_child_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpeechUnavailable() from exc
    if child.returncode or not child.stdout.startswith(b'OggS') or len(child.stdout) > voice_recordings.MAX_RECORDING_BYTES:
        raise SpeechUnavailable()
    return child.stdout


def _wav_to_ogg(data: bytes) -> bytes:
    try:
        import imageio_ffmpeg
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
             '-protocol_whitelist', 'pipe', '-f', 'wav', '-i', 'pipe:0', '-vn',
             '-c:a', 'libopus', '-b:a', '32k', '-f', 'ogg', 'pipe:1'],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False,
            env=_child_environment(),
        )
    except (OSError, ImportError, subprocess.TimeoutExpired) as exc:
        raise SpeechUnavailable() from exc
    if (result.returncode or not result.stdout.startswith(b'OggS')
            or len(result.stdout) > voice_recordings.MAX_RECORDING_BYTES):
        raise voice_recordings.UnsupportedRecording()
    return result.stdout


def _caption(claim: audio_store.AudioClaim) -> str:
    title = {
        'sms': '🔊 SMS read aloud',
        'call': '🎙 Incoming call recording',
        'import': '🎙 Imported programmable call recording',
    }[claim.kind]
    return f'{title}\nTo: {claim.to_number or "unknown"}\nFrom: {claim.from_number or "unknown"}'


def _deliver_one(claim: audio_store.AudioClaim) -> str:
    sending_started = False
    try:
        audio, audio_format = claim.audio, claim.audio_format
        if not audio:
            if claim.kind == 'sms':
                audio, audio_format = _speech_audio(claim.body), 'ogg'
            else:
                audio, audio_format = voice_recordings.obtain_recording(claim)
                if audio_format == 'wav':
                    audio, audio_format = _wav_to_ogg(audio), 'ogg'
            if not audio_store.save_audio(claim, audio, audio_format):
                return 'lost_lease'
        if not audio_store.begin_sending(claim):
            return 'lost_lease'
        sending_started = True
        outcome = notify.send_voice_owner(audio, audio_format or '', _caption(claim))
        error = {
            'sent': '', 'retry': 'telegram_rejected',
            'uncertain': 'delivery_unconfirmed', 'failed': 'unsupported_audio',
        }.get(outcome, 'delivery_unconfirmed')
        if outcome not in {'sent', 'retry', 'uncertain', 'failed'}:
            outcome = 'uncertain'
        if not audio_store.finish(claim, outcome, error, retry_seconds=RETRY_SECONDS):
            return 'lost_lease'
        return outcome
    except voice_recordings.AudioError as exc:
        outcome = 'uncertain' if sending_started else ('failed' if exc.permanent else 'retry')
        error = 'delivery_unconfirmed' if sending_started else exc.code
        audio_store.finish(claim, outcome, error, retry_seconds=RETRY_SECONDS)
        return outcome
    except Exception as exc:
        # Before upload it is safe to retry. Once submission begins the
        # result may be ambiguous and must not be resent automatically.
        logger.error('Audio processing failed: %s', type(exc).__name__)
        outcome = 'uncertain' if sending_started else 'retry'
        error = 'delivery_unconfirmed' if sending_started else 'audio_processing_error'
        audio_store.finish(claim, outcome, error, retry_seconds=RETRY_SECONDS)
        return outcome


def deliver_pending(limit: int = BATCH_SIZE) -> int:
    audio_store.prune(sync_lookback_hours())
    delivered = 0
    failures = 0
    for _ in range(max(0, min(int(limit), BATCH_SIZE))):
        claim = audio_store.claim_next(voice_config.enabled_kinds())
        if claim is None:
            break
        outcome = _deliver_one(claim)
        if outcome == 'sent':
            delivered += 1
        else:
            failures += 1
            if failures >= MAX_FAILURES_PER_BATCH:
                break
    return delivered


def wake() -> None:
    _wake.set()


def worker_running() -> bool:
    return _worker is not None and _worker.is_alive()


def _loop() -> None:
    while True:
        _wake.clear()
        try:
            delivered = deliver_pending()
            if delivered >= BATCH_SIZE - MAX_FAILURES_PER_BATCH:
                continue
        except Exception as exc:
            logger.error('Audio worker error: %s', type(exc).__name__)
        # A short check interval limits the chance of expiring signed URLs even
        # if a webhook or poll could not wake the worker.
        _wake.wait(5)


def start_if_enabled() -> None:
    global _worker
    if not voice_config.enabled_kinds():
        return
    with _start_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name='audio-delivery', daemon=True)
        _worker.start()
