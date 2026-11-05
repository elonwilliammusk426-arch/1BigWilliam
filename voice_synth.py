"""Local SMS-to-Opus helper. Run in a child process; SMS text arrives on stdin.

No message text is put into command-line arguments, logs, or a network TTS API.
The separate process contains native-library failures and global eSpeak state.
"""
from __future__ import annotations

import ctypes
import io
import os
import re
import subprocess
import sys
import wave


MAX_TEXT_CHARS = 2000
MAX_PCM_BYTES = 64 * 1024 * 1024


def speech_text(text: str) -> str:
    # Codes should be spoken digit by digit, not as a large cardinal number.
    text = text.replace('\x00', ' ')
    return re.sub(r'(?<!\d)\d{4,}(?!\d)', lambda match: ' '.join(match.group()), text)


def synthesize(text: str, voice: str = 'en-us') -> bytes:
    if not text or len(text) > MAX_TEXT_CHARS:
        raise ValueError('unsupported text length')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', voice):
        raise ValueError('invalid voice')

    import espeakng_loader
    import imageio_ffmpeg

    data_path = espeakng_loader.get_data_path()
    # The bundled native library has a short fixed-size path buffer. Refuse a
    # long or missing path rather than letting it terminate the child process.
    if not os.path.isdir(data_path) or len(os.fsencode(data_path)) >= 160:
        raise RuntimeError('speech data path unavailable')
    library = ctypes.CDLL(espeakng_loader.get_library_path())
    library.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    library.espeak_Initialize.restype = ctypes.c_int
    sample_rate = library.espeak_Initialize(2, 0, os.fsencode(data_path), 0)  # synchronous retrieval
    if sample_rate <= 0:
        raise RuntimeError('speech initialization failed')

    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)
    samples: list[bytes] = []
    sample_bytes = 0
    aborted = False

    def receive_samples(wav, count, _events):
        nonlocal sample_bytes, aborted
        if wav and count > 0:
            sample_bytes += count * 2
            if sample_bytes > MAX_PCM_BYTES:
                aborted = True
                return 1
            samples.append(ctypes.string_at(wav, count * 2))
        return 0

    callback = callback_type(receive_samples)
    library.espeak_SetSynthCallback.argtypes = [callback_type]
    library.espeak_SetSynthCallback(callback)
    library.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
    library.espeak_SetVoiceByName.restype = ctypes.c_int
    if library.espeak_SetVoiceByName(voice.encode('ascii')) != 0:
        raise RuntimeError('speech voice unavailable')
    library.espeak_Synth.argtypes = [
        ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
        ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
    ]
    library.espeak_Synth.restype = ctypes.c_int
    input_bytes = speech_text(text).encode('utf-8', errors='replace')
    # espeakCHARS_UTF8, without SSML or phoneme interpretation.
    result = library.espeak_Synth(input_bytes, len(input_bytes) + 1, 0, 1, 0, 1, None, None)
    if result != 0 or aborted or not samples:
        raise RuntimeError('speech synthesis failed')

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b''.join(samples))
    converted = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
         '-f', 'wav', '-i', 'pipe:0', '-vn', '-c:a', 'libopus', '-b:a', '24k', '-f', 'ogg', 'pipe:1'],
        input=wav_buffer.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
    )
    if converted.returncode or not converted.stdout.startswith(b'OggS'):
        raise RuntimeError('speech encoding failed')
    return converted.stdout


def main() -> int:
    try:
        raw = sys.stdin.buffer.read((MAX_TEXT_CHARS * 4) + 1)
        if len(raw) > MAX_TEXT_CHARS * 4:
            return 1
        text = raw.decode('utf-8', errors='replace')
        voice = os.getenv('SMS_VOICE_LANGUAGE', 'en-us').strip() or 'en-us'
        sys.stdout.buffer.write(synthesize(text, voice))
        return 0
    except Exception:
        # Do not print the text or exception details; stderr may be collected.
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
