from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import subprocess
import sys
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
import set_telegram_webhook
import store
import telegram_chat_id
import telnyx_sync
import telnyx_webhook
from config import load_config
from telnyx import PaginationLimitError, PartialDetailRecordsError, TelnyxClient
from test_bridge import DatabaseTestCase, FakeTelnyx, bridge_app, message, record, webhook


def update(chat_id=555, user_id=777, text='/latest'):
    return {'message': {'chat': {'id': chat_id, 'type': 'private'}, 'from': {'id': user_id}, 'text': text}}


def response(data, *, ok=True, status=200):
    return SimpleNamespace(ok=ok, status_code=status, json=lambda: data, raise_for_status=lambda: None)


class TelegramSecurityTests(DatabaseTestCase):
    def test_forged_owner_update_cannot_send_inbox_to_an_arbitrary_chat(self):
        store.save_telnyx_message('+15550000002', '+15550000001', 'private code', message_id='one')
        with mock.patch.object(bridge_app, 'send_telegram') as send, bridge_app.app.test_client() as client:
            result = client.post('/telegram/webhook', json=update(chat_id=999, text='/latest'))
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['unauthorized'])
        send.assert_not_called()

    def test_owner_commands_are_limited_to_owner_alert_and_explicit_chats(self):
        store.save_telnyx_message('+15550000002', '+15550000001', 'private code', message_id='one')
        with mock.patch.dict(os.environ, {'TELEGRAM_COMMAND_CHAT_IDS': '-100123, 888'}), \
             mock.patch.object(bridge_app, 'send_telegram') as send, bridge_app.app.test_client() as client:
            for chat_id in (777, 555, -100123, 888):
                result = client.post('/telegram/webhook', json=update(chat_id=chat_id))
                self.assertNotIn('unauthorized', result.json)
                self.assertIn('private code', send.call_args.args[1])
            result = client.post('/telegram/webhook', json=update(chat_id=555, user_id=123))
            self.assertTrue(result.json['unauthorized'])
            self.assertNotIn('private code', send.call_args.args[1])
            send.reset_mock()
            result = client.post('/telegram/webhook', json=update(chat_id=999))
            self.assertTrue(result.json['unauthorized'])
            send.assert_not_called()

    def test_secret_requires_correct_header_and_invalid_config_fails_closed(self):
        with mock.patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET': 'test_secret-123'}), \
             mock.patch.object(bridge_app, 'send_telegram') as send, bridge_app.app.test_client() as client:
            self.assertEqual(client.post('/telegram/webhook', json=update()).status_code, 403)
            self.assertEqual(client.post('/telegram/webhook', json=update(), headers={
                'X-Telegram-Bot-Api-Secret-Token': 'wrong',
            }).status_code, 403)
            send.assert_not_called()
            accepted = client.post('/telegram/webhook', json=update(), headers={
                'X-Telegram-Bot-Api-Secret-Token': 'test_secret-123',
            })
            self.assertEqual(accepted.status_code, 200)
            send.assert_called_once()
        for bad_secret in ('invalid secret', '   '):
            with mock.patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET': bad_secret}), bridge_app.app.test_client() as client:
                self.assertEqual(client.post('/telegram/webhook', json=update(), headers={
                    'X-Telegram-Bot-Api-Secret-Token': bad_secret,
                }).status_code, 403)

    def test_required_telegram_secret_fails_closed_when_missing(self):
        for setting in ('true', '1', 'unexpected-typo'):
            with mock.patch.dict(os.environ, {'TELEGRAM_REQUIRE_SECRET': setting}), \
                 mock.patch.object(bridge_app, 'send_telegram') as send, bridge_app.app.test_client() as client:
                self.assertEqual(client.post('/telegram/webhook', json=update()).status_code, 403)
                send.assert_not_called()

    def test_invalid_owner_and_malformed_updates_are_ignored_without_500(self):
        bad_updates = [
            [], None, 'text', 17, {'message': []}, {'message': 'text'},
            {'message': {'chat': 'bad', 'from': {}, 'text': '/latest'}},
            {'message': {'chat': {'id': True}, 'from': {'id': 777}, 'text': '/latest'}},
            {'message': {'chat': {'id': 555}, 'from': {'id': 777}, 'text': []}},
        ]
        with mock.patch.dict(os.environ, {'OWNER_TELEGRAM_ID': 'not-an-integer'}), \
             mock.patch.object(bridge_app, 'send_telegram') as send, bridge_app.app.test_client() as client:
            for payload in bad_updates:
                result = client.post('/telegram/webhook', json=payload)
                self.assertEqual(result.status_code, 200)
                self.assertTrue(result.json['ignored'])
            result = client.post('/telegram/webhook', json=update())
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json['unauthorized'])
            self.assertNotIn('private code', str(send.call_args_list))

    def test_request_size_is_bounded(self):
        with bridge_app.app.test_client() as client:
            result = client.post('/telegram/webhook', data=b'x' * (1024 * 1024 + 1), content_type='application/json')
            self.assertEqual(result.status_code, 413)
            result = client.post('/inbound/sms', data=b'x' * (1024 * 1024 + 1), content_type='application/json')
            self.assertEqual(result.status_code, 413)

    def test_status_is_sanitized_and_distinguishes_legacy_security(self):
        store.save_telnyx_message('+15550000002', '+15550000001', 'private code', message_id='one')
        with mock.patch.dict(os.environ, {'TELNYX_API_KEY': 'telnyx-private-key', 'TELEGRAM_BOT_TOKEN': 'bot-private-token'}), \
             mock.patch.object(bridge_app, 'send_telegram') as send, bridge_app.app.test_client() as client:
            result = client.post('/telegram/webhook', json=update(text='/status'))
            self.assertEqual(result.status_code, 200)
            text = send.call_args.args[1]
            self.assertIn('configuration only', text)
            self.assertIn('Telnyx accounts configured: 1', text)
            self.assertIn('disabled (legacy mode)', text)
            self.assertIn('Pending alerts: 1', text)
            for sensitive in ('telnyx-private-key', 'bot-private-token', 'private code', '+15550000002'):
                self.assertNotIn(sensitive, text)

    def test_available_command_sanitizes_provider_errors(self):
        error = requests.HTTPError('private-token and phone number', response=SimpleNamespace(status_code=403))
        fake = mock.Mock()
        fake.search_available_numbers.side_effect = error
        with mock.patch.dict(os.environ, {'TELNYX_API_KEY': 'private-token'}), \
             mock.patch.object(bridge_app, 'TelnyxClient', return_value=fake), \
             mock.patch.object(bridge_app, 'send_telegram') as send, bridge_app.app.test_client() as client:
            result = client.post('/telegram/webhook', json=update(text='/available'))
            self.assertEqual(result.status_code, 200)
            self.assertIn('HTTP 403', send.call_args.args[1])
            self.assertNotIn('private-token', send.call_args.args[1])


class TelnyxSecurityTests(DatabaseTestCase):
    def test_explicit_signature_requirement_fails_closed_without_a_key(self):
        self.assertTrue(telnyx_webhook.verify_telnyx_signature(b'{}', {}))
        for setting in ('true', '1', 'unexpected-typo'):
            with mock.patch.dict(os.environ, {'TELNYX_REQUIRE_SIGNATURE': setting}), bridge_app.app.test_client() as client:
                self.assertFalse(telnyx_webhook.verify_telnyx_signature(b'{}', {}))
                self.assertEqual(client.post('/inbound/sms', json=webhook()).status_code, 403)
        self.assertEqual(store.pending_notification_count(), 0)

    def test_invalid_tolerance_rejects_signed_webhooks_without_breaking_sync_config(self):
        from nacl.signing import SigningKey
        import base64
        import json
        import time

        key = SigningKey.generate()
        timestamp = str(int(time.time()))
        body = json.dumps(webhook()).encode()
        signature = key.sign(timestamp.encode() + b'|' + body).signature
        headers = {
            'Content-Type': 'application/json', 'Telnyx-Timestamp': timestamp,
            'Telnyx-Signature-Ed25519': base64.b64encode(signature).decode(),
        }
        environment = {
            'TELNYX_PUBLIC_KEY': base64.b64encode(bytes(key.verify_key)).decode(),
            'TELNYX_API_KEY': 'dummy', 'TELNYX_SIGNATURE_TOLERANCE': 'bad-value',
        }
        with mock.patch.dict(os.environ, environment), bridge_app.app.test_client() as client:
            self.assertEqual(client.post('/inbound/sms', data=body, headers=headers).status_code, 403)
            self.assertEqual(load_config().telnyx_api_key, 'dummy')
            self.assertEqual(client.post('/telegram/webhook', json=update(text='/status')).status_code, 200)
        self.assertEqual(store.pending_notification_count(), 0)

    def test_webhook_uuid_fallback_deduplicates_retries(self):
        event = webhook()
        payload = event['data']['payload']
        payload['uuid'] = payload.pop('id')
        with bridge_app.app.test_client() as client:
            self.assertFalse(client.post('/inbound/sms', json=event).json['duplicate'])
            self.assertTrue(client.post('/inbound/sms', json=event).json['duplicate'])
        self.assertEqual(store.pending_notification_count(), 1)

    def test_outbound_payload_is_not_treated_as_inbound(self):
        event = webhook()
        event['data']['payload']['direction'] = 'outbound'
        with bridge_app.app.test_client() as client:
            result = client.post('/inbound/sms', json=event)
        self.assertEqual(result.status_code, 400)
        self.assertEqual(store.pending_notification_count(), 0)

    def test_invalid_timestamp_and_base64_never_raise(self):
        with mock.patch.dict(os.environ, {'TELNYX_PUBLIC_KEY': 'not-a-valid-key'}):
            self.assertFalse(telnyx_webhook.verify_telnyx_signature(b'{}', {
                'Telnyx-Timestamp': 'invalid', 'Telnyx-Signature-Ed25519': '%%%'}))
            self.assertFalse(telnyx_webhook.verify_telnyx_signature(b'{}', {
                'Telnyx-Timestamp': str(int(datetime.now(timezone.utc).timestamp())),
                'Telnyx-Signature-Ed25519': '%%%'}))
            for tolerance in ('-1', '0'):
                with mock.patch.dict(os.environ, {'TELNYX_SIGNATURE_TOLERANCE': tolerance}):
                    self.assertFalse(telnyx_webhook.verify_telnyx_signature(b'{}', {}))


class SetupUtilitiesTests(DatabaseTestCase):
    def test_registration_preserves_updates_and_registers_secret(self):
        out, err = io.StringIO(), io.StringIO()
        env = {'TELEGRAM_BOT_TOKEN': 'private-token', 'PUBLIC_BASE_URL': 'https://example.com',
               'TELEGRAM_WEBHOOK_SECRET': 'private_secret'}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(set_telegram_webhook.requests, 'post', return_value=response({'ok': True})) as post, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(set_telegram_webhook.main([]), 0)
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['url'], 'https://example.com/telegram/webhook')
        self.assertFalse(payload['drop_pending_updates'])
        self.assertEqual(payload['secret_token'], 'private_secret')
        self.assertNotIn('private-token', out.getvalue() + err.getvalue())
        self.assertNotIn('private_secret', out.getvalue() + err.getvalue())

    def test_explicit_discard_flag_and_missing_secret_warning(self):
        out, err = io.StringIO(), io.StringIO()
        env = {'TELEGRAM_BOT_TOKEN': 'dummy', 'PUBLIC_BASE_URL': 'https://example.com'}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(set_telegram_webhook.requests, 'post', return_value=response({'ok': True})) as post, \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(set_telegram_webhook.main(['--drop-pending-updates']), 0)
        self.assertTrue(post.call_args.kwargs['json']['drop_pending_updates'])
        self.assertNotIn('secret_token', post.call_args.kwargs['json'])
        self.assertIn('authentication is disabled', err.getvalue())

    def test_registration_does_not_remove_a_required_secret(self):
        env = {'TELEGRAM_BOT_TOKEN': 'dummy', 'PUBLIC_BASE_URL': 'https://example.com',
               'TELEGRAM_REQUIRE_SECRET': 'true'}
        with mock.patch.dict(os.environ, env), mock.patch.object(set_telegram_webhook.requests, 'post') as post, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(set_telegram_webhook.main([]), 1)
            post.assert_not_called()

    def test_invalid_registration_inputs_and_network_errors_are_safe(self):
        env = {'TELEGRAM_BOT_TOKEN': 'private-token', 'PUBLIC_BASE_URL': 'http://example.com'}
        with mock.patch.dict(os.environ, env), mock.patch.object(set_telegram_webhook.requests, 'post') as post, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(set_telegram_webhook.main([]), 1)
            post.assert_not_called()
        with mock.patch.dict(os.environ, dict(env, PUBLIC_BASE_URL='https://example.com', TELEGRAM_WEBHOOK_SECRET='bad secret')), \
             mock.patch.object(set_telegram_webhook.requests, 'post') as post, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(set_telegram_webhook.main([]), 1)
            post.assert_not_called()
        err = io.StringIO()
        with mock.patch.dict(os.environ, dict(env, PUBLIC_BASE_URL='https://example.com')), \
             mock.patch.object(set_telegram_webhook.requests, 'post', side_effect=requests.ConnectionError('private-token in URL')), \
             contextlib.redirect_stderr(err):
            self.assertEqual(set_telegram_webhook.main([]), 1)
        self.assertNotIn('private-token', err.getvalue())
        self.assertIn('ConnectionError', err.getvalue())

    def test_chat_id_helper_reports_webhook_conflict_without_leaking_tokens(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'private-token'}), \
             mock.patch.object(telegram_chat_id.requests, 'get', return_value=response({}, ok=False, status=409)), \
             contextlib.redirect_stderr(err):
            self.assertEqual(telegram_chat_id.main(), 1)
        self.assertIn('/whoami', err.getvalue())
        self.assertIn('webhook', err.getvalue())
        self.assertNotIn('private-token', err.getvalue())
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'private-token'}), \
             mock.patch.object(telegram_chat_id.requests, 'get', side_effect=requests.ConnectionError('private-token')), \
             contextlib.redirect_stderr(err):
            self.assertEqual(telegram_chat_id.main(), 1)
        self.assertNotIn('private-token', err.getvalue())


class SyncUpgradeTests(DatabaseTestCase):
    def test_paginated_sync_fetches_only_the_requested_number(self):
        client = TelnyxClient('dummy')
        pages = [
            response({'data': [{'uuid': f'm-{i}'} for i in range(0, 100)], 'meta': {'total_pages': 3}}),
            response({'data': [{'uuid': f'm-{i}'} for i in range(100, 200)], 'meta': {'total_pages': 3}}),
            response({'data': [{'uuid': f'm-{i}'} for i in range(200, 300)], 'meta': {'total_pages': 3}}),
        ]
        client.session.get = mock.Mock(side_effect=pages)
        records = client.list_messaging_detail_records(limit=250, direction='inbound')
        self.assertEqual(len(records), 250)
        self.assertEqual(records[-1]['uuid'], 'm-249')
        self.assertEqual([c.kwargs['params']['page[number]'] for c in client.session.get.call_args_list], [1, 2, 3])
        self.assertTrue(all(c.kwargs['params']['page[size]'] == 100 for c in client.session.get.call_args_list))

    def test_provider_clamped_page_size_is_followed_via_metadata(self):
        client = TelnyxClient('dummy')
        client.session.get = mock.Mock(side_effect=[
            response({'data': [{'uuid': f'a-{i}'} for i in range(25)], 'meta': {'total_pages': 3}}),
            response({'data': [{'uuid': f'b-{i}'} for i in range(25)], 'meta': {'total_pages': 3}}),
            response({'data': [{'uuid': f'c-{i}'} for i in range(10)], 'meta': {'total_pages': 3}}),
        ])
        self.assertEqual(len(client.list_messaging_detail_records(limit=60)), 60)
        self.assertEqual(client.session.get.call_count, 3)

    def test_partial_pages_are_processed_and_the_error_is_reported(self):
        client = TelnyxClient('dummy')
        client.session.get = mock.Mock(side_effect=[
            response({'data': [record()], 'meta': {'total_pages': 2}}), requests.Timeout('secret in url'),
        ])
        with self.assertRaises(PartialDetailRecordsError) as caught:
            client.list_messaging_detail_records(limit=200)
        self.assertEqual(len(caught.exception.records), 1)
        self.assertIsInstance(caught.exception.cause, requests.Timeout)

        class Partial(FakeTelnyx):
            def list_messaging_detail_records(self, **kwargs):
                raise PartialDetailRecordsError([record()], requests.Timeout('secret in url'))

        fake = Partial([], {'message-123': message()})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]):
            result = telnyx_sync.sync_inbound_once(limit=200)
        self.assertEqual((result.returned, result.stored), (1, 1))
        self.assertIn('additional pages unavailable (request timed out)', result.errors[0])
        self.assertNotIn('secret', str(result.errors))

    def test_pagination_has_a_safety_cap(self):
        client = TelnyxClient('dummy')
        client.session.get = mock.Mock(return_value=response({'data': [record()], 'meta': {'total_pages': 999}}))
        with self.assertRaises(PartialDetailRecordsError) as caught:
            client.list_messaging_detail_records(limit=50)
        self.assertIsInstance(caught.exception.cause, PaginationLimitError)
        self.assertEqual(client.session.get.call_count, 10)

    def test_uuid_is_preferred_when_a_detail_record_has_both_ids(self):
        entry = dict(record('message-123'), id='other-record-id')
        client = FakeTelnyx([entry], {'message-123': message('body')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual(result.stored, 1)
        self.assertEqual(client.lookups, ['message-123'])

    def test_complete_detail_record_avoids_extra_lookup(self):
        complete = dict(record(), text='direct body')
        client = FakeTelnyx([complete], {})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once(limit=1)
        self.assertEqual(result.stored, 1)
        self.assertEqual(client.lookups, [])
        self.assertEqual(result.limited_accounts, 1)
        self.assertIn('Older records may be omitted', bridge_app._syncsms_summary(result))

    def test_account_outage_still_processes_records_with_complete_bodies(self):
        complete = dict(record('complete'), text='ready from detail record')
        incomplete = record('incomplete')
        client = FakeTelnyx([incomplete, complete], {'incomplete': requests.Timeout('private-token')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual((result.checked, result.stored, result.skipped), (2, 1, 1))
        self.assertEqual(client.lookups, ['incomplete'])
        self.assertIn('request timed out', result.errors[0])
        self.assertEqual(store.recent_all()[0].body, 'ready from detail record')

    def test_recovery_processes_newest_records_first(self):
        client = FakeTelnyx([record('newest'), record('older', age=1)], {
            'newest': message('newest'), 'older': message('older'),
        })
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', client)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual(result.stored, 2)
        self.assertEqual(client.lookups, ['newest', 'older'])
        self.assertEqual(store.recent_all(limit=1)[0].body, 'newest')
        self.assertEqual(store.recent_for_number('+15550000002', limit=1)[0].body, 'newest')

    def test_multiple_accounts_really_run_in_parallel(self):
        barrier = threading.Barrier(2, timeout=3)

        class Coordinated(FakeTelnyx):
            def list_messaging_detail_records(self, **kwargs):
                barrier.wait()
                return super().list_messaging_detail_records(**kwargs)

        first = Coordinated([record('first')], {'first': message('first')})
        second = Coordinated([record('second')], {'second': message('second')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', first), ('acct2', second)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual((result.accounts, result.stored, result.errors), (2, 2, []))
        self.assertEqual(store.pending_notification_count(), 2)

    def test_overlapping_manual_and_background_syncs_are_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        calls = []

        def run(limit, notify_new):
            calls.append(limit)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            else:
                second_entered.set()
            return telnyx_sync.SyncResult()

        with mock.patch.object(telnyx_sync, '_sync_inbound_once_unlocked', side_effect=run):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(telnyx_sync.sync_inbound_once, 1)
                self.assertTrue(entered.wait(3))
                second = executor.submit(telnyx_sync.sync_inbound_once, 2)
                self.assertFalse(second_entered.wait(0.1))
                release.set()
                first.result(timeout=3)
                second.result(timeout=3)
        self.assertEqual(calls, [1, 2])


class RetryUpgradeTests(DatabaseTestCase):
    def test_bad_message_does_not_block_newer_alerts_and_retry_is_persisted(self):
        for name in ('one', 'two'):
            store.save_telnyx_message('+15550000002', '+15550000001', name, message_id=name)
        with mock.patch.object(notify, 'notify_owner', side_effect=[False, True]) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(store.pending_notification_count(), 1)
            self.assertEqual(alerts.deliver_pending(), 0)
            self.assertEqual(send.call_count, 2)
        with sqlite3.connect('inbound.db') as conn:
            state, retry_at, attempts = conn.execute(
                "SELECT notification_state, notification_retry_at, notification_attempts FROM inbound WHERE provider_message_id = 'telnyx:one'"
            ).fetchone()
            self.assertEqual((state, attempts), ('pending', 1))
            self.assertGreater(datetime.fromisoformat(retry_at), datetime.now(timezone.utc))
            conn.execute("UPDATE inbound SET notification_retry_at = '2000-01-01T00:00:00+00:00'")
        with mock.patch.object(notify, 'notify_owner', return_value=True):
            self.assertEqual(alerts.deliver_pending(), 1)
        self.assertEqual(store.pending_notification_count(), 0)

    def test_provider_outage_has_a_per_batch_failure_cap(self):
        for i in range(4):
            store.save_telnyx_message('+15550000002', '+15550000001', str(i), message_id=str(i))
        with mock.patch.object(notify, 'notify_owner', return_value=False) as send:
            self.assertEqual(alerts.deliver_pending(), 0)
            self.assertEqual(send.call_count, alerts.MAX_FAILURES_PER_BATCH)
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(store.pending_notification_count(), 3)

    def test_new_body_resets_a_failed_empty_alerts_retry_delay(self):
        store.save_telnyx_message('+15550000002', '+15550000001', '', message_id='one')
        with mock.patch.object(notify, 'notify_owner', return_value=False):
            self.assertEqual(alerts.deliver_pending(), 0)
        _, updated = store.save_telnyx_message('+15550000002', '+15550000001', 'recovered code', message_id='one')
        self.assertTrue(updated)
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 1)
        self.assertIn('recovered code', send.call_args.args[0])

    def test_upgrade_migrates_existing_sent_and_pending_rows_safely(self):
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect('previous.db') as conn:
            conn.execute('CREATE TABLE inbound (id INTEGER PRIMARY KEY, to_number TEXT NOT NULL, '
                         'from_number TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, '
                         'provider_message_id TEXT, notification_state TEXT NOT NULL DEFAULT "sent", '
                         'notification_claim_token TEXT, notification_claimed_at TEXT)')
            conn.executemany('INSERT INTO inbound (to_number,from_number,body,received_at,provider_message_id,notification_state) '
                             'VALUES (?,?,?,?,?,?)', [
                ('to', 'from', 'old sent', now, 'telnyx:old', 'sent'),
                ('to', 'from', 'pending', now, 'telnyx:pending', 'pending'),
            ])
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: store.init_db('previous.db'), range(8)))
        with sqlite3.connect('previous.db') as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(inbound)')}
            rows = conn.execute('SELECT notification_state,notification_retry_at,notification_attempts FROM inbound ORDER BY id').fetchall()
        self.assertIn('notification_retry_at', columns)
        self.assertIn('notification_attempts', columns)
        self.assertEqual(rows, [('sent', None, 0), ('pending', None, 0)])
        claim = store.claim_next_notification('previous.db')
        self.assertEqual(claim.message.body, 'pending')
        self.assertTrue(store.finish_notification(claim, delivered=True, path='previous.db'))
        self.assertEqual(store.pending_notification_count('previous.db'), 0)

    def test_delivery_prunes_expired_alerts_even_without_polling(self):
        old = datetime.now(timezone.utc) - timedelta(hours=49)
        store.save_telnyx_message('+15550000002', '+15550000001', 'expired', message_id='old', received_at=old)
        with mock.patch.object(notify, 'notify_owner', return_value=True) as send:
            self.assertEqual(alerts.deliver_pending(), 0)
            send.assert_not_called()
        self.assertEqual(store.pending_notification_count(), 0)
        self.assertEqual(store.recent_all(), [])

    def test_retry_schedule_survives_a_new_process(self):
        store.save_telnyx_message('+15550000002', '+15550000001', 'hello', message_id='one')
        claim = store.claim_next_notification()
        self.assertTrue(store.finish_notification(claim, delivered=False, retry_seconds=30))
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(repo), INBOUND_DB_PATH=str(Path('inbound.db').resolve()))
        script = "import store; assert store.pending_notification_count() == 1; assert store.claim_next_notification() is None"
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        with sqlite3.connect('inbound.db') as conn:
            conn.execute("UPDATE inbound SET notification_retry_at = '2000-01-01T00:00:00+00:00'")
        script = "import store; c = store.claim_next_notification(); assert c; assert store.finish_notification(c, delivered=True)"
        result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(store.pending_notification_count(), 0)


if __name__ == '__main__':
    unittest.main()
