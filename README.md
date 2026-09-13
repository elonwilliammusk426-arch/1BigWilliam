# Telnyx → Telegram Bridge

A stripped-back owner-only bridge for **Telnyx inbound SMS** into your **private Telegram inbox/group**.

This rebuild intentionally removes the dashboard, voice/call features, bulk audit tools, and extra app layers.
It goes back to the simple Railway deploy shape: **Telnyx webhook → Flask app → Telegram alert**.

## Boundary
Use this only for numbers **you own/control** through Telnyx and your own private Telegram inbox.
This is not a public number-rental or third-party OTP harvesting service.
Some platforms may still refuse or block delivery to VoIP/cloud numbers.

## What it does
- receives inbound SMS from Telnyx on `/inbound/sms`
- stores them in local SQLite `inbound.db`
- forwards them to Telegram
- supports simple Telegram owner commands on `/telegram/webhook`
- supports **one or many Telnyx accounts** pointing to the same Railway webhook URL

## Telegram commands
- `/help`
- `/latest [limit]`
- `/recent <number> [limit]`
- `/numbers`
- `/mynumbers`
- `/available [country] [area] [limit]`
- `/testalert`
- `/whoami`
- `/chatid`

## Required env vars
```env
TELNYX_API_KEYS=
TELNYX_API_KEY=KEYxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELNYX_EXTRA_API_KEYS=
TELNYX_BASE_URL=https://api.telnyx.com/v2
TELNYX_FROM_NUMBER=+12015550123
TELNYX_NUMBERS=+12015550123,+12015550124
TELNYX_PUBLIC_KEYS=
TELNYX_PUBLIC_KEY=
TELNYX_EXTRA_PUBLIC_KEYS=
TELNYX_SIGNATURE_TOLERANCE=300

TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OWNER_TELEGRAM_ID=000000000
TELEGRAM_ALERT_CHAT_ID=-1001234567890

PUBLIC_BASE_URL=https://your-app.up.railway.app
```

## Multi-account notes
If you have many Telnyx accounts, keep the first/main key in `TELNYX_API_KEY` and put the others in `TELNYX_EXTRA_API_KEYS`, comma-separated.

Example:
```env
TELNYX_API_KEY=KEY_ACCOUNT_1
TELNYX_EXTRA_API_KEYS=KEY_ACCOUNT_2,KEY_ACCOUNT_3,KEY_ACCOUNT_4
TELNYX_NUMBERS=+1NUMBER1,+1NUMBER2,+1NUMBER3,+1NUMBER4
```

If you want to verify inbound webhook signatures for many accounts, do the same with public keys:

```env
TELNYX_PUBLIC_KEY=PUBLIC_KEY_ACCOUNT_1
TELNYX_EXTRA_PUBLIC_KEYS=PUBLIC_KEY_ACCOUNT_2,PUBLIC_KEY_ACCOUNT_3,PUBLIC_KEY_ACCOUNT_4
```

If you do not want signature verification during testing, leave the public key vars blank.

## Railway URLs
After deploy:
```text
https://YOUR-APP.up.railway.app/health
https://YOUR-APP.up.railway.app/inbound/sms
https://YOUR-APP.up.railway.app/telegram/webhook
```

## Telnyx setup
In your Telnyx Messaging Profile:
```text
API Version: API V2
Webhook URL: https://YOUR-APP.up.railway.app/inbound/sms
Webhook Failover URL: blank
```

## Telegram setup
Run once after deploy:
```bash
PUBLIC_BASE_URL=https://YOUR-APP.up.railway.app python set_telegram_webhook.py
```

## Start locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
