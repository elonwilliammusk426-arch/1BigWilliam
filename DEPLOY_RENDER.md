# Deploy SMS Telegram Toolkit to Render instead of ngrok

Upstash is great for Redis/database, but it does **not** host this Python webhook server. For the public HTTPS URL that replaces ngrok, use a web host like **Render**, Railway, Fly.io, or a VPS.

This guide uses Render because it is simple.

---

## What gets deployed

One cloud web service running `app.py`.

It handles both:

```text
Telnyx inbound SMS  -> https://YOUR-APP.onrender.com/inbound/sms
Telegram commands   -> https://YOUR-APP.onrender.com/telegram/webhook
Health check        -> https://YOUR-APP.onrender.com/health
```

So in production you do **not** need to run `bot.py` polling separately.

---

## Stage 1 — Put project on GitHub

Render normally deploys from GitHub.

Create a private GitHub repo and upload the project folder contents.

Do **not** commit real secrets if you can avoid it. In Render, secrets are added as Environment Variables.

---

## Stage 2 — Create Render Web Service

In Render:

1. Click **New +**
2. Choose **Web Service**
3. Connect your GitHub repo
4. Runtime: **Python**
5. Build command:

```bash
pip install -r requirements.txt
```

6. Start command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1
```

---

## Stage 3 — Add Render environment variables

Add these in Render Environment settings:

```env
TELNYX_API_KEY=your_telnyx_api_key
TELNYX_BASE_URL=https://api.telnyx.com/v2
TELNYX_FROM_NUMBER=+13412043006
TELNYX_PUBLIC_KEY=
TELNYX_SIGNATURE_TOLERANCE=300
OTP_LENGTH=6
OTP_TTL_SECONDS=300
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OWNER_TELEGRAM_ID=8355280769
TELEGRAM_ALERT_CHAT_ID=-1004447057144
```

For first test, `TELNYX_PUBLIC_KEY` can be blank. Set it later for production security.

---

## Stage 4 — Deploy and copy your Render URL

After deploy, Render gives you a URL like:

```text
https://sms-telegram-cloud.onrender.com
```

Test:

```text
https://sms-telegram-cloud.onrender.com/health
```

You should see:

```json
{"ok": true, "service": "sms-telegram-cloud"}
```

---

## Stage 5 — Set Telegram webhook

Telegram must know where to send bot commands.

Run this once from your computer/workspace after replacing the URL:

```bash
cd sms_project
PUBLIC_BASE_URL=https://sms-telegram-cloud.onrender.com python set_telegram_webhook.py
```

If successful, Telegram commands go to Render automatically.

---

## Stage 6 — Set Telnyx webhook

In Telnyx:

```text
Messaging → Messaging Profiles → your profile
```

Set API version:

```text
API V2
```

Set webhook URL:

```text
https://sms-telegram-cloud.onrender.com/inbound/sms
```

Save.

---

## Stage 7 — Test

1. Text your Telnyx number:

```text
+13412043006
```

2. The SMS should drop into your Telegram alert group.

3. In Telegram, test:

```text
/help
/testalert
/latest
/recent +13412043006
```

---

## Note about Upstash

Render free web services can restart, and local SQLite files may not be permanent. For production persistence, add Upstash Redis later for:

```text
inbound SMS history
OTP storage
rate limiting
```

But Upstash does not replace Render/ngrok as the public webhook server.
