"""Centralised configuration loaded from environment / .env file.

Current provider: Telnyx. The provider-specific code is isolated in
`telnyx.py` and `telnyx_webhook.py`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # Telnyx SMS provider
    telnyx_api_key: str
    telnyx_base_url: str
    telnyx_from_number: str
    telnyx_public_key: str

    # OTP settings
    otp_length: int
    otp_ttl_seconds: int

    # Telegram inbox / alerts
    telegram_bot_token: str
    owner_telegram_id: str
    telegram_alert_chat_id: str

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and (self.telegram_alert_chat_id or self.owner_telegram_id))


def load_config(env_file: str = ".env") -> Config:
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    except ImportError:
        pass

    api_key = os.getenv("TELNYX_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "Missing required environment variable: TELNYX_API_KEY\n"
            "Copy .env.example to .env and fill it in."
        )

    return Config(
        telnyx_api_key=api_key,
        telnyx_base_url=os.getenv("TELNYX_BASE_URL", "https://api.telnyx.com/v2"),
        telnyx_from_number=os.getenv("TELNYX_FROM_NUMBER", ""),
        telnyx_public_key=os.getenv("TELNYX_PUBLIC_KEY", ""),
        otp_length=int(os.getenv("OTP_LENGTH", "6")),
        otp_ttl_seconds=int(os.getenv("OTP_TTL_SECONDS", "300")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        owner_telegram_id=os.getenv("OWNER_TELEGRAM_ID", ""),
        telegram_alert_chat_id=os.getenv(
            "TELEGRAM_ALERT_CHAT_ID", os.getenv("OWNER_TELEGRAM_ID", "")
        ),
    )
