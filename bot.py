"""Telegram bot (OWNER-ONLY) to view SMS received on your own numbers.

Security: SMS-reading commands check the caller's Telegram user id against
OWNER_TELEGRAM_ID. Alerts can still be delivered to TELEGRAM_ALERT_CHAT_ID.

Commands:
    /start                      - intro
    /help                       - command list
    /latest [limit]             - show latest inbound SMS across all numbers
    /recent +14155550123 [limit]- show messages received on a number
    /numbers                    - list numbers that have received messages
    /mynumbers                  - list owned/configured inbox numbers
    /available [country] [area] [limit] - search SMS-capable Telnyx numbers
    /checknum +14155550123    - validate carrier/line type using Numverify
    /syncsms [limit]          - pull missed SMS from Telnyx
    /testalert                  - send a test alert to TELEGRAM_ALERT_CHAT_ID
    /whoami                     - show your Telegram user id
    /chatid                     - show the current chat/group id
"""
from __future__ import annotations

import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

import notify
import store
from config import load_config
from numverify import NumverifyError, format_numverify_result, validate_number
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telnyx import TelnyxClient
from telnyx_sync import list_all_owned_numbers, sync_inbound_once

OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0") or "0")
MAX_TELEGRAM_MESSAGE = 3900


HELP_TEXT = (
    "📩 1BigWilliam SMS Inbox\n"
    "Owner-only control panel.\n\n"
    "Inbox:\n"
    "• /latest [limit] — latest messages\n"
    "• /recent <number> [limit] — messages for one number\n"
    "• /numbers — numbers that received SMS\n"
    "• /mynumbers — configured/owned numbers\n\n"
    "Tools:\n"
    "• /available [country] [area] [limit] — search Telnyx numbers\n"
    "• /checknum <number> — validate carrier/line type\n"
    "• /syncsms [limit] — pull missed SMS from Telnyx\n"
    "• /testalert — test Telegram alert\n\n"
    "Setup:\n"
    "• /whoami — your Telegram user id\n"
    "• /chatid — this chat/group id\n\n"
    "Examples:\n"
    "/recent +15306908868 10\n"
    "/available US any 5\n"
    "/checknum +15306908868"
)


async def _authorized(update: Update) -> bool:
    user = update.effective_user
    message = update.effective_message
    user_id = user.id if user else 0
    if user_id != OWNER_ID:
        if message:
            await message.reply_text("🚫 Unauthorized.")
        return False
    return True


def _parse_limit(value: str | None, default: int, maximum: int = 50) -> int:
    if not value:
        return default
    try:
        limit = int(value)
    except ValueError:
        return default
    return max(1, min(limit, maximum))


def _format_messages(messages: list[store.InboundMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        lines.append(
            f"#{m.id} [{m.received_at}]\n"
            f"To: {m.to_number}\n"
            f"From: {m.from_number}\n"
            f"{m.body}\n"
        )
    text = "\n".join(lines).strip()
    if len(text) > MAX_TELEGRAM_MESSAGE:
        return text[:MAX_TELEGRAM_MESSAGE] + "\n...truncated"
    return text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    await update.effective_message.reply_text(HELP_TEXT)


async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    limit = _parse_limit(context.args[0] if context.args else None, default=10)
    messages = store.recent_all(limit=limit)
    if not messages:
        await update.effective_message.reply_text("No inbound messages yet.")
        return
    await update.effective_message.reply_text(_format_messages(messages))


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /recent +14155550123 [limit]")
        return
    number = context.args[0]
    limit = _parse_limit(context.args[1] if len(context.args) > 1 else None, default=20)
    messages = store.recent_for_number(number, limit=limit)
    if not messages:
        await update.effective_message.reply_text(f"No messages for {number}.")
        return
    await update.effective_message.reply_text(_format_messages(messages))


async def numbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    numbers = store.distinct_to_numbers()
    if not numbers:
        await update.effective_message.reply_text("No numbers with messages yet.")
        return
    await update.effective_message.reply_text(
        "Numbers with inbound SMS:\n" + "\n".join(numbers)
    )


def _configured_numbers() -> list[str]:
    raw_values = [os.getenv("TELNYX_FROM_NUMBER", ""), os.getenv("TELNYX_NUMBERS", "")]
    numbers: list[str] = []
    for raw in raw_values:
        for part in raw.replace(";", ",").split(","):
            number = part.strip()
            if number and number not in numbers:
                numbers.append(number)
    return numbers


def _owned_telnyx_numbers() -> tuple[list[str], str | None]:
    numbers, errors = list_all_owned_numbers()
    return numbers, "; ".join(errors) if errors else None


def _format_my_numbers() -> str:
    configured = _configured_numbers()
    seen = store.distinct_to_numbers()
    owned, api_error = _owned_telnyx_numbers()

    merged: list[str] = []
    for number in [*owned, *configured, *seen]:
        if number and number not in merged:
            merged.append(number)

    if not merged:
        text = "No numbers found yet. Add Telnyx numbers to the same Messaging Profile."
    else:
        lines = ["Your inbox numbers:"]
        for number in merged:
            tags = []
            if number in owned:
                tags.append("Telnyx")
            if number in configured:
                tags.append("configured")
            if number in seen:
                tags.append("received SMS")
            suffix = f" ({', '.join(tags)})" if tags else ""
            lines.append(f"• {number}{suffix}")
        text = "\n".join(lines)

    if api_error:
        text += "\n\nNote: Could not fetch Telnyx account numbers. Showing configured/seen numbers."
    return text[:MAX_TELEGRAM_MESSAGE]


async def mynumbers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    await update.effective_message.reply_text(_format_my_numbers())


def _parse_available_args(args: list[str]) -> tuple[str, str | None, int]:
    """Parse /available [country] [area_code|any] [limit].

    Friendly shortcuts:
      /available            -> US, any area, 10
      /available 732        -> US, area 732, 10
      /available US 732 20  -> US, area 732, 20
      /available US any 20  -> US, any area, 20
    """
    country = "US"
    area_code: str | None = None
    limit = 10

    if not args:
        return country, area_code, limit

    first = args[0].strip()
    if first.isdigit() and len(first) <= 4:
        area_code = first
        if len(args) > 1:
            limit = _parse_limit(args[1], default=10)
        return country, area_code, limit

    country = first.upper()
    if len(args) > 1:
        second = args[1].strip()
        if second.lower() not in {"any", "none", "-"}:
            area_code = second
    if len(args) > 2:
        limit = _parse_limit(args[2], default=10)
    return country, area_code, limit


def _format_available_numbers(numbers: list[dict], country: str, area_code: str | None) -> str:
    if not numbers:
        area = f" area {area_code}" if area_code else ""
        return f"No SMS-capable Telnyx numbers found for {country}{area}."

    lines = ["Available SMS-capable Telnyx numbers:"]
    for item in numbers:
        phone = item.get("phone_number", "-")
        cost = item.get("cost_information") or {}
        monthly = cost.get("monthly_cost")
        upfront = cost.get("upfront_cost")
        currency = cost.get("currency") or ""
        regions = item.get("region_information") or []
        region_names = [r.get("region_name") for r in regions if r.get("region_name")]
        region_text = ", ".join(region_names[:3])
        cost_parts = []
        if monthly is not None:
            cost_parts.append(f"monthly {monthly} {currency}".strip())
        if upfront is not None:
            cost_parts.append(f"upfront {upfront} {currency}".strip())
        cost_text = " | " + "; ".join(cost_parts) if cost_parts else ""
        region_suffix = f" | {region_text}" if region_text else ""
        lines.append(f"• {phone}{region_suffix}{cost_text}")

    text = "\n".join(lines)
    if len(text) > MAX_TELEGRAM_MESSAGE:
        return text[:MAX_TELEGRAM_MESSAGE] + "\n...truncated"
    return text


async def available(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return

    country, area_code, limit = _parse_available_args(context.args)
    await update.effective_message.reply_text(
        f"Searching Telnyx for SMS-capable numbers: country={country}, "
        f"area={area_code or 'any'}, limit={limit}..."
    )

    try:
        cfg = load_config()
        client = TelnyxClient(cfg.telnyx_api_key, cfg.telnyx_base_url)
        numbers = client.search_available_numbers(
            country_code=country,
            area_code=area_code,
            limit=limit,
            phone_number_type="local",
            features="sms",
        )
    except Exception as exc:
        await update.effective_message.reply_text(
            "Could not search available numbers. Check TELNYX_API_KEY in .env.\n"
            f"Error: {exc}"
        )
        return

    await update.effective_message.reply_text(
        _format_available_numbers(numbers, country, area_code)
    )


async def checknum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /checknum +15306908868")
        return
    try:
        data = validate_number(context.args[0])
        await update.effective_message.reply_text(format_numverify_result(data))
    except NumverifyError as exc:
        await update.effective_message.reply_text(
            "Could not check number. Add NUMVERIFY_API_KEY in .env/Railway Variables.\n"
            f"Error: {exc}"
        )


async def syncsms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    limit = _parse_limit(context.args[0] if context.args else None, default=20)
    try:
        res = sync_inbound_once(limit=limit, notify_new=True)
        await update.effective_message.reply_text(
            f"Sync complete. Accounts: {res.accounts}, checked: {res.checked}, stored new: {res.stored}, skipped: {res.skipped}."
            + (f"\nErrors: {'; '.join(res.errors[:3])}" if res.errors else "")
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Sync failed: {exc}")


async def testalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorized(update):
        return
    notify.notify_owner("✅ Telegram group alert test successful.")
    await update.effective_message.reply_text("Sent a test alert to TELEGRAM_ALERT_CHAT_ID.")


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Setup helper; intentionally not owner-locked so you can find your id."""
    user = update.effective_user
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"Your Telegram user id: {user.id if user else '-'}\n"
        f"This chat id: {chat.id if chat else '-'}\n"
        f"Chat type: {chat.type if chat else '-'}"
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Setup helper; intentionally not owner-locked so group id is easy to find."""
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"This chat id: {chat.id if chat else '-'}\n"
        f"Chat type: {chat.type if chat else '-'}"
    )


def main() -> None:
    store.init_db()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in your environment/.env")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("latest", latest))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("numbers", numbers))
    app.add_handler(CommandHandler("mynumbers", mynumbers))
    app.add_handler(CommandHandler("available", available))
    app.add_handler(CommandHandler("checknum", checknum))
    app.add_handler(CommandHandler("syncsms", syncsms))
    app.add_handler(CommandHandler("testalert", testalert))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("chatid", chatid))
    app.run_polling()


if __name__ == "__main__":
    main()
