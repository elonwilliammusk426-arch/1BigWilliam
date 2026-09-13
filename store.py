from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

DB_PATH = 'inbound.db'


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
            '''
            CREATE TABLE IF NOT EXISTS inbound (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                to_number TEXT NOT NULL,
                from_number TEXT NOT NULL,
                body TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            '''
        )
        conn.execute('CREATE INDEX IF NOT EXISTS idx_to_number ON inbound(to_number)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_received_at ON inbound(received_at)')


def save_message(to_number: str, from_number: str, body: str, path: str = DB_PATH) -> int:
    init_db(path)
    received_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            'INSERT INTO inbound (to_number, from_number, body, received_at) VALUES (?, ?, ?, ?)',
            (to_number, from_number, body, received_at),
        )
        return int(cur.lastrowid)


def _row_to_message(row: sqlite3.Row) -> InboundMessage:
    return InboundMessage(
        id=row['id'],
        to_number=row['to_number'],
        from_number=row['from_number'],
        body=row['body'],
        received_at=row['received_at'],
    )


def recent_all(limit: int = 10, path: str = DB_PATH) -> list[InboundMessage]:
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM inbound ORDER BY id DESC LIMIT ?', (max(1, min(int(limit), 100)),)).fetchall()
    return [_row_to_message(r) for r in rows]


def recent_for_number(to_number: str, limit: int = 20, path: str = DB_PATH) -> list[InboundMessage]:
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT * FROM inbound WHERE to_number = ? ORDER BY id DESC LIMIT ?',
            (to_number, max(1, min(int(limit), 100))),
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def distinct_to_numbers(path: str = DB_PATH) -> list[str]:
    init_db(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT DISTINCT to_number FROM inbound ORDER BY to_number').fetchall()
    return [str(r['to_number']) for r in rows]
