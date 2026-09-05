# Mobile Railway checklist — 1BigWilliam

Use this if you are setting up from your phone.

## 1) Open Railway and connect the repo

Repo:

```text
https://github.com/elonwilliammusk426-arch/1BigWilliam
```

Path in Railway:

```text
New Project → Deploy from GitHub repo → elonwilliammusk426-arch/1BigWilliam → main
```

If the Railway service already exists and is connected to the wrong repo:

```text
Project → Service → Settings → Service Source → Connect Repo → elonwilliammusk426-arch/1BigWilliam → main
```

## 2) Paste variables

Open this file in the repo and copy from it:

```text
RAILWAY_VARIABLES_TEMPLATE.env
```

Required minimum values:

```env
TELNYX_API_KEY=YOUR_MAIN_TELNYX_API_KEY
TELNYX_EXTRA_API_KEYS=YOUR_OTHER_KEYS_COMMA_SEPARATED
TELNYX_FROM_NUMBER=+1YOURMAINNUMBER
TELNYX_NUMBERS=+1YOURMAINNUMBER,+1YOURSECONDNUMBER
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
OWNER_TELEGRAM_ID=YOUR_TELEGRAM_USER_ID
TELEGRAM_ALERT_CHAT_ID=YOUR_PRIVATE_GROUP_CHAT_ID
TELNYX_POLLING_ENABLED=true
TELNYX_POLL_INTERVAL_SECONDS=300
TELNYX_POLL_LIMIT=10
TELNYX_SYNC_DATE_RANGE=
```

## 3) Generate Railway domain

```text
Project → Service → Settings → Networking → Generate Domain
```

Test:

```text
https://YOUR-APP.up.railway.app/health
```

Expected:

```json
{"ok": true, "service": "sms-telegram-cloud"}
```

## 4) Connect Telegram webhook

Run once from a shell/workspace:

```bash
PUBLIC_BASE_URL=https://YOUR-APP.up.railway.app python set_telegram_webhook.py
```

## 5) Connect Telnyx webhook

In each Telnyx Messaging Profile:

```text
API Version: API V2
Webhook URL: https://YOUR-APP.up.railway.app/inbound/sms
```

## 6) Test in Telegram

```text
/help
/mynumbers
/syncsms 20
/latest
/testalert
```

## 7) Bulk Telnyx key audits

If you get a new batch of Telnyx API keys, upload a plain `.txt` file with one full copied key per line, then run:

```bash
python tools/audit_telnyx_keys.py telnyx_keys_input.txt
```

Outputs:

```text
telnyx_valid_with_numbers.csv
telnyx_valid_no_numbers.csv
telnyx_invalid_keys.csv
telnyx_audit_summary.txt
railway_values_generated.env
```

## Security

- Do not commit `.env`
- Rotate/revoke temporary GitHub tokens after use
- Add `TELNYX_PUBLIC_KEY` / `TELNYX_EXTRA_PUBLIC_KEYS` later for stronger webhook verification
