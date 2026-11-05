# Telnyx → Telegram Bridge

An owner-only bridge for **Telnyx inbound SMS** to your private Telegram chat or group. It runs as a Flask/Gunicorn service on Railway: Telnyx webhook → SQLite inbox and alert queue → Telegram. Polling and `/syncsms` can recover some messages when a webhook is missed. Optional, disabled-by-default audio features can read new SMS aloud and deliver recordings from explicitly configured Telnyx TeXML calls.

Use this only for numbers **you own or control**, and a private Telegram destination. It is not a public number-rental or third-party OTP-harvesting service. Some platforms do not deliver to VoIP/cloud numbers.

## Commands

- `/help` — command list
- `/latest [limit]`, `/recent <number> [limit]` — view recent SMS
- `/numbers`, `/mynumbers` — numbers with messages and configured numbers
- `/available [country] [area] [limit]` — search available SMS-capable numbers
- `/syncsms [limit]` — start a background sync; the final reply includes counts and safe errors
- `/importrecordings [limit]` — explicitly import up to 10 eligible **programmable call recordings**, not verified native mailbox messages
- `/retryaudio confirm` — explicitly retry failed/unconfirmed audio, with a risk of duplicates
- `/testalert` — test the configured alert destination
- `/status` — configuration, local worker/last-poll status, and pending-alert diagnostics, **not** a live provider check
- `/whoami`, `/chatid` — discover the current user's ID and chat's numeric ID

Owner commands require the configured owner ID **and** an allowed chat: the owner's private chat, the numeric `TELEGRAM_ALERT_CHAT_ID`, or an ID in `TELEGRAM_COMMAND_CHAT_IDS`. Add any other existing command groups to that list before upgrading. `/whoami` and `/chatid` remain available for setup. If the alert destination is a username rather than a numeric chat ID, add its numeric ID to `TELEGRAM_COMMAND_CHAT_IDS` to use owner commands there.

## Configuration

Copy `.env.example` locally or configure Railway variables. Replace all placeholders; never commit secrets. Important settings:

```env
# One or more owner-controlled Telnyx accounts
TELNYX_API_KEY=
TELNYX_EXTRA_API_KEYS=
TELNYX_BASE_URL=https://api.telnyx.com/v2
TELNYX_FROM_NUMBER=
TELNYX_NUMBERS=

# Telnyx webhook authentication (see security migration below)
TELNYX_PUBLIC_KEY=
TELNYX_EXTRA_PUBLIC_KEYS=
TELNYX_REQUIRE_SIGNATURE=false
TELNYX_SIGNATURE_TOLERANCE=300

TELNYX_SYNC_LOOKBACK_HOURS=48
TELNYX_SYNC_DATE_RANGE=
TELNYX_POLLING_ENABLED=true
TELNYX_POLL_INTERVAL_SECONDS=60
TELNYX_POLL_LIMIT=20

TELEGRAM_BOT_TOKEN=
OWNER_TELEGRAM_ID=
TELEGRAM_ALERT_CHAT_ID=
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_REQUIRE_SECRET=false
TELEGRAM_COMMAND_CHAT_IDS=
PUBLIC_BASE_URL=https://your-app.up.railway.app

# Optional: only after attaching a volume and migrating any existing DB
# INBOUND_DB_PATH=/data/inbound.db
```

`TELNYX_API_KEYS` and `TELNYX_PUBLIC_KEYS` also accept comma-separated lists; the corresponding `*_EXTRA_*` variables can contain additional values. The alert chat defaults to the owner's private chat if `TELEGRAM_ALERT_CHAT_ID` is blank. `OWNER_TELEGRAM_ID` must be a positive numeric user ID, not a username or a group ID.

## Secure webhook setup and upgrade

**Existing installations remain unauthenticated until configured.** An owner ID and chat allowlist do not authenticate an incoming HTTP request. Use HTTPS and enable both webhook checks in production. `/status` reports whether settings are present; it cannot verify that Telegram registered the secret or that Telnyx is signing successfully.

### Telegram

1. Choose a strong, random `TELEGRAM_WEBHOOK_SECRET`: 1–256 ASCII letters, digits, underscores, or hyphens. Keep it private and use the **same value** in the registration utility and Railway. Do not paste it into chat or logs.
2. For a smooth upgrade of an existing service, run the new `set_telegram_webhook.py` against the existing public URL **before** deploying the new code with that secret. The old app ignores the extra header; the new app will require it. Make `TELEGRAM_BOT_TOKEN`, `PUBLIC_BASE_URL`, and `TELEGRAM_WEBHOOK_SECRET` available securely to the utility:

   ```bash
   python set_telegram_webhook.py
   ```

3. Set the same secret in Railway, set `TELEGRAM_REQUIRE_SECRET=true`, and deploy. The flag also rejects requests if the secret is accidentally removed. If the new app starts requiring the secret before Telegram is registered, Telegram requests receive HTTP 403 until registration is corrected. Telegram's retries are finite; avoid a long mismatch.
4. Confirm `/status`, then send `/testalert` and verify it appears in the destination. Re-register if the public URL or secret changes.

The utility preserves pending Telegram updates by default. The optional `--drop-pending-updates` flag **irreversibly discards** them. A missing secret with `TELEGRAM_REQUIRE_SECRET` false/unset leaves legacy mode unauthenticated; a required but missing secret or an invalid non-empty secret rejects requests. If `telegram_chat_id.py` reports HTTP 409, a webhook or another poller may be active; use `/whoami` or `/chatid` with the working webhook instead of deleting it.

### Telnyx

1. Configure the public verification key for **each** Telnyx account using `TELNYX_PUBLIC_KEY` and `TELNYX_EXTRA_PUBLIC_KEYS` (comma-separated). A configured key immediately enables signature verification; wrong/missing account keys reject that account's events.
2. Verify delivery for every account, then set `TELNYX_REQUIRE_SIGNATURE=true` to fail closed even if keys are accidentally removed. With no keys and this setting false/unset, inbound webhooks are accepted **without authentication** for legacy compatibility.
3. Set `TELNYX_SIGNATURE_TOLERANCE` to a positive number of seconds (default 300); an invalid value rejects signed requests. Keep the server clock accurate.

In each Telnyx Messaging Profile use API V2 and set the inbound webhook to:

```text
https://YOUR-APP.up.railway.app/inbound/sms
```

The other endpoints are `/telegram/webhook` and `/health`. `/health` checks that the web app responds, **not** that either provider, the queue, or a persistent volume is working.

## Sync, delivery and troubleshooting

1. Send `/status` and `/testalert`. The status distinguishes polling being configured from the polling thread actually running and shows its last completed attempt in this process. The timestamp and counts reset on restart; a live thread is not proof that Telnyx or Telegram is working. Verify the test really appears in the alert chat. Command replies go to the command chat; SMS alerts go to `TELEGRAM_ALERT_CHAT_ID` (or the owner's private chat when unset). The replies flag when a numeric destination differs.
2. Send a harmless test SMS from a phone you control. Confirm the Telnyx number is SMS-enabled, attached to the right messaging profile, and has the correct inbound webhook. Check Telnyx's message records and webhook delivery status.
3. Send `/syncsms 20` and inspect the **final** reply. `checked` is the number of eligible records examined in that run, **including previously stored messages**, not a count of newly delivered alerts. `stored/updated` is also not a delivery count. `alerts pending` means stored alerts have not yet been confirmed by Telegram; `/latest` shows the inbox in an authorized chat. `records returned: 0` means the configured accounts and date range returned none. Account errors are sanitized. Up to 50 errors are shown; `acct1`, `acct2`, etc. are positions in the current deduplicated key list, so they can change when keys are added or removed.
4. Sync inspects the newest records first, up to the requested per-account limit (20 by default; `/syncsms` and `TELNYX_POLL_LIMIT` accept up to 500 and at most 10 API pages). It warns when the requested limit is reached or more pages fail. Older messages beyond that limit may not be recovered. Higher limits can take longer and generate more provider requests. Up to four accounts are scanned concurrently, but individual message lookups remain sequential within an account.
5. If a new, identifiable record has a sender and recipient but Telnyx does not provide the body, the bridge queues a **message text unavailable** notice instead of silently skipping it. Sync continues to retry the body, and a later successful lookup or webhook queues the recovered text; if the first notice has already been sent, this intentionally results in a second, complete alert. On the first sync after upgrading, such notices can include previously unrecorded messages within the lookback window. Already complete records and legacy rows are not automatically replayed merely because of this upgrade.
6. Telnyx redaction, media-only messages, provider retention, lookup failures, or a missing message body can prevent text recovery; the bridge cannot reconstruct a code Telnyx does not provide. Telnyx's [redaction documentation](https://developers.telnyx.com/docs/messaging/messages/message-redaction) describes how profile redaction affects the API and inbound webhooks. Review the privacy implications before changing redaction. Polling is a fallback, not a replacement for a functioning webhook.

Webhooks use Telnyx's receive time (or event time) for inbox ordering and display when a valid timestamp is available; otherwise the bridge uses arrival time. Timestamps without an offset are treated as UTC, and implausibly future timestamps are ignored. A late webhook is still queued and retained from its arrival at the bridge; sync records are retained from their provider timestamp. Failed alerts remain queued and become eligible for retry after 30 seconds while the service is running; worker timing can add delay. A failed message does not indefinitely block newer alerts; retries are rate-limited during an outage. A lease interrupted by a crash can take up to two minutes to become eligible again. SQLite data and pending alerts are removed once older than the configured lookback (48 hours by default, range 1–720 hours), including when polling is off.

## Optional audio: read SMS and record calls

All three audio switches below default to **false**. Merely deploying this version does not change a phone number's routing or replay old messages. Audio goes to `TELEGRAM_ALERT_CHAT_ID`, or to the owner's private chat if that setting is blank—not necessarily to the command chat. **Every member of a destination group/channel can hear sensitive codes and recordings.** Confirm the destination and bot access with `/testalert` first. `/status` reports feature settings and audio queue counts, not end-to-end delivery.

### Read new SMS aloud

Set `SMS_VOICE_ENABLED=true` and redeploy. New complete SMS text is synthesized **locally**, using eSpeak NG and FFmpeg; it is not sent to another speech service. It produces an Ogg/Opus Telegram voice note in addition to the normal text alert. The default `SMS_VOICE_LANGUAGE=en-us` produces a synthetic voice; digit sequences of four or more digits are spoken individually. Other eSpeak voice names can be configured, but must be available in the bundled voice data. Check the licenses of these third-party components before redistributing them.

A webhook must have a valid Telnyx signature to create an audio job; legacy unsigned webhooks can still create text alerts but **not** audio directly. Authenticated Telnyx API polling can also queue audio. If an unsigned webhook arrived first, polling must independently confirm the same sender, destination and complete text; a mismatch is reported and is not spoken. Only messages received after the feature was enabled are eligible: existing database rows and older sync results are not read aloud merely by upgrading. The activation time survives a restart with the feature still enabled. Text unavailable due to redaction or media-only content cannot be spoken; a later authenticated recovery can queue speech if the original message was received after activation. SMS longer than 2,000 characters remains available as text but is not silently truncated into audio; `/status` shows the failure.

### Record new incoming calls

**This takes over answering for the numbers you assign to it.** It delivers a recording after the call; it is not a live or two-way Telegram call. It does not ring your existing phone or preserve its current native-voicemail routing. Voice calls and recordings may incur Telnyx charges. Obtain any legally required recording consent/notice before enabling this. The default recording beep is a notice, not a guarantee of legal compliance, and it or call setup can cause the beginning of an automated code to be missed. Test with a harmless call on a disposable number you own before moving any important number.

1. In each relevant Telnyx account, create a **separate TeXML application**. Set its voice URL method to **POST** and its URL to `https://YOUR-APP.up.railway.app/inbound/voice`. Do not assign existing numbers to it yet.
2. Configure the following privately in Railway, using the numeric TeXML connection ID and the API/public verification keys of the account that owns that connection. Add a separate prefix/key pair for each account; do not rely on the ordering of `TELNYX_API_KEYS`:

   ```env
   TELNYX_VOICE_ENABLED=true
   TELNYX_VOICE_CONNECTIONS=123456=TELNYX_VOICE_ACCOUNT_A,789012=TELNYX_VOICE_ACCOUNT_B
   TELNYX_VOICE_ACCOUNT_A_API_KEY=<account-A-API-key>
   TELNYX_VOICE_ACCOUNT_A_PUBLIC_KEY=<account-A-Ed25519-public-key>
   TELNYX_VOICE_ACCOUNT_B_API_KEY=<account-B-API-key>
   TELNYX_VOICE_ACCOUNT_B_PUBLIC_KEY=<account-B-Ed25519-public-key>
   TELNYX_VOICE_NUMBERS=+15550000001,+15550000002
   VOICE_PUBLIC_BASE_URL=https://YOUR-APP.up.railway.app
   TELNYX_VOICE_MAX_SECONDS=120
   TELNYX_VOICE_PLAY_BEEP=true
   ```

   The phone numbers above are placeholders; use only numbers you control. The base URL must be the public HTTPS origin without a path; if omitted, `PUBLIC_BASE_URL` is used. The API key is used to refresh expired recording links. The connection-specific public key verifies incoming-call requests and recording callbacks: missing/invalid signatures, unknown connections, and non-allowlisted destination numbers fail closed. The Telegram token and alert destination must also be configured. The bridge assumes Telnyx signs its initial TeXML instruction and recording callback POSTs; **verify this with your account before switching a number**. It rejects unsigned requests to those endpoints rather than falling back to unauthenticated mode. The separate, stateless action endpoint always returns a fixed Hangup instruction, even without a signature or after disabling calls, to reduce the risk of leaving a call connected; it accepts no recording or account data.
3. Deploy the code and settings, check `/status`, then deliberately assign only a test number to the new TeXML application. Have an authorized caller place a test call and check that a playable voice note reaches the intended chat. Confirm the entire recording, the hangup, actual Telnyx charges, and webhook delivery before adding more numbers. Changing this assignment is what changes voice routing; this project never changes it for you.

Telnyx receives instructions to record up to `TELNYX_VOICE_MAX_SECONDS` (30–300, default 120), then request a hangup. This is a **recording** limit, not a guaranteed provider-side call-duration limit if the app or network fails when the hangup instruction is fetched; configure appropriate Telnyx failover/timeouts and monitor costs. `TELNYX_VOICE_PLAY_BEEP=false` removes the beep only if the operator deliberately changes it; the operator remains responsible for consent. Playback begins only after Telnyx starts recording, so a voice-code caller may speak too early. The integration has not been verified against your live Telnyx/Telegram accounts.

### Existing recordings versus native voicemail

Set `TELNYX_RECORDING_IMPORT_ENABLED=true`, configure the same connection/account mapping and allowed numbers, and register a `TELEGRAM_WEBHOOK_SECRET`. An authorized owner can then send `/importrecordings 5` to intentionally queue up to five eligible **programmable call recordings** from Telnyx's `/v2/recordings` API (maximum 10). The importer scans one bounded page per configured connection; it may not find all recordings and does not claim a particular order. It verifies connection and destination, avoids re-sending already queued/sent recording IDs while their deduplication entries are retained, and sends audio to the configured alert chat. It never automatically imports old recordings on startup.

**This is not a proven import of Telnyx's native voicemail mailbox.** Telnyx's documented voicemail API exposes settings, not a documented mailbox-message list; Telnyx describes listening to native voicemail by dialing `*98` from the voicemail-enabled number and authenticating. Confirm export/forwarding options with Telnyx support before relying on this for existing mailbox messages. Recordings already available through the programmable recordings API can be deliberately imported as above.

Recording URLs can expire after about ten minutes. The worker saves downloaded audio to SQLite for retry and attempts to refresh an expired URL with the owning account's API key. Downloads are restricted to Telnyx's documented `telephony-recorder-prod` S3 bucket or the exact Telnyx recording API path; custom S3/GCS/Azure locations, redirects, unsupported formats, and recordings over 20 MiB are not supported. MP3 is uploaded directly; WAV is converted locally to Ogg/Opus. No signed recording URL or bot/API key is sent to Telegram as text.

Audio is queued independently from text alerts. An explicit Telegram rejection is retried; if an upload times out or the worker crashes after upload begins, the result is **unconfirmed** and is not automatically resent because Telegram may already have accepted it. `/status` shows pending/unconfirmed/failed counts and sanitized reasons. An owner can send `/retryaudio confirm` to requeue failed or unconfirmed items, knowing it may duplicate a voice note. This command and `/importrecordings` require a registered Telegram webhook secret as well as the owner/chat checks. Disabling an audio feature cancels its queued jobs so enabling it again does not unexpectedly replay them; an upload already in progress can still arrive. Audio jobs, including pending/failed/unconfirmed jobs, are pruned after the same configured lookback as the SMS inbox (48 hours by default).

## Persistence and delivery guarantees

Railway's container filesystem is ephemeral. Back up an existing database before upgrading: the app automatically adds missing columns and indexes on startup. To retain the inbox, pending queue, and deduplication state across container replacement, attach a Railway volume at `/data` and set `INBOUND_DB_PATH=/data/inbound.db`. **Changing the path does not copy an existing database.** Migrate it safely first (stop writes or use SQLite's online backup API). Restrict access to the volume and backups: they can contain SMS text, pending audio and expiring signed recording links. Audio blobs are cleared on confirmed delivery, but SQLite deletion is not guaranteed secure erasure; Telegram and Telnyx also have their own retention.

Use one Railway replica and the provided single Gunicorn worker with four request threads, with Railway Serverless/app sleeping disabled when continuous polling and retries are needed. The request threads allow incoming webhooks to proceed while an owner command waits on a provider; a saturated server can still delay requests. If Railway overrides the start command, update that override to match `railway.json`. Text-alert delivery is **at least once**: a crash after Telegram accepts an alert but before SQLite records success can duplicate it. Audio uses the more conservative unconfirmed-delivery behavior described above. Old rows are not enqueued just by migration, but older webhook rows without provider IDs or replacement of an ephemeral DB can cause recent messages to be rediscovered and alerted again. Recovery is not guaranteed for messages beyond the window/scan limit or unavailable from Telnyx.

Never share bot tokens, API keys, webhook secrets, phone numbers, SMS contents, or verification codes when requesting help.

## Run locally

The Railway Nixpacks version file selects Python 3.11, matching the tested runtime. A Railway `NIXPACKS_PYTHON_VERSION` variable may override it. Direct dependencies in `requirements.txt` are pinned to the versions tested here; transitive dependencies are not fully locked, so update and test dependencies deliberately. The bundled native eSpeak/FFmpeg packages are installed even when audio is disabled and add roughly 185 MiB unpacked on the tested Linux x86-64 runtime; allow for the larger build and check their redistribution licenses.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
python -m unittest discover -s tests -v
```

The GitHub Actions workflow runs the same offline and mocked-provider Gunicorn HTTP tests on pushes and pull requests; it does not require provider credentials.
