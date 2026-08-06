# Deploy SMS Telegram Toolkit to Railway

Railway can replace ngrok. It gives your Python app a permanent HTTPS URL.

Your deployed URLs will be:

```text
https://YOUR-APP.up.railway.app/health
https://YOUR-APP.up.railway.app/inbound/sms
https://YOUR-APP.up.railway.app/telegram/webhook
```

---

## Stage 1 — Put code on GitHub

Railway deploys easiest from GitHub.

Upload this project to a **private** GitHub repo.

Do **not** upload `.env`.

The repo root should contain:

```text
app.py
requirements.txt
railway.json
Procfile
config.py
telnyx.py
telnyx_webhook.py
store.py
notify.py
```

---

## Stage 2 — Create Railway project

1. Go to: https://railway.app
2. Login.
3. Click **New Project**.
4. Choose **Deploy from GitHub repo**.
5. Select your repo.
6. Railway should detect Python/Nixpacks automatically.

The included `railway.json` sets the start command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1
```

---

## Stage 3 — Add environment variables

In Railway:

```text
Project → Service → Variables
```

Add the variables below, or copy from `RAILWAY_VARIABLES_TEMPLATE.env` and replace the placeholders.

```env
TELNYX_API_KEY=your_primary_telnyx_api_key
TELNYX_EXTRA_API_KEYS=
TELNYX_BASE_URL=https://api.telnyx.com/v2
TELNYX_FROM_NUMBER=+15306908868
TELNYX_NUMBERS=+15306908868
TELNYX_PUBLIC_KEY=
TELNYX_EXTRA_PUBLIC_KEYS=
TELNYX_SIGNATURE_TOLERANCE=300
TELNYX_POLLING_ENABLED=true
TELNYX_POLL_INTERVAL_SECONDS=30
TELNYX_POLL_LIMIT=20
TELNYX_SYNC_DATE_RANGE=
NUMVERIFY_API_KEY=
NUMVERIFY_BASE_URL=https://apilayer.net/api/validate
OTP_LENGTH=6
OTP_TTL_SECONDS=300
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OWNER_TELEGRAM_ID=8355280769
TELEGRAM_ALERT_CHAT_ID=-1004447057144
```

For first test, `TELNYX_PUBLIC_KEY` can be blank. Add it later for production security.

---

## Stage 4 — Deploy

After variables are added, Railway will deploy/redeploy automatically.

Open the deployment logs. Wait until it says the app is running.

---

## Stage 5 — Generate public domain

In Railway:

```text
Project → Service → Settings → Networking
```

Click:

```text
Generate Domain
```

Railway will give you something like:

```text
https://sms-telegram-production.up.railway.app
```

Test:

```text
https://sms-telegram-production.up.railway.app/health
```

Expected:

```json
{"ok": true, "service": "sms-telegram-cloud"}
```

---

## Stage 6 — Connect Telegram webhook

After you have your Railway domain, run this once:

```bash
PUBLIC_BASE_URL=https://YOUR-APP.up.railway.app python set_telegram_webhook.py
```

This tells Telegram to send bot commands to:

```text
https://YOUR-APP.up.railway.app/telegram/webhook
```

---

## Stage 7 — Connect Telnyx webhook

In Telnyx Messaging Profile:

```text
API Version: API V2
Webhook URL: https://YOUR-APP.up.railway.app/inbound/sms
```

Save.

---

## Stage 8 — Test

In Telegram:

```text
/help
/testalert
/mynumbers
/checknum +15306908868
```

`/checknum` works only after you add `NUMVERIFY_API_KEY`.

Then send an SMS to your Telnyx number:

```text
+15306908868
```

The SMS should drop into your Telegram alert group.

---

## Note about storage

This deployment currently stores inbound history in local SQLite. That is fine for testing. For production, use Upstash Redis/Postgres later so message history survives redeploys/restarts reliably.
