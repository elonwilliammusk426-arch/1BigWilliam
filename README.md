# Telnyx → Telegram Bridge

An owner-only bridge for **Telnyx inbound SMS** to your private Telegram chat or group. It runs as a Flask/Gunicorn service on Railway: Telnyx webhook → SQLite inbox and alert queue → Telegram. Polling and `/syncsms` can recover some messages when a webhook is missed.

Use this only for numbers **you own or control**, and a private Telegram destination. It is not a public number-rental or third-party OTP-harvesting service. Some platforms do not deliver to VoIP/cloud numbers.

## Commands

- `/help` — command list
- `/latest [limit]`, `/recent <number> [limit]` — view recent SMS
- `/numbers`, `/mynumbers` — numbers with messages and configured numbers
- `/available [country] [area] [limit]` — search available SMS-capable numbers
- `/syncsms [limit]` — start a background sync; the final reply includes counts and safe errors
- `/testalert` — test the configured alert destination
- `/status` — configuration and pending-alert diagnostics, **not** a live provider check
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

1. Send `/status` and `/testalert`. Verify the test really appears in the alert chat; check the bot's access if it fails.
2. Send a harmless test SMS from a phone you control. Confirm the Telnyx number is SMS-enabled, attached to the right messaging profile, and has the correct inbound webhook. Check Telnyx's message records and webhook delivery status.
3. Send `/syncsms 20` and inspect the **final** reply. `records returned: 0` means the configured accounts and date range returned none. `alerts pending` means stored alerts have not yet been confirmed by Telegram. Account errors are sanitized.
4. Sync inspects the newest records first, up to the requested per-account limit (20 by default; `/syncsms` and `TELNYX_POLL_LIMIT` accept up to 500 and at most 10 API pages). It warns when the requested limit is reached or more pages fail. Older messages beyond that limit may not be recovered. Higher limits can take longer and generate more provider requests. Up to four accounts are scanned concurrently, but individual message lookups remain sequential within an account.
5. Telnyx redaction, media-only messages, provider retention, or a missing message body can prevent recovery. Review the privacy implications before changing redaction. Polling is a fallback, not a replacement for a functioning webhook.

Failed alerts remain queued and become eligible for retry after 30 seconds while the service is running; worker timing can add delay. A failed message does not indefinitely block newer alerts; retries are rate-limited during an outage. A lease interrupted by a crash can take up to two minutes to become eligible again. SQLite data and pending alerts are removed once older than the configured lookback (48 hours by default, range 1–720 hours), including when polling is off.

## Persistence and delivery guarantees

Railway's container filesystem is ephemeral. To retain the inbox, pending queue, and deduplication state across container replacement, attach a Railway volume at `/data` and set `INBOUND_DB_PATH=/data/inbound.db`. **Changing the path does not copy an existing database.** Back up and migrate it safely first (stop writes or use SQLite's online backup API). Restrict access to the volume and backups: they contain SMS text.

Use one Railway replica and the provided single Gunicorn worker, with Railway Serverless/app sleeping disabled when continuous polling and retries are needed. Delivery is **at least once**: a crash after Telegram accepts an alert but before SQLite records success can duplicate it. Old rows are not enqueued just by migration, but older webhook rows without provider IDs or replacement of an ephemeral DB can cause recent messages to be rediscovered and alerted again. Recovery is not guaranteed for messages beyond the window/scan limit or unavailable from Telnyx.

Never share bot tokens, API keys, webhook secrets, phone numbers, SMS contents, or verification codes when requesting help.

## Run locally

The Railway Nixpacks version file selects Python 3.11, matching the tested runtime. A Railway `NIXPACKS_PYTHON_VERSION` variable may override it. Direct dependencies in `requirements.txt` are pinned to the versions tested here; transitive dependencies are not fully locked, so update and test dependencies deliberately.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
python -m unittest discover -s tests -v
```

The GitHub Actions workflow runs the same offline and mocked-provider Gunicorn HTTP tests on pushes and pull requests; it does not require provider credentials.
