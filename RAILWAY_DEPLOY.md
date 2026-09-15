# Railway deploy — Telnyx → Telegram Bridge

## 1. Connect repo in Railway
Use the GitHub repo and deploy branch `main`.

## 2. Add Railway variables
```env
TELNYX_API_KEYS=
TELNYX_API_KEY=your_main_telnyx_api_key
TELNYX_EXTRA_API_KEYS=your_extra_key_2,your_extra_key_3
TELNYX_BASE_URL=https://api.telnyx.com/v2
TELNYX_FROM_NUMBER=+12015550123
TELNYX_NUMBERS=+12015550123,+12015550124,+12015550125
TELNYX_PUBLIC_KEYS=
TELNYX_PUBLIC_KEY=
TELNYX_EXTRA_PUBLIC_KEYS=
TELNYX_SIGNATURE_TOLERANCE=300
TELNYX_SYNC_LOOKBACK_HOURS=48
TELNYX_SYNC_DATE_RANGE=
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OWNER_TELEGRAM_ID=your_telegram_user_id
TELEGRAM_ALERT_CHAT_ID=your_private_group_chat_id
PUBLIC_BASE_URL=https://YOUR-APP.up.railway.app
```

## 3. Deploy
Railway start command:
```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1
```

## 4. Set Telegram webhook
```bash
PUBLIC_BASE_URL=https://YOUR-APP.up.railway.app python set_telegram_webhook.py
```

## 5. Set Telnyx webhook
In **each** Telnyx account's Messaging Profile:
```text
API Version: API V2
Webhook URL: https://YOUR-APP.up.railway.app/inbound/sms
Webhook Failover URL: blank
```

All your owner-controlled Telnyx accounts can point to the same Railway webhook URL.

## 6. Test
- open `https://YOUR-APP.up.railway.app/health`
- send `/help` to the bot
- send `/syncsms 20` to manually pull recent inbound SMS if needed
- send a normal SMS to your Telnyx number
