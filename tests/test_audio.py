from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import struct
import subprocess
import time
import unittest
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import urlencode
from xml.etree import ElementTree

import imageio_ffmpeg
import requests
from nacl.signing import SigningKey

import alerts
import audio_delivery
import audio_store
import notify
import store
import telnyx_sync
import voice_recordings
import voice_synth
from test_bridge import DatabaseTestCase, FakeTelnyx, bridge_app, message, record, webhook


class FakeResponse:
    def __init__(self, payload=None, *, status=200, content=b'', headers=None):
        self.payload = payload
        self.status_code = status
        self.ok = status < 400
        self.content = content
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed = True


def telegram_update(text):
    return {'message': {'chat': {'id': 555}, 'from': {'id': 777}, 'text': text}}


class AudioTestCase(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.key_a = SigningKey.generate()
        self.key_b = SigningKey.generate()
        self.voice_env = mock.patch.dict(os.environ, {
            'SMS_VOICE_ENABLED': 'true',
            'TELNYX_VOICE_ENABLED': 'true',
            'TELNYX_RECORDING_IMPORT_ENABLED': 'true',
            'TELNYX_VOICE_CONNECTIONS': '111=TELNYX_VOICE_ACCOUNT_A,222=TELNYX_VOICE_ACCOUNT_B',
            'TELNYX_VOICE_ACCOUNT_A_API_KEY': 'key-for-account-a',
            'TELNYX_VOICE_ACCOUNT_A_PUBLIC_KEY': base64.b64encode(bytes(self.key_a.verify_key)).decode(),
            'TELNYX_VOICE_ACCOUNT_B_API_KEY': 'key-for-account-b',
            'TELNYX_VOICE_ACCOUNT_B_PUBLIC_KEY': base64.b64encode(bytes(self.key_b.verify_key)).decode(),
            'TELNYX_VOICE_NUMBERS': '+15550000002,+15550000003',
            'VOICE_PUBLIC_BASE_URL': 'https://bridge.example.com',
            'TELEGRAM_BOT_TOKEN': 'fake-telegram-token',
        })
        self.voice_env.start()
        audio_store.configure_features(sms=True, calls=True, imports=True)

    def tearDown(self):
        self.voice_env.stop()
        super().tearDown()

    def signed_form(self, client, path, values, key=None, timestamp=None):
        raw = urlencode(values, doseq=True).encode()
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        signature = (key or self.key_a).sign(timestamp.encode() + b'|' + raw).signature
        return client.post(path, data=raw, headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Telnyx-Timestamp': timestamp,
            'Telnyx-Signature-Ed25519': base64.b64encode(signature).decode(),
        })

    def call_form(self, *, connection='111', call_sid='v3:fixture-call', to='+15550000002'):
        return {'ConnectionId': connection, 'CallSid': call_sid, 'To': to,
                'From': '+15550000001', 'CallStatus': 'in-progress'}

    def jobs(self):
        with sqlite3.connect('inbound.db') as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute('SELECT * FROM audio_jobs ORDER BY id').fetchall()

    def queue_sms(self, body='Your code is 123456', message_id='new-sms', **kwargs):
        return store.save_telnyx_message(
            '+15550000002', '+15550000001', body, message_id=message_id,
            authenticated_for_audio=True, **kwargs,
        )


class SMSVoiceTests(AudioTestCase):
    def test_local_speech_is_real_opus_and_text_alert_is_unchanged(self):
        self.queue_sms()
        self.assertEqual(store.pending_notification_count(), 1)
        with mock.patch.object(notify, 'send_voice_owner', return_value='sent') as send:
            self.assertEqual(audio_delivery.deliver_pending(), 1)
        voice, audio_format, caption = send.call_args.args
        self.assertEqual(audio_format, 'ogg')
        self.assertTrue(voice.startswith(b'OggS'))
        self.assertIn('SMS read aloud', caption)
        self.assertNotIn('123456', caption)
        decoded = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error',
             '-i', 'pipe:0', '-f', 'null', '-'],
            input=voice, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        self.assertEqual(decoded.returncode, 0)
        self.assertEqual(voice_synth.speech_text('Your code is 012345.'), 'Your code is 0 1 2 3 4 5.')
        self.assertEqual(self.jobs()[0]['state'], 'sent')
        self.assertIsNone(self.jobs()[0]['audio'])
        with mock.patch.object(notify, 'notify_owner', return_value=True) as text_alert:
            self.assertEqual(alerts.deliver_pending(), 1)
        self.assertIn('123456', text_alert.call_args.args[0])

    def test_no_replay_of_pre_activation_messages_and_missing_text_can_recover(self):
        audio_store.configure_features(sms=False, calls=True, imports=True)
        self.queue_sms('old complete', message_id='old-complete')
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        audio_store.configure_features(sms=True, calls=True, imports=True)
        self.queue_sms('old complete', message_id='old-complete')
        self.queue_sms('old fetched later', message_id='old-poll', received_at=old_time)
        self.queue_sms(store.MISSING_BODY, message_id='missing')
        self.assertEqual(self.jobs(), [])
        self.queue_sms('Recovered code 654321', message_id='missing')
        self.queue_sms('fresh', message_id='fresh')
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(store.pending_notification_count(), 4)

    def test_enable_time_persists_through_restart_and_resets_when_disabled(self):
        with sqlite3.connect('inbound.db') as conn:
            first = conn.execute("SELECT activated_at FROM audio_feature_state WHERE feature = 'sms'").fetchone()[0]
        audio_store.configure_features(sms=True, calls=True, imports=True)
        with sqlite3.connect('inbound.db') as conn:
            same = conn.execute("SELECT activated_at FROM audio_feature_state WHERE feature = 'sms'").fetchone()[0]
        self.assertEqual(first, same)
        self.queue_sms()
        claim = audio_store.claim_next(('sms',))
        self.assertTrue(audio_store.save_audio(claim, b'OggS-sample', 'ogg'))
        audio_store.configure_features(sms=False, calls=True, imports=True)
        self.assertEqual(self.jobs()[0]['state'], 'cancelled')
        self.assertIsNone(self.jobs()[0]['audio'])
        audio_store.configure_features(sms=True, calls=True, imports=True)
        self.queue_sms(message_id='new-sms')
        self.assertEqual(len(self.jobs()), 1)
        self.queue_sms(message_id='after-reenable')
        self.assertEqual(len(self.jobs()), 2)

    def test_unsigned_sms_webhook_still_gets_text_alert_but_no_audio(self):
        with bridge_app.app.test_client() as client:
            response = client.post('/inbound/sms', json=webhook('untrusted'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.pending_notification_count(), 1)
        self.assertEqual(self.jobs(), [])

    def test_poll_confirms_unsigned_first_sms_without_duplicate_text_or_audio(self):
        with bridge_app.app.test_client() as client:
            self.assertEqual(client.post('/inbound/sms', json=webhook('unsigned-first')).status_code, 200)
        self.assertEqual(self.jobs(), [])
        fake = FakeTelnyx([record('unsigned-first')], {'unsigned-first': message()})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]):
            first = telnyx_sync.sync_inbound_once()
            second = telnyx_sync.sync_inbound_once()
        self.assertEqual((first.already_stored, first.stored, second.stored), (1, 0, 0))
        self.assertEqual(fake.lookups, ['unsigned-first'])
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(store.pending_notification_count(), 1)

    def test_poll_does_not_speak_untrusted_or_pre_activation_content(self):
        forged = webhook('forged')
        forged['data']['payload']['text'] = 'fake untrusted text'
        with bridge_app.app.test_client() as client:
            self.assertEqual(client.post('/inbound/sms', json=forged).status_code, 200)
            self.assertEqual(client.post('/inbound/sms', json=webhook('old')).status_code, 200)
        fake = FakeTelnyx([record('forged'), record('old', age=1)],
                          {'forged': message('authenticated text'), 'old': message()})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual(self.jobs(), [])
        self.assertEqual(fake.lookups, ['forged'])
        self.assertIn('audio skipped', ' '.join(result.errors))
        self.assertNotIn('fake untrusted text', repr(result))
        self.assertNotIn('authenticated text', repr(result))

    def test_signed_webhook_and_poll_deduplicate_audio(self):
        payload = webhook('signed-sms')
        raw = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        signature = self.key_a.sign(timestamp.encode() + b'|' + raw).signature
        headers = {'Content-Type': 'application/json', 'Telnyx-Timestamp': timestamp,
                   'Telnyx-Signature-Ed25519': base64.b64encode(signature).decode()}
        with mock.patch.dict(os.environ, {'TELNYX_PUBLIC_KEY': base64.b64encode(bytes(self.key_a.verify_key)).decode()}), \
             bridge_app.app.test_client() as client:
            self.assertEqual(client.post('/inbound/sms', data=raw, headers=headers).status_code, 200)
            self.assertTrue(client.post('/inbound/sms', data=raw, headers=headers).json['duplicate'])
        fake = FakeTelnyx([record('signed-sms')], {'signed-sms': message()})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]):
            telnyx_sync.sync_inbound_once()
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(store.pending_notification_count(), 1)

    def test_polling_can_create_audio_for_a_new_authenticated_message(self):
        fake = FakeTelnyx([record('polled')], {'polled': message('Use 123456')})
        with mock.patch.object(telnyx_sync, 'telnyx_clients', return_value=[('acct1', fake)]):
            result = telnyx_sync.sync_inbound_once()
        self.assertEqual(result.stored, 1)
        self.assertEqual(len(self.jobs()), 1)

    def test_oversized_message_is_not_truncated_or_sent_as_audio(self):
        self.queue_sms('A' * (voice_synth.MAX_TEXT_CHARS + 1))
        with mock.patch.object(notify, 'send_voice_owner') as send:
            self.assertEqual(audio_delivery.deliver_pending(), 0)
        send.assert_not_called()
        row = self.jobs()[0]
        self.assertEqual((row['state'], row['last_error']), ('failed', 'sms_too_long'))
        self.assertEqual(store.pending_notification_count(), 1)
        status = bridge_app._status_text()
        self.assertIn('failed: 1', status)
        self.assertIn('SMS too long 1', status)
        self.assertNotIn('A' * 50, status)

    def test_explicit_rejection_retries_without_resynthesizing(self):
        self.queue_sms()
        with mock.patch.object(audio_delivery, '_speech_audio', return_value=b'OggS-fixture') as synth, \
             mock.patch.object(notify, 'send_voice_owner', side_effect=['retry', 'sent']) as send:
            self.assertEqual(audio_delivery.deliver_pending(), 0)
            self.assertEqual(self.jobs()[0]['state'], 'pending')
            with sqlite3.connect('inbound.db') as conn:
                conn.execute('UPDATE audio_jobs SET retry_at = NULL')
            self.assertEqual(audio_delivery.deliver_pending(), 1)
        self.assertEqual(synth.call_count, 1)
        self.assertEqual(send.call_count, 2)
        self.assertIsNone(self.jobs()[0]['audio'])

    def test_unexpected_failures_retry_only_before_upload(self):
        self.queue_sms(message_id='pre-upload')
        with mock.patch.object(audio_delivery, '_speech_audio', side_effect=RuntimeError('secret-body')), \
             mock.patch.object(notify, 'send_voice_owner') as send, \
             self.assertLogs('audio_delivery', level='ERROR') as logs:
            self.assertEqual(audio_delivery.deliver_pending(), 0)
        send.assert_not_called()
        self.assertEqual(self.jobs()[0]['state'], 'pending')
        self.assertEqual(self.jobs()[0]['last_error'], 'audio_processing_error')
        self.assertNotIn('secret-body', repr(logs.output))

        self.queue_sms(message_id='during-upload')
        with mock.patch.object(audio_delivery, '_speech_audio', return_value=b'OggS-fixture'), \
             mock.patch.object(notify, 'send_voice_owner', side_effect=RuntimeError('secret-token')), \
             self.assertLogs('audio_delivery', level='ERROR') as logs:
            self.assertEqual(audio_delivery.deliver_pending(), 0)
        self.assertEqual(self.jobs()[1]['state'], 'uncertain')
        self.assertNotIn('secret-token', repr(logs.output))

    def test_uncertain_delivery_requires_explicit_authenticated_retry(self):
        self.queue_sms()
        with mock.patch.object(audio_delivery, '_speech_audio', return_value=b'OggS-fixture'), \
             mock.patch.object(notify, 'send_voice_owner', side_effect=['uncertain', 'sent']) as send:
            self.assertEqual(audio_delivery.deliver_pending(), 0)
            self.assertEqual(self.jobs()[0]['state'], 'uncertain')
            self.assertEqual(audio_delivery.deliver_pending(), 0)
            self.assertEqual(send.call_count, 1)
            with mock.patch.object(bridge_app, 'send_telegram') as reply, bridge_app.app.test_client() as client:
                client.post('/telegram/webhook', json=telegram_update('/retryaudio confirm'))
                self.assertIn('require a registered', reply.call_args.args[1])
                self.assertEqual(self.jobs()[0]['state'], 'uncertain')
                with mock.patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET': 'test-secret'}):
                    client.post('/telegram/webhook', json=telegram_update('/retryaudio'),
                                headers={'X-Telegram-Bot-Api-Secret-Token': 'test-secret'})
                    self.assertIn('risk duplicates', reply.call_args.args[1])
                    client.post('/telegram/webhook', json=telegram_update('/retryaudio confirm'),
                                headers={'X-Telegram-Bot-Api-Secret-Token': 'test-secret'})
            self.assertEqual(self.jobs()[0]['state'], 'pending')
            self.assertEqual(audio_delivery.deliver_pending(), 1)

    def test_lost_before_sending_can_be_reclaimed_but_lost_during_send_is_uncertain(self):
        self.queue_sms()
        first = audio_store.claim_next(('sms',))
        self.assertTrue(audio_store.save_audio(first, b'OggS-fixture', 'ogg'))
        old = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        with sqlite3.connect('inbound.db') as conn:
            conn.execute('UPDATE audio_jobs SET claimed_at = ?', (old,))
        second = audio_store.claim_next(('sms',))
        self.assertIsNotNone(second)
        self.assertEqual(second.audio, b'OggS-fixture')
        self.assertFalse(audio_store.begin_sending(first))
        self.assertTrue(audio_store.begin_sending(second))
        with sqlite3.connect('inbound.db') as conn:
            conn.execute('UPDATE audio_jobs SET claimed_at = ?', (old,))
        self.assertIsNone(audio_store.claim_next(('sms',)))
        self.assertEqual(self.jobs()[0]['state'], 'uncertain')
        self.assertFalse(audio_store.finish(second, 'sent'))

    def test_parallel_audio_claims_are_unique(self):
        self.queue_sms()
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: audio_store.claim_next(('sms',)), range(8)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_audio_retention_prunes_old_jobs_without_deleting_sms_even_if_disabled(self):
        self.queue_sms()
        audio_store.register_call('111', 'fixture-call', '+15550000002', '+15550000001')
        with sqlite3.connect('inbound.db') as conn:
            conn.execute('UPDATE audio_jobs SET created_at = ?',
                         ((datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(),))
            conn.execute('UPDATE audio_calls SET created_at = ?',
                         ((datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(),))
        audio_store.configure_features(sms=False, calls=False, imports=False)
        store.prune_old_messages(hours=48)
        self.assertEqual(self.jobs(), [])
        self.assertIsNone(audio_store.find_call('111', 'fixture-call'))
        self.assertEqual(store.pending_notification_count(), 1)


class VoiceWebhookTests(AudioTestCase):
    def test_default_disabled_and_no_key_both_fail_closed(self):
        with bridge_app.app.test_client() as client:
            with mock.patch.dict(os.environ, {'TELNYX_VOICE_ENABLED': 'false'}):
                self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form()).status_code, 404)
            with mock.patch.dict(os.environ, {'TELNYX_VOICE_ACCOUNT_A_PUBLIC_KEY': ''}):
                self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form()).status_code, 403)
            self.assertEqual(client.post('/inbound/voice', data=self.call_form()).status_code, 403)
        self.assertEqual(self.jobs(), [])

    def test_signed_call_response_has_bounded_recording_and_hangup(self):
        with bridge_app.app.test_client() as client:
            response = self.signed_form(client, '/inbound/voice', self.call_form())
            self.assertEqual(response.status_code, 200)
            root = ElementTree.fromstring(response.data)
            rec = root.find('Record')
            self.assertEqual(rec.attrib['maxLength'], '120')
            self.assertEqual(rec.attrib['playBeep'], 'true')
            self.assertEqual(rec.attrib['format'], 'mp3')
            self.assertEqual(rec.attrib['action'], 'https://bridge.example.com/inbound/voice/end')
            self.assertEqual(rec.attrib['recordingStatusCallback'], 'https://bridge.example.com/inbound/voice/recording')
            self.assertIsNotNone(root.find('Hangup'))
            end = self.signed_form(client, '/inbound/voice/end', self.call_form())
            self.assertEqual(ElementTree.fromstring(end.data).find('Hangup').tag, 'Hangup')
            with mock.patch.dict(os.environ, {'TELNYX_VOICE_ENABLED': 'false'}):
                unsigned_end = client.post('/inbound/voice/end')
            self.assertEqual(unsigned_end.status_code, 200)
            self.assertEqual([child.tag for child in ElementTree.fromstring(unsigned_end.data)], ['Hangup'])
            self.assertEqual(self.jobs(), [])
        with sqlite3.connect('inbound.db') as conn:
            data = repr(conn.execute('SELECT * FROM audio_calls').fetchall())
        self.assertNotIn('v3:fixture-call', data)

    def test_wrong_account_key_stale_signature_number_and_duplicate_fields_rejected(self):
        with bridge_app.app.test_client() as client:
            self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form(), key=self.key_b).status_code, 403)
            self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form(), timestamp=int(time.time())-1000).status_code, 403)
            self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form(to='+15559999999')).status_code, 403)
            values = self.call_form()
            values['To'] = ['+15550000002', '+15550000003']
            self.assertEqual(self.signed_form(client, '/inbound/voice', values).status_code, 403)
            second = self.call_form(connection='222', call_sid='v3:second-call', to='+15550000003')
            self.assertEqual(self.signed_form(client, '/inbound/voice', second, key=self.key_b).status_code, 200)
            self.assertEqual(self.signed_form(client, '/inbound/voice', second, key=self.key_a).status_code, 403)

    def test_callback_only_accepts_our_known_calls_and_deduplicates(self):
        recording_id = str(uuid.uuid4())
        callback = {'ConnectionId': '111', 'CallSid': 'v3:fixture-call', 'RecordingStatus': 'completed',
                    'RecordingSid': recording_id,
                    'RecordingUrl': 'https://s3.amazonaws.com/telephony-recorder-prod/test.mp3?signature=fake'}
        with bridge_app.app.test_client() as client:
            self.assertEqual(self.signed_form(client, '/inbound/voice/recording', callback).status_code, 403)
            self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form()).status_code, 200)
            first = self.signed_form(client, '/inbound/voice/recording', callback)
            duplicate = self.signed_form(client, '/inbound/voice/recording', callback)
            self.assertFalse(first.json['duplicate'])
            self.assertTrue(duplicate.json['duplicate'])
            self.assertEqual(self.signed_form(client, '/inbound/voice/recording', callback, key=self.key_b).status_code, 403)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(self.jobs()[0]['recording_id'], recording_id)
        self.assertEqual(self.jobs()[0]['to_number'], '+15550000002')
        with mock.patch.object(voice_recordings, '_download', return_value=b'ID3fake-mp3') as download, \
             mock.patch.object(notify, 'send_voice_owner', return_value='sent') as send:
            self.assertEqual(audio_delivery.deliver_pending(), 1)
        self.assertEqual(download.call_args.args[2], 'key-for-account-a')
        self.assertEqual(send.call_args.args[:2], (b'ID3fake-mp3', 'mp3'))
        self.assertIn('Incoming call recording', send.call_args.args[2])
        self.assertIsNone(self.jobs()[0]['recording_url'])
        self.assertIsNone(self.jobs()[0]['audio'])

    def test_call_configuration_fails_closed_on_bad_mapping_or_base(self):
        with bridge_app.app.test_client() as client:
            with mock.patch.dict(os.environ, {'TELNYX_VOICE_CONNECTIONS': '111=TELNYX_VOICE_ACCOUNT_A,bad'}):
                self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form()).status_code, 403)
            for base in ('http://bridge.example.com', 'https://127.0.0.1', 'https://u:p@bridge.example.com',
                         'https://bridge.example.com/path'):
                with mock.patch.dict(os.environ, {'VOICE_PUBLIC_BASE_URL': base}):
                    self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form()).status_code, 503)
            self.assertIn('Call configuration complete: 2/2', bridge_app._status_text())
            with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': ''}):
                self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form()).status_code, 503)
                self.assertIn('Call configuration complete: 0/2', bridge_app._status_text())
            with mock.patch.dict(os.environ, {'TELEGRAM_ALERT_CHAT_ID': '', 'OWNER_TELEGRAM_ID': ''}):
                self.assertEqual(self.signed_form(client, '/inbound/voice', self.call_form()).status_code, 503)
                self.assertIn('Call configuration complete: 0/2', bridge_app._status_text())


class RecordingTests(AudioTestCase):
    def test_wav_recordings_are_converted_to_real_opus(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b''.join(struct.pack('<h', 1000 if i % 2 else -1000) for i in range(8000)))
        raw = buffer.getvalue()
        self.assertEqual(voice_recordings._audio_kind(raw), 'wav')
        result = audio_delivery._wav_to_ogg(raw)
        self.assertTrue(result.startswith(b'OggS'))
        decoded = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0', '-f', 'null', '-'],
            input=result, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        self.assertEqual(decoded.returncode, 0)

    def test_recording_download_checks_streaming_deadline_and_closes_response(self):
        url = 'https://s3.amazonaws.com/telephony-recorder-prod/file.mp3?signature=fixture'
        response = FakeResponse(content=b'ID3fixture')
        with mock.patch.object(voice_recordings.requests, 'get', return_value=response), \
             mock.patch.object(voice_recordings.time, 'monotonic', side_effect=[0, 46]):
            with self.assertRaises(voice_recordings.RecordingUnavailable):
                voice_recordings._download(url, str(uuid.uuid4()), 'never-logged-key')
        self.assertTrue(response.closed)

    def test_download_urls_are_restricted_and_never_follow_redirects(self):
        rid = str(uuid.uuid4())
        good = 'https://s3.amazonaws.com/telephony-recorder-prod/example.mp3?sig=x'
        self.assertEqual(voice_recordings._url_kind(good, rid), 's3')
        for bad in (
            'http://s3.amazonaws.com/telephony-recorder-prod/file',
            'https://s3.amazonaws.com.evil.example/telephony-recorder-prod/file',
            'https://127.0.0.1/private', 'https://s3.amazonaws.com/other-bucket/file',
            'https://s3.amazonaws.com/telephony-recorder-prod/../other/file',
            'https://u:p@s3.amazonaws.com/telephony-recorder-prod/file',
            'https://s3.amazonaws.com:444/telephony-recorder-prod/file',
            'gs://telephony-recorder-prod/file',
        ):
            self.assertEqual(voice_recordings._url_kind(bad, rid), '', bad)
        with mock.patch.object(voice_recordings.requests, 'get', return_value=FakeResponse(status=302)) as get:
            with self.assertRaises(voice_recordings.RecordingUnavailable):
                voice_recordings._download(good, rid, 'sensitive-api-key')
        self.assertEqual(get.call_args.kwargs['headers'], {})
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_expired_signed_url_is_refreshed_with_owning_account_key(self):
        rid = str(uuid.uuid4())
        old_url = 'https://s3.amazonaws.com/telephony-recorder-prod/old.mp3?sig=old'
        new_url = 'https://s3.amazonaws.com/telephony-recorder-prod/new.mp3?sig=new'
        audio_store.enqueue_recording(kind='import', connection_id='111', recording_id=rid,
                                      to_number='+15550000002', from_number='+15550000001', url=old_url)
        claim = audio_store.claim_next(('import',))
        responses = [FakeResponse(status=403), FakeResponse({'data': {
            'id': rid, 'connection_id': '111', 'to': '+15550000002', 'download_urls': {'mp3': new_url},
        }}), FakeResponse(content=b'ID3actual-recording')]
        with mock.patch.object(voice_recordings.requests, 'get', side_effect=responses) as get:
            data, kind = voice_recordings.obtain_recording(claim)
        self.assertEqual((data, kind), (b'ID3actual-recording', 'mp3'))
        self.assertEqual(get.call_args_list[0].kwargs['headers'], {})
        self.assertEqual(get.call_args_list[1].kwargs['headers']['Authorization'], 'Bearer key-for-account-a')
        self.assertEqual(get.call_args_list[2].kwargs['headers'], {})
        self.assertTrue(all(not call.kwargs['allow_redirects'] for call in get.call_args_list))

    def test_import_is_explicit_bounded_multi_account_and_deduplicated(self):
        ids = [str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())]
        fixtures = {
            '111': [{'id': ids[0], 'connection_id': '111', 'to': '+15550000002', 'from': '+15550000001',
                     'status': 'completed', 'download_urls': {}},
                    {'id': ids[2], 'connection_id': '111', 'to': '+15559999999', 'from': '+15550000001'}],
            '222': [{'id': ids[1], 'connection_id': '222', 'to': '+15550000003', 'from': '+15550000004',
                     'status': 'completed', 'download_urls': {}}],
        }

        def fake_get(url, **kwargs):
            conn = kwargs['params']['filter[connection_id]']
            self.assertEqual(kwargs['headers']['Authorization'], 'Bearer key-for-account-' + ('a' if conn == '111' else 'b'))
            return FakeResponse({'data': fixtures[conn]})

        self.assertEqual(self.jobs(), [])
        with mock.patch.object(voice_recordings.requests, 'get', side_effect=fake_get):
            first = voice_recordings.import_recordings(limit=2)
            second = voice_recordings.import_recordings(limit=2)
        self.assertEqual((first.queued, first.checked, first.skipped, first.errors), (2, 3, 1, 0))
        self.assertEqual(second.queued, 0)
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual({row['kind'] for row in self.jobs()}, {'import'})

    def test_import_reports_sanitized_account_failures_without_blocking_other_accounts(self):
        rid = str(uuid.uuid4())

        def fake_get(url, **kwargs):
            conn = kwargs['params']['filter[connection_id]']
            if conn == '111':
                return FakeResponse({'message': 'key-for-account-a'}, status=401)
            return FakeResponse({'data': [{'id': rid, 'connection_id': '222', 'to': '+15550000003'}]})

        with mock.patch.object(voice_recordings.requests, 'get', side_effect=fake_get):
            result = voice_recordings.import_recordings(limit=2)
        self.assertEqual((result.queued, result.errors), (1, 1))
        self.assertEqual(result.error_details, ['TELNYX_VOICE_ACCOUNT_A: HTTP 401'])
        self.assertNotIn('key-for-account-a', repr(result))

    def test_recording_import_command_requires_telegram_secret_and_opt_in(self):
        with mock.patch.object(bridge_app, 'send_telegram') as reply, \
             mock.patch.object(bridge_app, '_start_background_recording_import', return_value=True) as start, \
             bridge_app.app.test_client() as client:
            client.post('/telegram/webhook', json=telegram_update('/importrecordings 99'))
            start.assert_not_called()
            self.assertIn('registered TELEGRAM_WEBHOOK_SECRET', reply.call_args.args[1])
            with mock.patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET': 'test-secret'}):
                self.assertEqual(client.post('/telegram/webhook', json=telegram_update('/importrecordings 99')).status_code, 403)
                client.post('/telegram/webhook', json=telegram_update('/importrecordings 99'),
                            headers={'X-Telegram-Bot-Api-Secret-Token': 'test-secret'})
                self.assertEqual(start.call_args.args, (555, 10))
                self.assertIn('native voicemail', reply.call_args.args[1])

    def test_telegram_upload_classifies_uncertain_and_explicit_rejections(self):
        with mock.patch.object(notify.requests, 'post', return_value=FakeResponse({'ok': True})) as post:
            self.assertEqual(notify.send_voice_owner(b'OggS-fixture', 'ogg', 'test'), 'sent')
        self.assertEqual(post.call_args.kwargs['files']['voice'][2], 'audio/ogg')
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        for response, outcome in ((FakeResponse({'ok': False}, status=429), 'retry'),
                                  (FakeResponse({'ok': False}, status=500), 'uncertain'),
                                  (FakeResponse(ValueError('bad response')), 'uncertain')):
            with mock.patch.object(notify.requests, 'post', return_value=response):
                self.assertEqual(notify.send_voice_owner(b'OggS-fixture', 'ogg', 'test'), outcome)
        with mock.patch.object(notify.requests, 'post', side_effect=requests.Timeout('secret-token in URL')):
            with self.assertLogs('notify', level='WARNING') as captured:
                self.assertEqual(notify.send_voice_owner(b'OggS-fixture', 'ogg', 'test'), 'uncertain')
        self.assertNotIn('secret-token', ' '.join(captured.output))


if __name__ == '__main__':
    unittest.main()
