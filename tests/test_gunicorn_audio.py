from __future__ import annotations

import base64
import json
import os
import shlex
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree

import requests
from nacl.signing import SigningKey


# This is imported only in the isolated Gunicorn child. No external providers
# or real credentials are contacted; actual local speech synthesis still runs.
FAKE_AUDIO_PROVIDERS = r'''
import json
import os
from pathlib import Path
import requests

class Response:
    def __init__(self, payload=None, status=200, content=b''):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400
        self.content = content
        self.headers = {}
    def json(self):
        return self.payload
    def iter_content(self, chunk_size=65536):
        yield self.content
    def close(self):
        pass

def post(url, json=None, data=None, files=None, **kwargs):
    if url.endswith('/sendMessage'):
        row = {'kind': 'text', 'text': json.get('text', '')}
        status = 200
    elif url.endswith('/sendVoice'):
        blob = files['voice'][1]
        marker = Path(os.environ['TEST_FIRST_AUDIO_REJECTED'])
        if not marker.exists():
            marker.touch()
            status = 429
        else:
            status = 200
        row = {'kind': 'voice', 'caption': data['caption'], 'chat_id': data['chat_id'],
               'format': files['voice'][2], 'header': blob[:4].hex(), 'bytes': len(blob), 'status': status}
    else:
        raise requests.ConnectionError('unexpected test URL')
    with open(os.environ['TEST_AUDIO_LOG'], 'a', encoding='utf-8') as out:
        out.write(__import__('json').dumps(row) + '\n')
    return Response({'ok': status == 200}, status)

def get(url, **kwargs):
    if url.startswith('https://s3.amazonaws.com/telephony-recorder-prod/'):
        if kwargs.get('headers') or kwargs.get('allow_redirects') is not False:
            raise requests.ConnectionError('unsafe test request')
        return Response(content=b'ID3-fixture-recorded-call')
    raise requests.ConnectionError('unexpected test URL')

requests.post = post
requests.get = get
import audio_delivery
audio_delivery.RETRY_SECONDS = 0.1
'''


class GunicornAudioTest(unittest.TestCase):
    def test_signed_sms_and_call_audio_delivery_with_retry_over_real_http(self):
        repo = Path(__file__).resolve().parents[1]
        signing_key = SigningKey.generate()
        public_key = base64.b64encode(bytes(signing_key.verify_key)).decode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sitecustomize.py').write_text(FAKE_AUDIO_PROVIDERS)
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                port = reserved.getsockname()[1]
            log_path = root / 'provider.jsonl'
            server_log = root / 'gunicorn.log'
            db = root / 'inbound.db'
            env = {
                'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': f'{root}{os.pathsep}{repo}',
                'INBOUND_DB_PATH': str(db), 'TELNYX_POLLING_ENABLED': 'false',
                'SMS_VOICE_ENABLED': 'true', 'TELNYX_REQUIRE_SIGNATURE': 'true',
                'TELNYX_PUBLIC_KEY': public_key, 'TELNYX_VOICE_ENABLED': 'true',
                'TELNYX_VOICE_CONNECTIONS': '111=TELNYX_VOICE_ACCOUNT_A',
                'TELNYX_VOICE_ACCOUNT_A_PUBLIC_KEY': public_key,
                'TELNYX_VOICE_ACCOUNT_A_API_KEY': 'fake-account-key',
                'TELNYX_VOICE_NUMBERS': '+15550000002',
                'VOICE_PUBLIC_BASE_URL': 'https://bridge.example.com',
                'TELEGRAM_BOT_TOKEN': 'fake-bot-token', 'TELEGRAM_ALERT_CHAT_ID': '555',
                'OWNER_TELEGRAM_ID': '777', 'TELEGRAM_WEBHOOK_SECRET': 'fake-secret',
                'TEST_AUDIO_LOG': str(log_path), 'TEST_FIRST_AUDIO_REJECTED': str(root / 'first-rejected'),
            }
            base = f'http://127.0.0.1:{port}'

            def wait_until(check, description, timeout=12):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        if check():
                            return
                    except (OSError, ValueError, requests.RequestException, sqlite3.Error):
                        pass
                    time.sleep(0.05)
                self.fail(description + '\n' + (server_log.read_text() if server_log.exists() else ''))

            def delivered():
                if not log_path.exists():
                    return []
                return [json.loads(line) for line in log_path.read_text().splitlines() if line]

            def states():
                with sqlite3.connect(db) as conn:
                    return conn.execute('SELECT kind, state, audio IS NULL FROM audio_jobs ORDER BY kind').fetchall()

            def signed_post(path, raw, mime):
                timestamp = str(int(time.time()))
                signature = signing_key.sign(timestamp.encode() + b'|' + raw).signature
                return requests.post(base + path, data=raw, headers={
                    'Content-Type': mime, 'Telnyx-Timestamp': timestamp,
                    'Telnyx-Signature-Ed25519': base64.b64encode(signature).decode(),
                }, timeout=5)

            command = shlex.split(json.loads((repo / 'railway.json').read_text())['deploy']['startCommand'])
            command = [sys.executable, '-m', 'gunicorn'] + [arg.replace('$PORT', str(port)) for arg in command[1:]]
            with server_log.open('w') as logs:
                process = subprocess.Popen(command, cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=logs)
                try:
                    wait_until(lambda: requests.get(base + '/health', timeout=1).status_code == 200,
                               'Gunicorn did not start')
                    event = {'data': {'event_type': 'message.received', 'payload': {
                        'id': 'audio-http-sms', 'direction': 'inbound', 'text': 'Fixture code 123456',
                        'from': {'phone_number': '+15550000001'}, 'to': [{'phone_number': '+15550000002'}],
                    }}}
                    raw = json.dumps(event).encode()
                    self.assertEqual(signed_post('/inbound/sms', raw, 'application/json').status_code, 200)
                    self.assertTrue(signed_post('/inbound/sms', raw, 'application/json').json()['duplicate'])
                    wait_until(lambda: any(row['kind'] == 'voice' and row['status'] == 429 for row in delivered()),
                               'Audio upload was not initially attempted and rejected')

                    call = {'ConnectionId': '111', 'CallSid': 'v3:gunicorn-fixture', 'To': '+15550000002',
                            'From': '+15550000001', 'CallStatus': 'in-progress'}
                    denied = requests.post(base + '/inbound/voice', data=call, timeout=5)
                    self.assertEqual(denied.status_code, 403)
                    response = signed_post('/inbound/voice', urlencode(call).encode(), 'application/x-www-form-urlencoded')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(ElementTree.fromstring(response.content).find('Record').attrib['maxLength'], '120')
                    callback = {'ConnectionId': '111', 'CallSid': 'v3:gunicorn-fixture',
                                'RecordingStatus': 'completed', 'RecordingSid': str(uuid.uuid4()),
                                'RecordingUrl': 'https://s3.amazonaws.com/telephony-recorder-prod/example.mp3?sig=fake'}
                    callback_raw = urlencode(callback).encode()
                    first = signed_post('/inbound/voice/recording', callback_raw, 'application/x-www-form-urlencoded')
                    duplicate = signed_post('/inbound/voice/recording', callback_raw, 'application/x-www-form-urlencoded')
                    self.assertFalse(first.json()['duplicate'])
                    self.assertTrue(duplicate.json()['duplicate'])
                    wait_until(lambda: states() == [('call', 'sent', 1), ('sms', 'sent', 1)],
                               'Audio workers did not complete both jobs and clear the local blobs')
                    rows = delivered()
                    successful = [row for row in rows if row['kind'] == 'voice' and row['status'] == 200]
                    self.assertEqual(len(successful), 2)
                    sms = next(row for row in successful if 'SMS read aloud' in row['caption'])
                    call_row = next(row for row in successful if 'Incoming call recording' in row['caption'])
                    self.assertEqual((sms['format'], sms['header'], sms['chat_id']), ('audio/ogg', '4f676753', '555'))
                    self.assertGreater(sms['bytes'], 1000)
                    self.assertEqual((call_row['format'], call_row['header']), ('audio/mpeg', '4944332d'))
                    self.assertEqual(sum(row['kind'] == 'text' and 'Fixture code 123456' in row['text'] for row in rows), 1)
                    self.assertNotIn('fake-account-key', server_log.read_text())
                    self.assertNotIn('fake-bot-token', server_log.read_text())
                finally:
                    process.terminate()
                    try:
                        exit_code = process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        self.fail('Gunicorn did not stop')
                    self.assertEqual(exit_code, 0)


if __name__ == '__main__':
    unittest.main()
