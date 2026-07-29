"""SQLite store for inbound SMS received on YOUR OWN numbers.

Keyed by the destination number (the number you own that received the SMS),
so you can later query "what arrived on +1XXX?".
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

DB_PATH = "inbound.db"


@dataclass
class InboundMessage:
    id: int
    to_number: str
    from_number: str
    body: str
    received_at: str


def init_db(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                to_number    TEXT NOT NULL,
                from_number  TEXT NOT NULL,
                body         TEXT NOT NULL,
                received_at  TEXT NOT NULL,
                provider_message_id TEXT
            )
            """
        )
        # Migration for older DBs created before provider_message_id existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(inbound)").fetchall()}
        if "provider_message_id" not in cols:
            conn.execute("ALTER TABLE inbound ADD COLUMN provider_message_id TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_to ON inbound(to_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_inbound_id ON inbound(id)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_message "
            "ON inbound(provider_message_id) WHERE provider_message_id IS NOT NULL"
        )


def _row_to_message(r: sqlite3.Row) -> InboundMessage:
    return InboundMessage(
        id=r["id"],
        to_number=r["to_number"],
        from_number=r["from_number"],
        body=r["body"],
        received_at=r["received_at"],
    )


def has_provider_message(provider_message_id: str, path: str = DB_PATH) -> bool:
    init_db(path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT id FROM inbound WHERE provider_message_id = ? LIMIT 1",
            (provider_message_id,),
        ).fetchone()
    return row is not None


def save_message(
    to_number: str,
    from_number: str,
    body: str,
    path: str = DB_PATH,
    provider_message_id: str | None = None,
) -> int:
    init_db(path)
    received_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        if provider_message_id:
            cur = conn.execute(
                "INSERT OR IGNORE INTO inbound "
                "(to_number, from_number, body, received_at, provider_message_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (to_number, from_number, body, received_at, provider_message_id),
            )
            if cur.rowcount == 0:
                row = conn.execute(
                    "SELECT id FROM inbound WHERE provider_message_id = ?",
                    (provider_message_id,),
                ).fetchone()
                return int(row[0]) if row else 0
            return int(cur.lastrowid)

        cur = conn.execute(
            "INSERT INTO inbound (to_number, from_number, body, received_at) "
            "VALUES (?, ?, ?, ?)",
            (to_number, from_number, body, received_at),
        )
        return int(cur.lastrowid)


def recent_for_number(to_number: str, limit: int = 20, path: str = DB_PATH):
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM inbound WHERE to_number = ? ORDER BY id DESC LIMIT ?",
            (to_number, limit),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def recent_all(limit: int = 10, path: str = DB_PATH):
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM inbound ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def distinct_to_numbers(path: str = DB_PATH) -> list[str]:
    """Numbers in this inbox that have received at least one message."""
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT to_number FROM inbound ORDER BY to_number"
        ).fetchall()
    return [r["to_number"] for r in rows]
