# Railway deploy — Telnyx → Telegram Bridge

These steps describe a deployment; making repository changes alone does not deploy them. See the [README](README.md) for security migration, troubleshooting, and delivery guarantees.

## 1. Prepare and configure

Connect the intended GitHub branch in Railway. Keep **one replica and one Gunicorn worker**, using the four request threads in the start command below. This lets incoming webhooks proceed while an owner command waits on a provider. The checked-in Nixpacks configuration uses Python 3.11 via `.python-version`; `NIXPACKS_PYTHON_VERSION` can override it.

Set the following Railway variables to your own values (never commit or share them):

```env
TELNYX_API_KEY=<main-account-key>
TELNYX_EXTRA_API_KEYS=<other-account-keys-comma-separated>
TELNYX_BASE_URL=https://api.telnyx.com/v2
TELNYX_FROM_NUMBER=<optional-owned-number>
TELNYX_NUMBERS=<optional-owned-numbers-comma-separated>
TELNYX_PUBLIC_KEY=<main-account-public-verification-key>
TELNYX_EXTRA_PUBLIC_KEYS=<other-account-public-keys-comma-separated>
TELNYX_SIGNATURE_TOLERANCE=300
# Set true after configuring and testing every account's public key.
TELNYX_REQUIRE_SIGNATURE=true
TELNYX_SYNC_LOOKBACK_HOURS=48
TELNYX_SYNC_DATE_RANGE=
TELNYX_POLLING_ENABLED=true
TELNYX_POLL_INTERVAL_SECONDS=60
TELNYX_POLL_LIMIT=20

TELEGRAM_BOT_TOKEN=<bot-token>
OWNER_TELEGRAM_ID=<numeric-owner-user-id>
TELEGRAM_ALERT_CHAT_ID=<numeric-private-chat-or-group-id>
TELEGRAM_WEBHOOK_SECRET=<strong-random-secret>
TELEGRAM_REQUIRE_SECRET=true
# Optional: other numeric chats where the owner may issue commands
TELEGRAM_COMMAND_CHAT_IDS=
PUBLIC_BASE_URL=https://YOUR-APP.up.railway.app
```

Blank `TELEGRAM_WEBHOOK_SECRET` with `TELEGRAM_REQUIRE_SECRET=false`, or blank Telnyx public keys with `TELNYX_REQUIRE_SIGNATURE=false`, leaves that webhook unauthenticated. Any configured Telnyx public key immediately enables verification; a missing or wrong key rejects events from that account. Test all accounts before requiring signatures.

For existing bots, register the new Telegram secret **before** deploying the new app with the same secret, as described in the README. Add any existing owner-command group that is neither the owner's private chat nor the numeric alert chat to `TELEGRAM_COMMAND_CHAT_IDS` before upgrading. Otherwise owner commands in that group will be rejected.

## 2. Persistent storage and background work

Railway container filesystems are ephemeral. Back up an existing database before upgrading; the app automatically adds missing columns and indexes on startup. To persist the inbox, alert queue and deduplication state, attach a volume at `/data` and set `INBOUND_DB_PATH=/data/inbound.db`. Changing the path does not migrate an existing database: safely copy/restore the backup first, stopping writes or using SQLite's online backup API. The DB and backups contain SMS text; restrict access.

Disable Railway Serverless/app sleeping if polling and retries must continue while there is no incoming traffic. Messages and pending alerts are pruned after the configured lookback (48 hours by default).

## 3. Deploy and register webhooks

The repository's start command is shown below. If Railway has a custom start-command override, update it too; otherwise the old single-request worker can block webhooks while a provider call is slow. The zero keep-alive timeout limits idle HTTP connections, which can otherwise delay a graceful worker shutdown.

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 4 --timeout 120 --keep-alive 0
```

Using the same bot token and Telegram webhook secret as Railway, run the setup utility with `PUBLIC_BASE_URL` set to the deployed HTTPS base URL:

```bash
python set_telegram_webhook.py
```

The utility preserves pending Telegram updates unless `--drop-pending-updates` is explicitly specified. If the app requires a secret before Telegram is registered with it, updates receive HTTP 403 until they match; retries are finite.

In **each** Telnyx account's Messaging Profile, configure API V2 and the inbound webhook:

```text
https://YOUR-APP.up.railway.app/inbound/sms
```

## 4. Test and diagnose

- Open `/health`; it checks the process response, not providers or persistence.
- Send `/status` and `/testalert`, and verify the alert actually appears in the destination.
- Send a harmless test SMS from a phone you control. Check the Telnyx profile, message record, and webhook delivery status if it does not arrive.
- Send `/syncsms 20` and inspect its final `records returned`, `stored/updated`, `alerts pending`, and errors. It scans a bounded number of newest records per account; text may not be available if Telnyx redacted it.
- Check Railway logs for sanitized errors. Never share secrets, phone numbers, SMS contents, or verification codes.

A crash after Telegram accepts an alert but before SQLite records success may duplicate it. Replacing an ephemeral DB, or rediscovering older rows without provider IDs, can also replay recent alerts. Retention and provider availability limit recovery.
