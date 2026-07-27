"""One-time-passcode (OTP) helper for your own app/testing flows.

Generates a numeric code, persists it in SQLite with a TTL, and sends it via
Telnyx to a recipient number. This is intentionally generic: it is for your
own customers, app testing, or accounts you control — not for selling/renting
verification numbers to third parties.
"""
from __future__ import annotations

import secrets
import sqlite3
import time

from config import Config
from telnyx import SendResult, TelnyxClient

OTP_DB_PATH = "otp.db"


class OTPService:
    def __init__(self, config: Config, client: TelnyxClient, db_path: str = OTP_DB_PATH):
        self.config = config
        self.client = client
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS otp_codes (
                    to_number   TEXT PRIMARY KEY,
                    code        TEXT NOT NULL,
                    expires_at  REAL NOT NULL,
                    created_at  REAL NOT NULL
                )
                """
            )

    def _generate_code(self) -> str:
        return "".join(
            secrets.choice("0123456789") for _ in range(self.config.otp_length)
        )

    def send(self, from_number: str, to_number: str) -> SendResult:
        code = self._generate_code()
        now = time.time()
        expires_at = now + self.config.otp_ttl_seconds

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO otp_codes (to_number, code, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(to_number) DO UPDATE SET
                    code = excluded.code,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at
                """,
                (to_number, code, expires_at, now),
            )

        body = (
            f"Your verification code is {code}. "
            f"It expires in {max(1, self.config.otp_ttl_seconds // 60)} minutes."
        )
        return self.client.send_message(from_number, to_number, body)

    def verify(self, to_number: str, code: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT code, expires_at FROM otp_codes WHERE to_number = ?",
                (to_number,),
            ).fetchone()

            if not row:
                return False

            expected_code, expires_at = row
            if time.time() > float(expires_at):
                conn.execute("DELETE FROM otp_codes WHERE to_number = ?", (to_number,))
                return False

            if secrets.compare_digest(str(expected_code), str(code)):
                conn.execute("DELETE FROM otp_codes WHERE to_number = ?", (to_number,))
                return True

        return False

    def purge_expired(self) -> int:
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM otp_codes WHERE expires_at < ?", (now,))
            return int(cur.rowcount)
