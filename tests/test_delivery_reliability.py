from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import requests

import alerts
import notify
import store
import telnyx_sync
from test_bridge import DatabaseTestCase, FakeTelnyx, bridge_app, message, record, webhook


class DeliveryReliabilityTests(DatabaseTestCase):
    def test_three_then_seven_checked_only_notifies_for_the_four_new_messages(self):
        records = [record(f'm-{i}') for i in range(3)]
        messages = {f'm-{i}': message(f'fixture body {i}') for i in range(7)}
        client = FakeTelnyx(records, messages)
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]), \
             mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            first = telnyx_sync.sync_inbound_once()
            self.assertEqual((first.checked, first.stored), (3, 3))
            self.assertEqual(alerts.deliver_pending(), 3)
            client.records.extend(record(f'm-{i}') for i in range(3, 7))
            second = telnyx_sync.sync_inbound_once()
            self.assertEqual((second.checked, second.stored, second.skipped), (7, 4, 3))
            self.assertEqual(second.already_stored, 3)
            self.assertEqual(alerts.deliver_pending(), 4)
            third = telnyx_sync.sync_inbound_once()
            self.assertEqual((third.checked, third.stored, third.already_stored), (7, 0, 7))
            self.assertEqual(alerts.deliver_pending(), 0)
        self.assertEqual(send.call_count, 7)
        notices = [call.args[0] for call in send.call_args_list]
        for i in range(7):
            self.assertEqual(sum(f'fixture body {i}' in text for text in notices), 1)
        self.assertIn('includes previously stored', bridge_app._syncsms_summary(second))

    def test_existing_legacy_rows_are_not_replayed_but_missing_text_can_be_enriched(self):
        store.save_message(
            '+15550000002', '+15550000001', 'existing fixture',
            provider_message_id='telnyx:acct1:legacy-fixture:complete',
        )
        store.save_message(
            '+15550000002', '+15550000001', store.MISSING_BODY,
            provider_message_id='telnyx:acct1:legacy-fixture:incomplete',
        )
        client = FakeTelnyx([record('complete'), record('incomplete')], {'incomplete': message('')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            first = telnyx_sync.sync_inbound_once()
            self.assertEqual((first.checked, first.stored, first.already_stored), (2, 0, 1))
            self.assertEqual(store.pending_notification_count(), 0)
            client.messages['incomplete'] = message('recovered legacy fixture')
            self.assertEqual(telnyx_sync.sync_inbound_once().stored, 1)
        self.assertEqual(store.pending_notification_count(), 1)
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
            self.assertIn('recovered legacy fixture', send.call_args.args[0])
        self.assertEqual(send.call_count, 1)

    def test_unavailable_text_is_not_silent_and_later_body_is_delivered_once(self):
        client = FakeTelnyx([record()], {'message-123': message('')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]), \
             mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            first = telnyx_sync.sync_inbound_once()
            self.assertEqual((first.checked, first.unavailable_text, first.stored), (1, 1, 1))
            self.assertTrue(store.body_is_incomplete(store.recent_all()[0].body))
            self.assertFalse(store.has_complete_telnyx_message('message-123'))
            self.assertEqual(alerts.deliver_pending(), 1)
            self.assertIn('text unavailable', send.call_args.args[0].lower())
            repeated = telnyx_sync.sync_inbound_once()
            self.assertEqual((repeated.unavailable_text, repeated.stored), (1, 0))
            self.assertEqual(alerts.deliver_pending(), 0)
            client.messages['message-123'] = message('recovered fixture body')
            recovered = telnyx_sync.sync_inbound_once()
            self.assertEqual(recovered.stored, 1)
            self.assertEqual(alerts.deliver_pending(), 1)
            self.assertIn('recovered fixture body', send.call_args.args[0])
            self.assertEqual(telnyx_sync.sync_inbound_once().stored, 0)
            self.assertEqual(alerts.deliver_pending(), 0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(store.recent_all()[0].body, 'recovered fixture body')

    def test_account_timeout_still_queues_known_messages_without_repeated_slow_lookups(self):
        names = ('one', 'two', 'three')
        client = FakeTelnyx([record(name) for name in names], {
            name: requests.Timeout('private-token in request URL') for name in names
        })
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual(client.lookups, ['one'])
        self.assertEqual((result.checked, result.stored, result.unavailable_text), (3, 3, 3))
        self.assertEqual(store.pending_notification_count(), 3)
        self.assertIn('request timed out', result.errors[0])
        self.assertNotIn('private-token', bridge_app._syncsms_summary(result))
        self.assertTrue(all(store.body_is_incomplete(row.body) for row in store.recent_all()))

    def test_unusable_detail_phone_fields_fall_back_to_record_and_nested_text_is_read(self):
        nested = dict(record('nested'), body={'text': 'nested fixture body'})
        incomplete_phones = {
            'direction': 'inbound', 'from': {'phone_number': None},
            'to': [{'phone_number': ''}], 'body': {'text': 'detail fixture body'},
        }
        client = FakeTelnyx([nested, record('phones')], {'phones': incomplete_phones})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual(result.stored, 2)
        self.assertEqual(client.lookups, ['phones'])
        rows = store.recent_all()
        self.assertEqual({row.body for row in rows}, {'nested fixture body', 'detail fixture body'})
        self.assertTrue(all(row.from_number == '+15550000001' and row.to_number == '+15550000002' for row in rows))

    def test_missing_sender_or_recipient_is_reported_without_fabricating_an_alert(self):
        entry = record()
        del entry['cli']
        del entry['cld']
        client = FakeTelnyx([entry], {'message-123': {'direction': 'inbound', 'text': ''}})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual((result.checked, result.stored, result.skipped, result.unavailable_text), (1, 0, 1, 1))
        self.assertIn('missing a sender or recipient', result.errors[0])
        self.assertEqual(store.pending_notification_count(), 0)
        self.assertEqual(store.recent_all(), [])

    def test_body_recovery_replaces_a_failed_incomplete_alert_without_retry_delay(self):
        client = FakeTelnyx([record()], {'message-123': message('')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            telnyx_sync.sync_inbound_once()
            with mock.patch.object(notify, 'notify_owner', return_value=False):
                self.assertEqual(alerts.deliver_pending(), 0)
            self.assertEqual(store.pending_notification_count(), 1)
            client.messages['message-123'] = message('complete fixture text')
            self.assertEqual(telnyx_sync.sync_inbound_once().stored, 1)
            with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
                self.assertEqual(alerts.deliver_pending(), 1)
                self.assertIn('complete fixture text', send.call_args.args[0])
                self.assertNotIn('text unavailable', send.call_args.args[0].lower())
        self.assertEqual(store.pending_notification_count(), 0)

    def test_many_bad_accounts_do_not_block_a_valid_account_or_hide_their_errors(self):
        class Unauthorized(FakeTelnyx):
            def list_messaging_detail_records(self, **kwargs):
                response = requests.Response()
                response.status_code = 401
                raise requests.HTTPError('private-key in request URL', response=response)

        bad = Unauthorized([], {})
        good = FakeTelnyx([record('good')], {'good': message('valid fixture body')})
        clients = [(f'acct{i}', bad if i < 26 else good) for i in range(1, 27)]
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=clients):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual((result.accounts, result.checked, result.stored), (26, 1, 1))
        self.assertEqual(len(result.errors), 25)
        summary = bridge_app._syncsms_summary(result)
        self.assertIn('acct25', summary)
        self.assertNotIn('private-key', summary)
        self.assertEqual(store.pending_notification_count(), 1)
        self.assertEqual(store.recent_all()[0].body, 'valid fixture body')

    def test_real_webhook_wins_if_detail_lookup_fails_after_it_arrives(self):
        entered, release = threading.Event(), threading.Event()

        class SlowFailure(FakeTelnyx):
            def get_message(self, message_id):
                entered.set()
                if not release.wait(3):
                    raise AssertionError('test did not release lookup')
                raise requests.Timeout('fake timeout')

        client = SlowFailure([record()], {})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(telnyx_sync.sync_inbound_once)
                self.assertTrue(entered.wait(3))
                event = webhook()
                event['data']['payload']['text'] = 'actual webhook fixture'
                with bridge_app.app.test_client() as http:
                    self.assertEqual(http.post('/inbound/sms', json=event).status_code, 200)
                release.set()
                future.result(timeout=3)
        self.assertEqual(len(store.recent_all()), 1)
        self.assertEqual(store.recent_all()[0].body, 'actual webhook fixture')
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
            self.assertIn('actual webhook fixture', send.call_args.args[0])
            self.assertEqual(alerts.deliver_pending(), 0)
        self.assertEqual(send.call_count, 1)

    def test_summary_explains_checked_and_reports_all_26_sanitized_account_errors(self):
        result = telnyx_sync.SyncResult(
            accounts=26, checked=7, stored=4, skipped=3, unavailable_text=1,
            errors=[f'acct{i}: list records failed (HTTP 401)' for i in range(1, 27)],
        )
        summary = bridge_app._syncsms_summary(result, chat_id='777')
        self.assertIn('checked: 7', summary)
        self.assertIn('includes previously stored', summary)
        self.assertIn('not the same as delivered', summary)
        self.assertIn('acct26', summary)
        self.assertIn('different chat', summary)
        self.assertIn('text unavailable', summary.lower())
        result.errors.extend(f'acct{i}: list records failed (HTTP 401)' for i in range(27, 52))
        capped = bridge_app._syncsms_summary(result)
        self.assertIn('acct50', capped)
        self.assertNotIn('acct51', capped)
        self.assertIn('1 more not shown', capped)

    def test_alert_destination_comparison_never_redirects_or_discloses_ids(self):
        self.assertIn('this chat', bridge_app._alert_chat_note('555'))
        note = bridge_app._alert_chat_note('777')
        self.assertIn('different chat', note)
        self.assertNotIn('555', note)
        with mock.patch.dict(os.environ, {'TELEGRAM_ALERT_CHAT_ID': '@fixture_channel'}):
            self.assertIn('may be a different chat', bridge_app._alert_chat_note('777'))
        with mock.patch.dict(os.environ, {'TELEGRAM_ALERT_CHAT_ID': ''}):
            self.assertIn('this chat', bridge_app._alert_chat_note('777'))
        with mock.patch.dict(os.environ, {'TELEGRAM_ALERT_CHAT_ID': '', 'OWNER_TELEGRAM_ID': 'invalid'}):
            self.assertIn('No alert destination', bridge_app._alert_chat_note('777'))

    def test_testalert_tells_the_owner_when_alerts_go_to_a_different_chat(self):
        update = {'message': {'chat': {'id': 777}, 'from': {'id': 777}, 'text': '/testalert'}}
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send_alert, \
             mock.patch.object(bridge_app, 'send_telegram') as reply, bridge_app.app.test_client() as http:
            self.assertEqual(http.post('/telegram/webhook', json=update).status_code, 200)
        send_alert.assert_called_once()
        self.assertEqual(reply.call_args.args[0], 777)
        self.assertIn('different chat', reply.call_args.args[1])
        self.assertNotIn('555', reply.call_args.args[1])

    def test_polling_start_is_idempotent_and_reports_sanitized_failures(self):
        fake_thread = mock.Mock()
        fake_thread.is_alive.return_value = True
        with mock.patch.dict(os.environ, {'TELNYX_POLLING_ENABLED': 'true'}), \
             mock.patch.object(telnyx_sync, '_poll_thread', None), \
             mock.patch.object(telnyx_sync, '_last_poll', None), \
             mock.patch.object(telnyx_sync.threading, 'Thread', return_value=fake_thread) as thread_factory:
            telnyx_sync.start_polling_if_enabled()
            telnyx_sync.start_polling_if_enabled()
            self.assertEqual(thread_factory.call_count, 1)
            fake_thread.start.assert_called_once_with()
            self.assertTrue(telnyx_sync.polling_status()['running'])
            telnyx_sync._record_poll(None, requests.Timeout('private-token in URL'))
            snapshot = telnyx_sync.polling_status()
        self.assertEqual(snapshot['errors'], 1)
        self.assertEqual(snapshot['failure'], 'request timed out')
        self.assertNotIn('private-token', str(snapshot))

    def test_status_distinguishes_configured_polling_and_live_workers(self):
        snapshot = {
            'running': True, 'last_completed_at': '2026-11-05T00:00:00+00:00',
            'checked': 7, 'stored': 4, 'errors': 2,
        }
        with mock.patch.dict(os.environ, {'TELNYX_POLLING_ENABLED': 'true'}), \
             mock.patch.object(telnyx_sync, 'polling_status', return_value=snapshot), \
             mock.patch.object(alerts, 'delivery_worker_running', return_value=True):
            status = bridge_app._status_text(chat_id='777')
        self.assertIn('Polling worker: running', status)
        self.assertIn('Alert worker: running', status)
        self.assertIn('checked 7', status)
        self.assertIn('errors 2', status)
        self.assertIn('different chat', status)


# Exercise the actual automatic worker and Telegram queue in a fresh process. The
# providers, credentials and messages are fixtures; no external requests are made.
AUTOMATIC_WORKER_SCRIPT = r'''
import copy
import os
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

import requests

lock = threading.Lock()
records = []
messages = {}
sent = []

class FakeResponse:
    status_code = 200
    ok = True
    def __init__(self, data):
        self.data = data
    def raise_for_status(self):
        return None
    def json(self):
        return self.data

def fake_get(self, url, **kwargs):
    path = urlparse(url).path
    with lock:
        if path.endswith('/detail_records'):
            return FakeResponse({'data': copy.deepcopy(records), 'meta': {'total_pages': 1}})
        message_id = path.rsplit('/', 1)[-1]
        return FakeResponse({'data': copy.deepcopy(messages[message_id])})

def fake_post(url, **kwargs):
    with lock:
        sent.append(kwargs['json']['text'])
    return FakeResponse({'ok': True, 'result': {}})

requests.Session.get = fake_get
requests.post = fake_post

def add_record(i, text):
    name = 'fixture-' + str(i)
    with lock:
        records.append({
            'uuid': name, 'record_type': 'message_detail_record', 'direction': 'inbound',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'cli': '+15550000001', 'cld': '+15550000002',
        })
        messages[name] = {
            'direction': 'inbound', 'from': {'phone_number': '+15550000001'},
            'to': [{'phone_number': '+15550000002'}], 'text': text,
        }

def wait_for(predicate):
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('automatic delivery did not reach the expected state')

def count():
    with lock:
        return len(sent)

for i in range(3):
    add_record(i, 'fixture body ' + str(i))

# Accelerate only the sync module's sleep in this subprocess. Production keeps
# its normal five-second startup delay and minimum 15-second polling interval.
import telnyx_sync
telnyx_sync.time = SimpleNamespace(sleep=lambda seconds: time.sleep(0.03))
import app

wait_for(lambda: count() == 3)
for i in range(3, 7):
    add_record(i, 'fixture body ' + str(i))
wait_for(lambda: count() == 7)
time.sleep(0.15)
assert count() == 7, 'already-notified records were replayed'

add_record(7, '')
wait_for(lambda: count() == 8)
with lock:
    assert 'text unavailable' in sent[-1].lower()
    messages['fixture-7']['text'] = 'recovered fixture body'
wait_for(lambda: count() == 9)
assert 'recovered fixture body' in sent[-1]

with app.app.test_client() as http:
    event = {'data': {'event_type': 'message.received', 'payload': {
        'id': 'webhook-fixture', 'direction': 'inbound',
        'from': {'phone_number': '+15550000001'},
        'to': [{'phone_number': '+15550000002'}], 'text': 'webhook fixture body',
    }}}
    assert http.post('/inbound/sms', json=event).status_code == 200
wait_for(lambda: count() == 10)
time.sleep(0.15)
assert count() == 10
with lock:
    all_sent = list(sent)
for i in range(7):
    assert sum('fixture body ' + str(i) in text for text in all_sent) == 1
assert sum('recovered fixture body' in text for text in all_sent) == 1
assert sum('webhook fixture body' in text for text in all_sent) == 1
assert telnyx_sync.polling_status()['running']
print('automatic fixture delivery passed')
'''


class AutomaticWorkerIntegrationTests(unittest.TestCase):
    def test_automatic_polling_and_webhook_delivery_without_manual_sync(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as root:
            env = {
                **os.environ,
                'PYTHONPATH': str(repo),
                'INBOUND_DB_PATH': str(Path(root) / 'inbound.db'),
                'TELNYX_API_KEYS': '', 'TELNYX_API_KEY': 'fake-api-key', 'TELNYX_EXTRA_API_KEYS': '',
                'TELNYX_BASE_URL': 'https://fixture.invalid/v2', 'TELNYX_SYNC_DATE_RANGE': '',
                'TELNYX_SYNC_LOOKBACK_HOURS': '48',
                'TELNYX_POLLING_ENABLED': 'true', 'TELNYX_POLL_INTERVAL_SECONDS': '15', 'TELNYX_POLL_LIMIT': '20',
                'TELNYX_PUBLIC_KEYS': '', 'TELNYX_PUBLIC_KEY': '', 'TELNYX_EXTRA_PUBLIC_KEYS': '',
                'TELNYX_REQUIRE_SIGNATURE': 'false',
                'TELEGRAM_BOT_TOKEN': 'fake-bot-token', 'OWNER_TELEGRAM_ID': '777',
                'TELEGRAM_ALERT_CHAT_ID': '555', 'TELEGRAM_WEBHOOK_SECRET': '', 'TELEGRAM_REQUIRE_SECRET': 'false',
            }
            result = subprocess.run(
                [sys.executable, '-c', AUTOMATIC_WORKER_SCRIPT], cwd=root, env=env,
                text=True, capture_output=True, timeout=12,
            )
        self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)
        self.assertIn('automatic fixture delivery passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
