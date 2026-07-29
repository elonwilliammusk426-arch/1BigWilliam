# SMS Toolkit (Python + Telnyx)

A compliant SMS toolkit for numbers **you own/control through Telnyx**.

It covers the selected use cases:

1. **Outbound SMS** — send from your Telnyx number to a recipient.
2. **OTP / verification for your own app/testing** — generate, send, and verify one-time codes.
3. **Bulk campaigns** — CSV-based send with an explicit opt-in confirmation guard.
4. **Inbound inbox** — receive SMS on your Telnyx numbers and read them in Telegram.

This project is **not** a public activation-number rental service. It is built for your own numbers and your own inbox.

---

## Important boundary

You said you want numbers so you can verify things without exposing your personal SIM. That is okay when the account/use is yours and the service allows it.

This toolkit will not help you:

- rent/share numbers to third parties;
- bypass another platform's phone-verification rules;
- evade bans/rate limits;
- build a public OTP-harvesting group/bot.

Also: many platforms block VoIP/cloud numbers. If a platform refuses a Telnyx number, the compliant answer is to use a verification method they accept — not to bypass their controls.

---

## Project layout

| File | Responsibility |
|---|---|
| `config.py` | Loads `.env` settings into a typed config |
| `telnyx.py` | Telnyx outbound REST client |
| `telnyx_webhook.py` | Flask webhook receiver for Telnyx inbound SMS |
| `store.py` | SQLite inbound message store (`inbound.db`) |
| `notify.py` | Telegram push alert when SMS arrives |
| `bot.py` | Owner-only Telegram bot with `/recent` and `/numbers` |
| `otp.py` | OTP generation, send, verify, expiry; persists to `otp.db` |
| `cli.py` | CLI for send, OTP, and bulk CSV campaigns |

Marchex and Twilio provider files were removed from the active project.

---

## 1. Install

```bash
cd sms_project
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. Telnyx setup

1. Create/login to Telnyx.
2. Buy/rent an SMS-enabled number that you control.
3. Create a Messaging Profile.
4. Assign your number to that Messaging Profile.
5. Set the Messaging Profile webhook URL to:

```text
https://YOUR-DOMAIN.example/inbound/sms
```

For local testing, run the webhook locally and expose it with a tunnel:

```bash
python telnyx_webhook.py
ngrok http 5000
# then set https://<ngrok-id>.ngrok-free.app/inbound/sms in Telnyx
```

For production, set `TELNYX_PUBLIC_KEY` so webhook signatures are verified.

---

## 3. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Minimum values:

```env
TELNYX_API_KEY=KEYxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELNYX_FROM_NUMBER=+12015550123
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OWNER_TELEGRAM_ID=000000000
TELEGRAM_ALERT_CHAT_ID=-1001234567890
```

Use `OWNER_TELEGRAM_ID` for your personal Telegram user id. Use `TELEGRAM_ALERT_CHAT_ID` for the private group where inbound SMS alerts should drop. If `TELEGRAM_ALERT_CHAT_ID` is blank, alerts go to your private chat instead.

Recommended for production:

```env
TELNYX_PUBLIC_KEY=your_telnyx_webhook_public_key_here
TELNYX_SIGNATURE_TOLERANCE=300
```

---

## 4. Outbound SMS

```bash
python cli.py send --to +12015550123 --body "Hello from my Telnyx number"
```

Or override the sender:

```bash
python cli.py send --from +12015550000 --to +12015550123 --body "Hello"
```

---

## 5. OTP / verification for your own app or testing

Send a code:

```bash
python cli.py otp-send --to +12015550123
```

Verify the code:

```bash
python cli.py otp-verify --to +12015550123 --code 123456
```

The OTP store is SQLite (`otp.db`), so verification works across separate CLI runs. Codes are single-use and expire after `OTP_TTL_SECONDS`.

---

## 6. Bulk campaigns

Create a CSV like this:

```csv
phone,name
+12015550123,Ada
+12015550124,Grace
```

Dry run first:

```bash
python cli.py bulk-send --csv recipients.csv --body "Service update" --dry-run
```

Real send requires explicit opt-in confirmation:

```bash
python cli.py bulk-send \
  --csv recipients.csv \
  --body "Service update" \
  --confirm-opt-in
```

Notes:

- Only text people who consented.
- Include opt-out language where required.
- Follow Telnyx/carrier registration requirements for your traffic type and country.
- Increase `--delay` for slower sending.

---

## 7. Create Telegram bot + private alert group

1. In Telegram, open **@BotFather**.
2. Send `/newbot`.
3. Choose a display name, for example `My SMS Inbox`.
4. Choose a username ending in `bot`, for example `my_sms_inbox_123_bot`.
5. Copy the token BotFather gives you into `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

6. Create a **private** Telegram group, for example `SMS Inbox Alerts`.
7. Add your bot to that group.
8. Send a test message in the group, for example `/id`.
9. Also open a private chat with your bot and send `/start`.
10. Run:

```bash
python telegram_chat_id.py
```

It will print IDs. Put them in `.env`:

```env
OWNER_TELEGRAM_ID=your_personal_user_id
TELEGRAM_ALERT_CHAT_ID=your_private_group_chat_id
```

Group chat IDs are often negative and may start with `-100...`.

### Inbound SMS → Telegram group

Run the receiver:

```bash
python telnyx_webhook.py
```

Run the Telegram command bot in another terminal:

```bash
python bot.py
```

Telegram commands, owner-only:

```text
/start
/help
/latest [limit]
/recent +12015550123 [limit]
/numbers
/mynumbers
/available [country] [area_code] [limit]
/checknum +12015550123
/testalert
/whoami
/chatid
```

Examples:

```text
/available
/available 732
/available US 732 10
/available US any 20
/checknum +15306908868
```

`/available` searches Telnyx inventory for SMS-capable local numbers. It does **not** buy/order the number.

`/checknum` uses Numverify to validate a number and show metadata like country, carrier, and line type. It requires `NUMVERIFY_API_KEY`. It does **not** provide numbers or receive SMS.

Inbound SMS alerts drop into `TELEGRAM_ALERT_CHAT_ID`. Bot commands remain locked to `OWNER_TELEGRAM_ID`.

---

## 8. Test the webhook locally without Telnyx

If `TELNYX_PUBLIC_KEY` is blank, the webhook accepts unsigned test requests:

```bash
curl -X POST http://localhost:5000/inbound/sms \
  -H "Content-Type: application/json" \
  -d '{
    "data": {
      "event_type": "message.received",
      "payload": {
        "id": "test-message-id",
        "from": {"phone_number": "+12015550199"},
        "to": [{"phone_number": "+12015550123"}],
        "text": "Your test code is 123456",
        "type": "SMS"
      }
    }
  }'
```

Then in Telegram:

```text
/recent +12015550123
```

---

## 9. Security / production checklist

- Set `TELNYX_PUBLIC_KEY` and keep signature verification enabled.
- Use HTTPS for the webhook.
- Keep `.env` secret.
- Do not expose this Telegram bot publicly; keep `OWNER_TELEGRAM_ID` set.
- Back up `inbound.db` if messages matter.
- For high-volume OTP, replace SQLite with Redis/Postgres and add rate limits.
