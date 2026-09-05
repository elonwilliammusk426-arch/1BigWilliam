# Using 7–10 Telnyx accounts in one Railway app

The app now supports multiple Telnyx accounts in the same Railway deployment.
You do **not** need a separate Railway app for every account.

## The idea

```text
Telnyx account 1 -> number 1 --\
Telnyx account 2 -> number 2 ----> same Railway app -> same Telegram group
Telnyx account 3 -> number 3 --/
```

## Required in every Telnyx account

For each Telnyx account:

1. Buy/rent an SMS-capable number.
2. Create/open a Messaging Profile.
3. Set:

```text
API Version: API V2
Webhook URL: https://YOUR-APP.up.railway.app/inbound/sms
Webhook Failover URL: blank
```

If you are using the already-running 1BigWilliam Railway app, that webhook URL is:

```text
https://1bigwilliam.up.railway.app/inbound/sms
```

4. Assign that account's number to that Messaging Profile.

## Railway variables

Use the ready template file:

```text
RAILWAY_VARIABLES_TEMPLATE.env
```

### Example with 4 Telnyx accounts

```env
TELNYX_API_KEY=KEY_ACCOUNT_01_REAL_VALUE
TELNYX_EXTRA_API_KEYS=KEY_ACCOUNT_02_REAL_VALUE,KEY_ACCOUNT_03_REAL_VALUE,KEY_ACCOUNT_04_REAL_VALUE

TELNYX_FROM_NUMBER=+15306908868
TELNYX_NUMBERS=+15306908868,+1ACCOUNT2NUMBER,+1ACCOUNT3NUMBER,+1ACCOUNT4NUMBER
```

### Example with 10 Telnyx accounts

```env
TELNYX_API_KEY=KEY_ACCOUNT_01_REAL_VALUE
TELNYX_EXTRA_API_KEYS=KEY_ACCOUNT_02_REAL_VALUE,KEY_ACCOUNT_03_REAL_VALUE,KEY_ACCOUNT_04_REAL_VALUE,KEY_ACCOUNT_05_REAL_VALUE,KEY_ACCOUNT_06_REAL_VALUE,KEY_ACCOUNT_07_REAL_VALUE,KEY_ACCOUNT_08_REAL_VALUE,KEY_ACCOUNT_09_REAL_VALUE,KEY_ACCOUNT_10_REAL_VALUE

TELNYX_NUMBERS=+1NUMBER1,+1NUMBER2,+1NUMBER3,+1NUMBER4,+1NUMBER5,+1NUMBER6,+1NUMBER7,+1NUMBER8,+1NUMBER9,+1NUMBER10
```

No spaces after commas.

## Public keys

Public keys are optional while testing.

Easy setup:

```env
TELNYX_PUBLIC_KEY=
TELNYX_EXTRA_PUBLIC_KEYS=
```

More secure setup later:

```env
TELNYX_PUBLIC_KEY=PUBLIC_KEY_ACCOUNT_01
TELNYX_EXTRA_PUBLIC_KEYS=PUBLIC_KEY_ACCOUNT_02,PUBLIC_KEY_ACCOUNT_03,PUBLIC_KEY_ACCOUNT_04
```

## Polling fallback

Keep polling enabled, especially with multiple accounts:

```env
TELNYX_POLLING_ENABLED=true
TELNYX_POLL_INTERVAL_SECONDS=300
TELNYX_POLL_LIMIT=10
TELNYX_SYNC_DATE_RANGE=
```

Leave `TELNYX_SYNC_DATE_RANGE` blank so it can pull recent SMS across UTC date boundaries.

## Test commands

After Railway redeploys, use Telegram:

```text
/mynumbers
/syncsms 20
/latest
/recent +15306908868
```

With multiple accounts, `/syncsms` should say something like:

```text
Sync complete. Accounts: 4, checked: ..., stored new: ..., skipped: ...
```

If one API key is wrong, the command will still check the other accounts and show an error for the bad one.
