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
TELNYX_API_KEY=KEYxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELNYX_BASE_URL=https://api.telnyx.com/v2
TELNYX_FROM_NUMBER=+12015550123
TELNYX_NUMBERS=+12015550123
TELNYX_PUBLIC_KEY=
TELNYX_SIGNATURE_TOLERANCE=300

TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OWNER_TELEGRAM_ID=000000000
TELEGRAM_ALERT_CHAT_ID=-1001234567890

PUBLIC_BASE_URL=https://your-app.up.railway.app
```

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
