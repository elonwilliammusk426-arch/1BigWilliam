from __future__ import annotations

import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import requests
from nacl.signing import SigningKey


# Inject deterministic provider responses into the Gunicorn child only. No
# real Telnyx or Telegram credentials, requests, phone numbers, or SMS are used.
FAKE_PROVIDERS = r'''
import json
import os
from datetime import datetime, timezone
import requests

class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400
    def json(self):
        return self.payload
    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(response=self)

def get(self, url, params=None, **kwargs):
    if url.endswith('/detail_records'):
        return Response({'data': [{
            'uuid': 'sync-message', 'direction': 'inbound',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'cli': '+15550000001', 'cld': '+15550000002',
        }], 'meta': {'total_pages': 1}})
    if url.endswith('/messages/sync-message'):
        return Response({'data': {'direction': 'inbound', 'text': 'sync sample',
            'from': {'phone_number': '+15550000001'},
            'to': [{'phone_number': '+15550000002'}]}})
    raise requests.ConnectionError('Unexpected provider URL in test')

def post(url, json=None, **kwargs):
    if not url.endswith('/sendMessage'):
        raise requests.ConnectionError('Unexpected provider URL in test')
    text = json.get('text', '')
    status = 400 if 'failure-message' in text else 200
    with open(os.environ['TEST_SEND_LOG'], 'a', encoding='utf-8') as output:
        output.write(__import__('json').dumps({'text': text, 'status': status}) + '\n')
    return Response({'ok': status == 200}, status)

requests.Session.get = get
requests.post = post
'''


class GunicornHttpTest(unittest.TestCase):
    def test_authenticated_webhooks_sync_delivery_and_retry_over_real_http(self):
        repo = Path(__file__).resolve().parents[1]
        signing_key = SigningKey.generate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sitecustomize.py').write_text(FAKE_PROVIDERS)
            db = root / 'inbound.db'
            send_log = root / 'sent.jsonl'
            server_log = root / 'gunicorn.log'
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                port = reserved.getsockname()[1]

            env = {
                'PATH': os.environ.get('PATH', ''),
                'PYTHONPATH': f'{root}{os.pathsep}{repo}',
                'INBOUND_DB_PATH': str(db),
                'TELNYX_API_KEY': 'fake-key',
                'TELNYX_PUBLIC_KEY': base64.b64encode(bytes(signing_key.verify_key)).decode(),
                'TELNYX_REQUIRE_SIGNATURE': 'true',
                'TELNYX_POLLING_ENABLED': 'false',
                'TELEGRAM_BOT_TOKEN': 'fake-token',
                'TELEGRAM_ALERT_CHAT_ID': '555',
                'OWNER_TELEGRAM_ID': '777',
                'TELEGRAM_WEBHOOK_SECRET': 'test_secret',
                'TEST_SEND_LOG': str(send_log),
            }
            base = f'http://127.0.0.1:{port}'
            telegram_headers = {'X-Telegram-Bot-Api-Secret-Token': 'test_secret'}

            def wait_until(check, message, timeout=8):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        if check():
                            return
                    except (OSError, sqlite3.Error, requests.RequestException, ValueError):
                        pass
                    time.sleep(0.05)
                self.fail(message + '\n' + (server_log.read_text() if server_log.exists() else ''))

            def sent_messages():
                if not send_log.exists():
                    return []
                return [json.loads(line) for line in send_log.read_text().splitlines() if line]

            def state(message_id):
                if not db.exists():
                    return None
                with sqlite3.connect(db) as conn:
                    row = conn.execute(
                        'SELECT notification_state, notification_attempts FROM inbound WHERE provider_message_id = ?',
                        (f'telnyx:{message_id}',),
                    ).fetchone()
                return row

            def telnyx_event(message_id, text):
                payload = {'data': {'event_type': 'message.received', 'payload': {
                    'id': message_id, 'direction': 'inbound',
                    'from': {'phone_number': '+15550000001'},
                    'to': [{'phone_number': '+15550000002'}], 'text': text,
                }}}
                body = json.dumps(payload).encode()
                timestamp = str(int(time.time()))
                signature = signing_key.sign(timestamp.encode() + b'|' + body).signature
                headers = {
                    'Content-Type': 'application/json', 'Telnyx-Timestamp': timestamp,
                    'Telnyx-Signature-Ed25519': base64.b64encode(signature).decode(),
                }
                return requests.post(base + '/inbound/sms', data=body, headers=headers, timeout=5)

            def telegram_update(chat_id, text):
                return {'message': {'chat': {'id': chat_id}, 'from': {'id': 777}, 'text': text}}

            with server_log.open('w') as logs:
                process = subprocess.Popen(
                    [sys.executable, '-m', 'gunicorn', 'app:app', '--bind', f'0.0.0.0:{port}',
                     '--workers', '1', '--timeout', '120'],
                    cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=logs,
                )
                try:
                    wait_until(lambda: requests.get(base + '/health', timeout=1).status_code == 200,
                               'Gunicorn did not start')
                    denied = requests.post(base + '/telegram/webhook', json=telegram_update(555, '/latest'), timeout=5)
                    self.assertEqual(denied.status_code, 403)
                    forged = requests.post(base + '/telegram/webhook', json=telegram_update(999, '/latest'),
                                           headers=telegram_headers, timeout=5)
                    self.assertTrue(forged.json()['unauthorized'])

                    first = telnyx_event('http-message', 'webhook sample')
                    self.assertEqual(first.status_code, 200)
                    self.assertFalse(first.json()['duplicate'])
                    wait_until(lambda: state('http-message') == ('sent', 0), 'Webhook alert was not sent')
                    duplicate = telnyx_event('http-message', 'webhook sample')
                    self.assertTrue(duplicate.json()['duplicate'])
                    self.assertEqual(sum('http-message' in m['text'] for m in sent_messages()), 1)

                    sync = requests.post(base + '/telegram/webhook', json=telegram_update(555, '/syncsms 20'),
                                         headers=telegram_headers, timeout=5)
                    self.assertEqual(sync.status_code, 200)
                    wait_until(lambda: state('sync-message') == ('sent', 0), 'Sync alert was not sent')
                    wait_until(lambda: any('stored/updated: 1' in m['text'] for m in sent_messages()),
                               'Sync summary was not sent')

                    self.assertEqual(telnyx_event('failure-message', 'failure sample').status_code, 200)
                    wait_until(lambda: state('failure-message') == ('pending', 1), 'Failed alert was not retained')
                    self.assertEqual(telnyx_event('good-message', 'newer sample').status_code, 200)
                    wait_until(lambda: state('good-message') == ('sent', 0), 'A failed alert blocked a newer alert')
                    self.assertEqual(state('failure-message'), ('pending', 1))
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
