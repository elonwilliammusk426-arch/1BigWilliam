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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from nacl.signing import SigningKey


# Inject deterministic provider responses into the Gunicorn child only. No
# real Telnyx or Telegram credentials, requests, phone numbers, or SMS are used.
FAKE_PROVIDERS = r'''
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
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
    if url.endswith('/available_phone_numbers'):
        # Simulate a slow provider while another HTTP request arrives. The
        # parent test releases this call only after probing webhook handling.
        Path(os.environ['TEST_BLOCK_ENTERED']).touch()
        deadline = time.monotonic() + 10
        while not Path(os.environ['TEST_BLOCK_RELEASE']).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return Response({'data': []})
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
            block_entered = root / 'block-entered'
            block_release = root / 'block-release'
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
                'TEST_BLOCK_ENTERED': str(block_entered),
                'TEST_BLOCK_RELEASE': str(block_release),
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

            def telnyx_event(message_id, text, timeout=5, received_at=None):
                payload = {'data': {'event_type': 'message.received', 'payload': {
                    'id': message_id, 'direction': 'inbound',
                    'from': {'phone_number': '+15550000001'},
                    'to': [{'phone_number': '+15550000002'}], 'text': text,
                }}}
                if received_at is not None:
                    payload['data']['payload']['received_at'] = received_at
                body = json.dumps(payload).encode()
                timestamp = str(int(time.time()))
                signature = signing_key.sign(timestamp.encode() + b'|' + body).signature
                headers = {
                    'Content-Type': 'application/json', 'Telnyx-Timestamp': timestamp,
                    'Telnyx-Signature-Ed25519': base64.b64encode(signature).decode(),
                }
                return requests.post(base + '/inbound/sms', data=body, headers=headers, timeout=timeout)

            def telegram_update(chat_id, text):
                return {'message': {'chat': {'id': chat_id}, 'from': {'id': 777}, 'text': text}}

            start_command = json.loads((repo / 'railway.json').read_text())['deploy']['startCommand']
            self.assertEqual((repo / 'Procfile').read_text().strip(), f'web: {start_command}')
            command = shlex.split(start_command)
            self.assertEqual(command[0], 'gunicorn')
            command = [sys.executable, '-m', 'gunicorn'] + [arg.replace('$PORT', str(port)) for arg in command[1:]]
            with server_log.open('w') as logs:
                process = subprocess.Popen(
                    command, cwd=directory, env=env, stdout=subprocess.DEVNULL, stderr=logs,
                )
                try:
                    wait_until(lambda: requests.get(base + '/health', timeout=1).status_code == 200,
                               'Gunicorn did not start')
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        command_request = pool.submit(
                            requests.post, base + '/telegram/webhook', json=telegram_update(555, '/available'),
                            headers=telegram_headers, timeout=12,
                        )
                        wait_until(block_entered.exists, 'Slow provider call did not start')
                        try:
                            try:
                                concurrent_webhook = telnyx_event('concurrent-message', 'concurrent sample', timeout=1.5)
                            except requests.Timeout:
                                self.fail('A slow owner command blocked the Telnyx webhook beyond its timeout')
                            self.assertEqual(concurrent_webhook.status_code, 200)
                        finally:
                            block_release.touch()
                        self.assertEqual(command_request.result(timeout=5).status_code, 200)
                    wait_until(lambda: state('concurrent-message') == ('sent', 0),
                               'Concurrent webhook alert was not sent')
                    late = telnyx_event(
                        'late-message', 'late sample',
                        received_at=(datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(),
                    )
                    self.assertEqual(late.status_code, 200)
                    self.assertFalse(late.json()['duplicate'])
                    wait_until(lambda: state('late-message') == ('sent', 0), 'A late webhook was not delivered')
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

                    with ThreadPoolExecutor(max_workers=8) as pool:
                        responses = list(pool.map(lambda _: telnyx_event('race-message', 'same sample'), range(8)))
                    self.assertTrue(all(response.status_code == 200 for response in responses))
                    self.assertEqual(sum(not response.json()['duplicate'] for response in responses), 1)
                    wait_until(lambda: state('race-message') == ('sent', 0), 'Concurrent duplicates were not delivered')
                    self.assertEqual(sum('race-message' in m['text'] for m in sent_messages()), 1)

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
                        exit_code = process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                        self.fail('Gunicorn did not stop after the HTTP requests finished')
                    self.assertEqual(exit_code, 0, 'Gunicorn did not shut down cleanly')


if __name__ == '__main__':
    unittest.main()
