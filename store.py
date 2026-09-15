from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
                received_at TEXT NOT NULL,
                provider_message_id TEXT
            )
            '''
        )
        cols = {r[1] for r in conn.execute('PRAGMA table_info(inbound)').fetchall()}
        if 'provider_message_id' not in cols:
            conn.execute('ALTER TABLE inbound ADD COLUMN provider_message_id TEXT')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_to_number ON inbound(to_number)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_received_at ON inbound(received_at)')
        conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_message_id '
            'ON inbound(provider_message_id) WHERE provider_message_id IS NOT NULL'
        )


def has_provider_message(provider_message_id: str, path: str = DB_PATH) -> bool:
    init_db(path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            'SELECT id FROM inbound WHERE provider_message_id = ? LIMIT 1',
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
                'INSERT OR IGNORE INTO inbound (to_number, from_number, body, received_at, provider_message_id) '
                'VALUES (?, ?, ?, ?, ?)',
                (to_number, from_number, body, received_at, provider_message_id),
            )
            if cur.rowcount == 0:
                row = conn.execute(
                    'SELECT id FROM inbound WHERE provider_message_id = ? LIMIT 1',
                    (provider_message_id,),
                ).fetchone()
                return int(row[0]) if row else 0
            return int(cur.lastrowid)

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


def _cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).isoformat()


def prune_old_messages(hours: int = 48, path: str = DB_PATH) -> int:
    init_db(path)
    cutoff = _cutoff_iso(hours)
    with sqlite3.connect(path) as conn:
        cur = conn.execute('DELETE FROM inbound WHERE received_at < ?', (cutoff,))
        return int(cur.rowcount or 0)


def recent_all(limit: int = 10, path: str = DB_PATH, max_age_hours: int | None = None) -> list[InboundMessage]:
    init_db(path)
    query = 'SELECT * FROM inbound'
    params: list[object] = []
    if max_age_hours:
        query += ' WHERE received_at >= ?'
        params.append(_cutoff_iso(max_age_hours))
    query += ' ORDER BY id DESC LIMIT ?'
    params.append(max(1, min(int(limit), 100)))
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [_row_to_message(r) for r in rows]


def recent_for_number(
    to_number: str,
    limit: int = 20,
    path: str = DB_PATH,
    max_age_hours: int | None = None,
) -> list[InboundMessage]:
    init_db(path)
    query = 'SELECT * FROM inbound WHERE to_number = ?'
    params: list[object] = [to_number]
    if max_age_hours:
        query += ' AND received_at >= ?'
        params.append(_cutoff_iso(max_age_hours))
    query += ' ORDER BY id DESC LIMIT ?'
    params.append(max(1, min(int(limit), 100)))
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [_row_to_message(r) for r in rows]


def distinct_to_numbers(path: str = DB_PATH, max_age_hours: int | None = None) -> list[str]:
    init_db(path)
    query = 'SELECT DISTINCT to_number FROM inbound'
    params: list[object] = []
    if max_age_hours:
        query += ' WHERE received_at >= ?'
        params.append(_cutoff_iso(max_age_hours))
    query += ' ORDER BY to_number'
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [str(r['to_number']) for r in rows]
