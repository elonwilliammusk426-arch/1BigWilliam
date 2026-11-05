from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

import alerts
import notify
import store
import telnyx_sync
from telnyx import TelnyxClient


# Import the Flask app without starting its long-running threads during unit tests.
with tempfile.TemporaryDirectory() as import_dir:
    previous_dir = os.getcwd()
    try:
        os.chdir(import_dir)
        with mock.patch.object(alerts, 'start_delivery_worker'), mock.patch.object(telnyx_sync, 'start_polling_if_enabled'):
            import app as bridge_app
    finally:
        os.chdir(previous_dir)


class FakeTelnyx:
    def __init__(self, records, messages):
        self.records = records
        self.messages = messages
        self.lookups = []
        self.list_arguments = None

    def list_messaging_detail_records(self, **kwargs):
        self.list_arguments = kwargs
        return self.records

    def get_message(self, message_id):
        self.lookups.append(message_id)
        result = self.messages[message_id]
        if isinstance(result, Exception):
            raise result
        return {'data': result}


def record(message_id='message-123', *, field='uuid', age=0):
    return {
        field: message_id,
        'record_type': 'message_detail_record',
        'created_at': (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat(),
        'direction': 'inbound',
        'cli': '+15550000001',
        'cld': '+15550000002',
    }


def message(text='example code 123456'):
    return {
        'direction': 'inbound',
        'from': {'phone_number': '+15550000001'},
        'to': [{'phone_number': '+15550000002'}],
        'text': text,
    }


def webhook(message_id='message-123', event_type='message.received'):
    return {
        'data': {
            'event_type': event_type,
            'record_type': 'event',
            'payload': dict(message(), id=message_id, record_type='message'),
        }
    }


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_dir = os.getcwd()
        os.chdir(self.temp.name)
        self.environment = mock.patch.dict(os.environ, {
            'OWNER_TELEGRAM_ID': '777',
            'TELEGRAM_ALERT_CHAT_ID': '555',
            'TELNYX_POLLING_ENABLED': 'false',
            'TELNYX_SYNC_LOOKBACK_HOURS': '48',
        }, clear=True)
        self.environment.start()
        store.init_db()
        bridge_app.app.config['TESTING'] = True

    def tearDown(self):
        self.environment.stop()
        os.chdir(self.previous_dir)
        self.temp.cleanup()


class SyncTests(DatabaseTestCase):
    def test_uuid_records_are_retrieved_queued_and_deduplicated(self):
        client = FakeTelnyx([record()], {'message-123': message()})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
            repeated = telnyx_sync.sync_inbound_once()

        self.assertEqual((result.returned, result.checked, result.stored, result.skipped), (1, 1, 1, 0))
        self.assertEqual((repeated.stored, repeated.skipped), (0, 1))
        self.assertEqual(client.lookups, ['message-123'])
        self.assertEqual(client.list_arguments['date_range'], 'last_3_days')
        self.assertEqual(store.recent_all()[0].body, 'example code 123456')
        self.assertEqual(store.pending_notification_count(), 1)
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
            self.assertIn('example code 123456', send.call_args.args[0])
        self.assertEqual(store.pending_notification_count(), 0)

    def test_id_records_and_explicit_date_range_remain_supported(self):
        client = FakeTelnyx([record(field='id')], {'message-123': message()})
        with mock.patch.dict(os.environ, {'TELNYX_SYNC_DATE_RANGE': 'yesterday'}), \
             mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once(notify_new=False)
        self.assertEqual(result.stored, 1)
        self.assertEqual(client.list_arguments['date_range'], 'yesterday')
        self.assertEqual(store.pending_notification_count(), 0)

    def test_missing_id_and_timestamp_are_reported(self):
        missing_id = record()
        del missing_id['uuid']
        missing_timestamp = record('message-456')
        del missing_timestamp['created_at']
        client = FakeTelnyx([missing_id, missing_timestamp], {})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual((result.returned, result.missing_ids, result.missing_timestamps, result.skipped), (2, 1, 1, 2))
        summary = bridge_app._syncsms_summary(result)
        self.assertIn('Records missing an id/uuid: 1', summary)
        self.assertIn('Records missing a usable timestamp: 1', summary)

    def test_missing_text_is_not_marked_processed_and_can_be_retried(self):
        client = FakeTelnyx([record()], {'message-123': message('')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            first = telnyx_sync.sync_inbound_once()
            self.assertEqual((first.unavailable_text, first.stored), (1, 1))
            self.assertFalse(store.has_complete_telnyx_message('message-123'))
            self.assertEqual(store.pending_notification_count(), 1)
            self.assertIn('redaction', bridge_app._syncsms_summary(first))
            client.messages['message-123'] = message('recovered text')
            second = telnyx_sync.sync_inbound_once()
        self.assertEqual(second.stored, 1)
        self.assertEqual(store.recent_all()[0].body, 'recovered text')

    def test_provider_errors_are_reported_without_secrets(self):
        client = FakeTelnyx([record()], {'message-123': requests.ConnectionError('secret-token in URL')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual(result.stored, 1)
        self.assertEqual(result.unavailable_text, 1)
        self.assertEqual(result.errors, ['acct1: message details unavailable (connection failed)'])
        self.assertNotIn('secret-token', bridge_app._syncsms_summary(result))
        self.assertTrue(store.has_telnyx_message('message-123'))
        self.assertFalse(store.has_complete_telnyx_message('message-123'))
        self.assertEqual(store.pending_notification_count(), 1)

    def test_account_wide_timeout_stops_repeated_slow_lookups(self):
        records = [record('one'), record('two'), record('three')]
        client = FakeTelnyx(records, {name: requests.Timeout() for name in ('one', 'two', 'three')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual(len(client.lookups), 1)
        self.assertIn('request timed out', result.errors[0])
        self.assertEqual(result.returned, 3)

    def test_old_and_outbound_records_are_not_processed(self):
        old = record('old', age=49)
        outbound = dict(record('outbound'), direction='outbound')
        client = FakeTelnyx([old, outbound], {})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual((result.checked, result.skipped), (0, 2))
        self.assertEqual(client.lookups, [])

    def test_naive_timestamp_is_treated_as_utc(self):
        rec = record()
        rec['created_at'] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        self.assertTrue(telnyx_sync._record_is_within_window(rec, 48))

    def test_legacy_account_specific_id_is_not_replayed(self):
        with sqlite3.connect('legacy.db') as conn:
            conn.execute(
                'CREATE TABLE inbound (id INTEGER PRIMARY KEY, to_number TEXT NOT NULL, '
                'from_number TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, '
                'provider_message_id TEXT)'
            )
            conn.execute(
                'INSERT INTO inbound VALUES (1, ?, ?, ?, ?, ?)',
                ('+15550000002', '+15550000001', 'old text', datetime.now(timezone.utc).isoformat(),
                 'telnyx:acct1:KEYold...1234:message-123'),
            )
        store.init_db('legacy.db')
        row_id, created = store.save_telnyx_message(
            '+15550000002', '+15550000001', 'old text', message_id='message-123', path='legacy.db'
        )
        self.assertEqual((row_id, created), (1, False))
        self.assertEqual(store.pending_notification_count('legacy.db'), 0)

    def test_client_requests_latest_records_and_validates_response(self):
        client = TelnyxClient('dummy-key')
        response = mock.Mock()
        response.json.return_value = {'data': [record()]}
        client.session.get = mock.Mock(return_value=response)
        self.assertEqual(len(client.list_messaging_detail_records(date_range='last_3_days', direction='inbound')), 1)
        params = client.session.get.call_args.kwargs['params']
        self.assertEqual(params['sort'], '-created_at')
        self.assertEqual(params['filter[record_type]'], 'messaging')
        self.assertEqual(params['filter[date_range]'], 'last_3_days')
        response.json.return_value = {'data': None}
        with self.assertRaises(ValueError):
            client.list_messaging_detail_records()


class NotificationTests(DatabaseTestCase):
    def test_failed_alert_remains_queued_until_telegram_accepts_it(self):
        store.save_telnyx_message('+15550000002', '+15550000001', 'hello', message_id='one')
        with mock.patch.object(notify, 'notify_owner', side_effect=[False, True]) as send:
            self.assertEqual(alerts.deliver_pending(), 0)
            self.assertEqual(store.pending_notification_count(), 1)
            # Failed alerts have a persisted retry delay; simulate its expiry.
            self.assertEqual(alerts.deliver_pending(), 0)
            with sqlite3.connect('inbound.db') as conn:
                conn.execute("UPDATE inbound SET notification_retry_at = '2000-01-01T00:00:00+00:00'")
            self.assertEqual(alerts.deliver_pending(), 1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(store.pending_notification_count(), 0)
        self.assertEqual(alerts.deliver_pending(), 0)

    def test_concurrent_delivery_claims_do_not_send_twice(self):
        store.save_telnyx_message('+15550000002', '+15550000001', 'hello', message_id='one')
        entered = threading.Event()
        release = threading.Event()

        def send(_):
            entered.set()
            self.assertTrue(release.wait(5))
            return True

        with mock.patch.object(notify, 'notify_owner', side_effect=send) as send_mock:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(alerts.deliver_pending)
                self.assertTrue(entered.wait(5))
                second = executor.submit(alerts.deliver_pending)
                self.assertEqual(second.result(timeout=5), 0)
                release.set()
                self.assertEqual(first.result(timeout=5), 1)
        self.assertEqual(send_mock.call_count, 1)

    def test_expired_lease_can_be_retried_and_old_owner_cannot_finalize(self):
        store.save_telnyx_message('+15550000002', '+15550000001', 'hello', message_id='one')
        first = store.claim_next_notification()
        self.assertIsNotNone(first)
        with sqlite3.connect('inbound.db') as conn:
            conn.execute("UPDATE inbound SET notification_claimed_at = '2000-01-01T00:00:00+00:00'")
        second = store.claim_next_notification()
        self.assertIsNotNone(second)
        self.assertFalse(store.finish_notification(first, delivered=True))
        self.assertTrue(store.finish_notification(second, delivered=True))
        self.assertEqual(store.pending_notification_count(), 0)

    def test_upgrading_missing_text_invalidates_an_inflight_empty_alert(self):
        store.save_telnyx_message('+15550000002', '+15550000001', '', message_id='one')
        empty_claim = store.claim_next_notification()
        self.assertIsNotNone(empty_claim)
        _, updated = store.save_telnyx_message('+15550000002', '+15550000001', 'actual code', message_id='one')
        self.assertTrue(updated)
        self.assertFalse(store.finish_notification(empty_claim, delivered=True))
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
        self.assertIn('actual code', send.call_args.args[0])

    def test_atomic_insert_deduplicates_concurrent_webhook_and_sync(self):
        def save():
            return store.save_telnyx_message('+15550000002', '+15550000001', 'hello', message_id='one')
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: save(), range(16)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({row_id for row_id, _ in results}), 1)
        self.assertEqual(store.pending_notification_count(), 1)

    def test_telegram_failures_are_detected_and_do_not_log_tokens(self):
        with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'private-token', 'TELEGRAM_ALERT_CHAT_ID': '123'}):
            with mock.patch.object(notify.requests, 'post', side_effect=requests.ConnectionError('private-token code 123456')):
                with self.assertLogs('notify', level='WARNING') as logs:
                    self.assertFalse(notify.notify_owner('code 123456'))
                self.assertNotIn('private-token', str(logs.output))
                self.assertNotIn('123456', str(logs.output))
            response = SimpleNamespace(ok=False, status_code=403)
            with mock.patch.object(notify.requests, 'post', return_value=response):
                self.assertFalse(notify.notify_owner('hello'))
            response = SimpleNamespace(ok=True, status_code=200, json=lambda: {'ok': False})
            with mock.patch.object(notify.requests, 'post', return_value=response):
                self.assertFalse(notify.notify_owner('hello'))
            response = SimpleNamespace(ok=True, status_code=200, json=lambda: {'ok': True})
            with mock.patch.object(notify.requests, 'post', return_value=response) as post:
                self.assertTrue(notify.notify_owner('x' * 8000))
                self.assertEqual(post.call_count, 3)


class WebhookTests(DatabaseTestCase):
    def test_webhook_is_queued_then_sync_and_retry_do_not_duplicate_alerts(self):
        with bridge_app.app.test_client() as client:
            first = client.post('/inbound/sms', json=webhook())
            repeated = client.post('/inbound/sms', json=webhook())
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json['duplicate'])
        self.assertTrue(repeated.json['duplicate'])
        self.assertEqual(len(store.recent_all()), 1)
        self.assertEqual(store.pending_notification_count(), 1)
        fake = FakeTelnyx([record()], {'message-123': message()})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]):
            self.assertEqual(telnyx_sync.sync_inbound_once().stored, 0)
        self.assertEqual(fake.lookups, [])
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
            self.assertEqual(send.call_count, 1)

    def test_empty_webhook_can_be_enriched_by_later_sync(self):
        payload = webhook()
        payload['data']['payload']['text'] = ''
        with bridge_app.app.test_client() as client:
            self.assertEqual(client.post('/inbound/sms', json=payload).status_code, 200)
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
        self.assertIn('text unavailable', send.call_args.args[0])
        fake = FakeTelnyx([record()], {'message-123': message('recovered code')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]):
            self.assertEqual(telnyx_sync.sync_inbound_once().stored, 1)
        self.assertEqual(len(store.recent_all()), 1)
        self.assertEqual(store.recent_all()[0].body, 'recovered code')
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
        self.assertIn('recovered code', send.call_args.args[0])

    def test_sync_before_webhook_also_deduplicates(self):
        fake = FakeTelnyx([record()], {'message-123': message()})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]):
            self.assertEqual(telnyx_sync.sync_inbound_once().stored, 1)
        with bridge_app.app.test_client() as client:
            response = client.post('/inbound/sms', json=webhook())
        self.assertTrue(response.json['duplicate'])
        self.assertEqual(len(store.recent_all()), 1)

    def test_outbound_events_and_invalid_signatures_are_not_queued(self):
        with bridge_app.app.test_client() as client:
            response = client.post('/inbound/sms', json=webhook(event_type='message.finalized'))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json['ignored'])
            with mock.patch.object(bridge_app, 'verify_telnyx_signature', return_value=False):
                response = client.post('/inbound/sms', json=webhook())
                self.assertEqual(response.status_code, 403)
        self.assertEqual(store.pending_notification_count(), 0)

    def test_ed25519_signed_webhook_is_accepted_and_tampering_is_rejected(self):
        import base64
        import json
        import time
        from nacl.signing import SigningKey

        signing_key = SigningKey.generate()
        timestamp = str(int(time.time()))
        body = json.dumps(webhook()).encode('utf-8')
        signature = signing_key.sign(timestamp.encode('utf-8') + b'|' + body).signature
        headers = {
            'Content-Type': 'application/json',
            'Telnyx-Timestamp': timestamp,
            'Telnyx-Signature-Ed25519': base64.b64encode(signature).decode('ascii'),
        }
        key = base64.b64encode(bytes(signing_key.verify_key)).decode('ascii')
        with mock.patch.dict(os.environ, {'TELNYX_PUBLIC_KEY': key}):
            with bridge_app.app.test_client() as client:
                valid = client.post('/inbound/sms', data=body, headers=headers)
                invalid = client.post('/inbound/sms', data=body + b' ', headers=headers)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(invalid.status_code, 403)
        self.assertEqual(store.pending_notification_count(), 1)

    def test_testalert_reports_the_actual_delivery_result(self):
        update = {'message': {'chat': {'id': 555}, 'from': {'id': 777}, 'text': '/testalert'}}
        with mock.patch.object(notify, 'notify_owner', return_value=False), \
             mock.patch.object(bridge_app, 'send_telegram') as reply:
            with bridge_app.app.test_client() as client:
                self.assertEqual(client.post('/telegram/webhook', json=update).status_code, 200)
        self.assertIn('Test alert failed', reply.call_args.args[1])
        with mock.patch.object(notify, 'notify_owner', return_value=True), \
             mock.patch.object(bridge_app, 'send_telegram') as reply:
            with bridge_app.app.test_client() as client:
                client.post('/telegram/webhook', json=update)
        self.assertIn('accepted', reply.call_args.args[1])

    def test_sync_command_eventually_reports_a_real_sync_result(self):
        fake = FakeTelnyx([record()], {'message-123': message()})
        done = threading.Event()
        replies = []

        def reply(chat_id, text):
            replies.append(text)
            if text.startswith('Sync finished'):
                done.set()
            return True

        update = {'message': {'chat': {'id': 555}, 'from': {'id': 777}, 'text': '/syncsms 20'}}
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]), \
             mock.patch.object(bridge_app, 'send_telegram', side_effect=reply):
            with bridge_app.app.test_client() as client:
                self.assertEqual(client.post('/telegram/webhook', json=update).status_code, 200)
            self.assertTrue(done.wait(5))
        self.assertTrue(any('Sync started' in text for text in replies))
        self.assertTrue(any('stored/updated: 1' in text for text in replies))
        self.assertEqual(store.pending_notification_count(), 1)

    def test_summary_explains_when_no_records_are_returned(self):
        summary = bridge_app._syncsms_summary(telnyx_sync.SyncResult(accounts=1))
        self.assertIn('records returned: 0', summary)
        self.assertIn('no inbound records', summary)


class WorkerIntegrationTest(unittest.TestCase):
    def test_real_worker_wakes_for_webhook_and_marks_delivery(self):
        repo = Path(__file__).resolve().parents[1]
        script = r'''
import os
import threading
from types import SimpleNamespace
from unittest import mock
import requests
import store
finished = threading.Event()
original_finish = store.finish_notification

def finish(*args, **kwargs):
    result = original_finish(*args, **kwargs)
    finished.set()
    return result

response = SimpleNamespace(ok=True, status_code=200, json=lambda: {'ok': True})
with mock.patch.object(requests, 'post', return_value=response) as post, \
     mock.patch.object(store, 'finish_notification', side_effect=finish):
    import app
    payload = {'data': {'event_type': 'message.received', 'payload': {
        'id': 'worker-message', 'from': {'phone_number': '+15550000001'},
        'to': [{'phone_number': '+15550000002'}], 'text': 'hello worker'}}}
    with app.app.test_client() as client:
        assert client.post('/inbound/sms', json=payload).status_code == 200
    assert finished.wait(5), 'background worker did not deliver the alert'
    assert post.call_count == 1
    assert store.pending_notification_count() == 0
'''
        with tempfile.TemporaryDirectory() as directory:
            env = {
                'PATH': os.environ.get('PATH', ''),
                'PYTHONPATH': str(repo),
                'TELEGRAM_BOT_TOKEN': 'dummy-token',
                'TELEGRAM_ALERT_CHAT_ID': '123',
                'TELNYX_POLLING_ENABLED': 'false',
            }
            result = subprocess.run(
                [sys.executable, '-c', script], cwd=directory, env=env,
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_failed_alert_survives_restart_with_volume_path(self):
        repo = Path(__file__).resolve().parents[1]
        script = r'''
import os
import threading
from types import SimpleNamespace
from unittest import mock
import requests
import store
finished = threading.Event()
original_finish = store.finish_notification

def finish(*args, **kwargs):
    result = original_finish(*args, **kwargs)
    finished.set()
    return result

ok = os.environ['TEST_DELIVERY'] == 'success'
response = SimpleNamespace(ok=ok, status_code=200 if ok else 403, json=lambda: {'ok': ok})
with mock.patch.object(requests, 'post', return_value=response), \
     mock.patch.object(store, 'finish_notification', side_effect=finish):
    import app
    if not ok:
        payload = {'data': {'event_type': 'message.received', 'payload': {
            'id': 'restart-message', 'from': {'phone_number': '+15550000001'},
            'to': [{'phone_number': '+15550000002'}], 'text': 'hello'}}}
        with app.app.test_client() as client:
            assert client.post('/inbound/sms', json=payload).status_code == 200
    assert finished.wait(5), 'worker did not attempt delivery'
    assert store.pending_notification_count() == (0 if ok else 1)
'''
        with tempfile.TemporaryDirectory() as directory:
            volume = Path(directory) / 'volume'
            volume.mkdir()
            env = {
                'PATH': os.environ.get('PATH', ''),
                'PYTHONPATH': str(repo),
                'TELEGRAM_BOT_TOKEN': 'dummy-token',
                'TELEGRAM_ALERT_CHAT_ID': '123',
                'TELNYX_POLLING_ENABLED': 'false',
                'INBOUND_DB_PATH': str(volume / 'inbound.db'),
            }
            for outcome in ('failure', 'success'):
                result = subprocess.run(
                    [sys.executable, '-c', script], cwd=directory,
                    env=dict(env, TEST_DELIVERY=outcome), capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if outcome == 'failure':
                    # Retry state survives a restart; advance the scheduled time
                    # rather than sleeping for the real 30-second interval.
                    with sqlite3.connect(volume / 'inbound.db') as conn:
                        self.assertIsNotNone(conn.execute(
                            'SELECT notification_retry_at FROM inbound'
                        ).fetchone()[0])
                        conn.execute("UPDATE inbound SET notification_retry_at = '2000-01-01T00:00:00+00:00'")


if __name__ == '__main__':
    unittest.main()
