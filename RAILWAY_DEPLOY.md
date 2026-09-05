# Deploy 1BigWilliam SMS Telegram Toolkit to Railway

Railway gives this Python app a permanent HTTPS URL, so you do not need ngrok.

Your deployed URLs will look like:

```text
https://YOUR-APP.up.railway.app/health
https://YOUR-APP.up.railway.app/inbound/sms
https://YOUR-APP.up.railway.app/telegram/webhook
```

---

## Stage 1 — GitHub source repo

This project is already available here:

```text
https://github.com/elonwilliammusk426-arch/1BigWilliam
```

Git clone URL:

```text
https://github.com/elonwilliammusk426-arch/1BigWilliam.git
```

You can deploy directly from this repo, or from your own fork/private copy.

Do **not** commit your real `.env`.

---

## Stage 2 — Create a Railway project

1. Go to `https://railway.app`
2. Log in
3. Click **New Project**
4. Choose **Deploy from GitHub repo**
5. Select `elonwilliammusk426-arch/1BigWilliam`
6. Select branch `main`

Railway should detect Python/Nixpacks automatically.

The included `railway.json` / `Procfile` start the app with:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1
```

---

## Stage 2B — If your Railway service is already connected to the wrong repo

Open:

```text
Project → Service → Settings → Service Source → Connect Repo
```

Then select:

```text
elonwilliammusk426-arch/1BigWilliam
branch: main
```

Review the staged changes and deploy them.

---

## Stage 3 — Add environment variables

In Railway:

```text
Project → Service → Variables
```

Paste values from:

```text
RAILWAY_VARIABLES_TEMPLATE.env
```

Minimum example:

```env
TELNYX_API_KEY=your_primary_telnyx_api_key
TELNYX_EXTRA_API_KEYS=
TELNYX_BASE_URL=https://api.telnyx.com/v2
TELNYX_FROM_NUMBER=+12015550123
TELNYX_NUMBERS=+12015550123
TELNYX_PUBLIC_KEY=
TELNYX_EXTRA_PUBLIC_KEYS=
TELNYX_SIGNATURE_TOLERANCE=300
TELNYX_POLLING_ENABLED=true
TELNYX_POLL_INTERVAL_SECONDS=300
TELNYX_POLL_LIMIT=10
TELNYX_SYNC_DATE_RANGE=
NUMVERIFY_API_KEY=
NUMVERIFY_BASE_URL=https://apilayer.net/api/validate
OTP_LENGTH=6
OTP_TTL_SECONDS=300
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OWNER_TELEGRAM_ID=your_telegram_user_id
TELEGRAM_ALERT_CHAT_ID=your_private_group_chat_id
```

For first test, `TELNYX_PUBLIC_KEY` can be blank. Add it later for stronger production security.

---

## Stage 4 — Deploy

After variables are added, Railway will deploy or redeploy automatically.

Open the deployment logs and wait until the app is running.

---

## Stage 5 — Generate public domain

In Railway:

```text
Project → Service → Settings → Networking → Generate Domain
```

Test:

```text
https://YOUR-APP.up.railway.app/health
```

Expected response:

```json
{"ok": true, "service": "sms-telegram-cloud"}
```

---

## Stage 6 — Connect Telegram webhook

Run this once from a shell/workspace:

```bash
PUBLIC_BASE_URL=https://YOUR-APP.up.railway.app python set_telegram_webhook.py
```

Telegram will then send bot commands to:

```text
https://YOUR-APP.up.railway.app/telegram/webhook
```

---

## Stage 7 — Connect Telnyx webhook

In each Telnyx Messaging Profile, set:

```text
API Version: API V2
Webhook URL: https://YOUR-APP.up.railway.app/inbound/sms
Webhook Failover URL: blank
```

Save.

---

## Stage 8 — Test from Telegram

Run:

```text
/help
/testalert
/mynumbers
/syncsms 20
/latest
/checknum +12015550123
```

`/checknum` works only after you add `NUMVERIFY_API_KEY`.

Then send an SMS to one of your Telnyx numbers. It should drop into your private Telegram alert chat.

---

## Storage note

This project currently stores inbound history in local SQLite. That is okay for testing, but for stronger persistence you can move message history / OTP storage to Redis or Postgres later.
