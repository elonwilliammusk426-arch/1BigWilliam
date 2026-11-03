from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv('INBOUND_DB_PATH', '').strip() or 'inbound.db'
INCOMPLETE_BODIES = {'', '(inbound SMS text unavailable from Telnyx API)'}


@dataclass
class InboundMessage:
    id: int
    to_number: str
    from_number: str
    body: str
    received_at: str
    provider_message_id: str | None = None


@dataclass(frozen=True)
class NotificationClaim:
    message: InboundMessage
    token: str


def init_db(path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS inbound (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                to_number TEXT NOT NULL,
                from_number TEXT NOT NULL,
                body TEXT NOT NULL,
                received_at TEXT NOT NULL,
                provider_message_id TEXT,
                notification_state TEXT NOT NULL DEFAULT 'sent',
                notification_claim_token TEXT,
                notification_claimed_at TEXT,
                notification_retry_at TEXT,
                notification_attempts INTEGER NOT NULL DEFAULT 0
            )
            '''
        )
        cols = {r[1] for r in conn.execute('PRAGMA table_info(inbound)').fetchall()}
        if 'provider_message_id' not in cols:
            conn.execute('ALTER TABLE inbound ADD COLUMN provider_message_id TEXT')
        # Existing messages predate delivery tracking. Mark them as sent rather
        # than replaying potentially stale codes after an upgrade.
        if 'notification_state' not in cols:
            conn.execute("ALTER TABLE inbound ADD COLUMN notification_state TEXT NOT NULL DEFAULT 'sent'")
        if 'notification_claim_token' not in cols:
            conn.execute('ALTER TABLE inbound ADD COLUMN notification_claim_token TEXT')
        if 'notification_claimed_at' not in cols:
            conn.execute('ALTER TABLE inbound ADD COLUMN notification_claimed_at TEXT')
        if 'notification_retry_at' not in cols:
            conn.execute('ALTER TABLE inbound ADD COLUMN notification_retry_at TEXT')
        if 'notification_attempts' not in cols:
            conn.execute('ALTER TABLE inbound ADD COLUMN notification_attempts INTEGER NOT NULL DEFAULT 0')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_to_number ON inbound(to_number)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_received_at ON inbound(received_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_pending_notifications ON inbound(notification_state, notification_claimed_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_notification_retry ON inbound(notification_state, notification_retry_at)')
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


def _find_telnyx_message(conn: sqlite3.Connection, message_id: str) -> tuple | sqlite3.Row | None:
    provider_key = f'telnyx:{message_id}'
    legacy_suffix = f':{message_id}'
    return conn.execute(
        "SELECT id, body, notification_state FROM inbound WHERE provider_message_id = ? OR "
        "(substr(provider_message_id, 1, 11) = 'telnyx:acct' "
        "AND substr(provider_message_id, -length(?)) = ?) LIMIT 1",
        (provider_key, legacy_suffix, legacy_suffix),
    ).fetchone()


def has_telnyx_message(message_id: str, path: str = DB_PATH) -> bool:
    init_db(path)
    with sqlite3.connect(path) as conn:
        return _find_telnyx_message(conn, message_id) is not None


def has_complete_telnyx_message(message_id: str, path: str = DB_PATH) -> bool:
    init_db(path)
    with sqlite3.connect(path) as conn:
        row = _find_telnyx_message(conn, message_id)
        return bool(row and row[1] not in INCOMPLETE_BODIES)


def save_telnyx_message(
    to_number: str,
    from_number: str,
    body: str,
    *,
    message_id: str | None,
    notify: bool = True,
    received_at: datetime | None = None,
    path: str = DB_PATH,
) -> tuple[int, bool]:
    """Store one Telnyx message and queue its alert in the same transaction.

    The provider's message UUID is shared by webhook and sync paths. Older
    releases used an account/key-dependent prefix; recognize it to avoid
    replaying already-synced messages after an upgrade.
    """
    init_db(path)
    message_id = str(message_id or '').strip()
    provider_key = f'telnyx:{message_id}' if message_id else None
    timestamp = received_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp_text = timestamp.astimezone(timezone.utc).isoformat()

    with sqlite3.connect(path) as conn:
        # Serialize the lookup/insert across the webhook and polling threads.
        conn.execute('BEGIN IMMEDIATE')
        if provider_key:
            row = _find_telnyx_message(conn, message_id)
            if row:
                if row[1] in INCOMPLETE_BODIES and body not in INCOMPLETE_BODIES:
                    # A webhook or older sync may have supplied no text. If a
                    # later lookup has content, update and deliver it instead
                    # of treating the incomplete record as permanently handled.
                    state = 'pending' if notify else row[2]
                    conn.execute(
                        'UPDATE inbound SET to_number = ?, from_number = ?, body = ?, '
                        'notification_state = ?, notification_claim_token = NULL, '
                        'notification_claimed_at = NULL, notification_retry_at = NULL, '
                        'notification_attempts = 0 WHERE id = ?',
                        (to_number, from_number, body, state, row[0]),
                    )
                    return int(row[0]), True
                return int(row[0]), False
        cur = conn.execute(
            'INSERT INTO inbound (to_number, from_number, body, received_at, '
            'provider_message_id, notification_state) VALUES (?, ?, ?, ?, ?, ?)',
            (to_number, from_number, body, timestamp_text, provider_key,
             'pending' if notify else 'sent'),
        )
        return int(cur.lastrowid), True


def claim_next_notification(path: str = DB_PATH, lease_seconds: int = 120) -> NotificationClaim | None:
    """Atomically lease a pending alert, including a lease abandoned by a restart."""
    init_db(path)
    now = datetime.now(timezone.utc)
    expired_before = (now - timedelta(seconds=lease_seconds)).isoformat()
    token = uuid.uuid4().hex
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            "SELECT * FROM inbound WHERE notification_state = 'pending' AND "
            '(notification_claimed_at IS NULL OR notification_claimed_at < ?) AND '
            '(notification_retry_at IS NULL OR notification_retry_at <= ?) '
            'ORDER BY CASE WHEN notification_attempts = 0 THEN 0 ELSE 1 END, id LIMIT 1',
            (expired_before, now.isoformat()),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            'UPDATE inbound SET notification_claim_token = ?, notification_claimed_at = ? WHERE id = ?',
            (token, now.isoformat(), row['id']),
        )
        return NotificationClaim(_row_to_message(row), token)


def finish_notification(
    claim: NotificationClaim, *, delivered: bool, path: str = DB_PATH, retry_seconds: int = 30,
) -> bool:
    """Complete a lease or schedule a persisted, per-message retry."""
    retry_at = None if delivered else (
        datetime.now(timezone.utc) + timedelta(seconds=max(0, retry_seconds))
    ).isoformat()
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            'UPDATE inbound SET notification_state = ?, notification_claim_token = NULL, '
            'notification_claimed_at = NULL, notification_retry_at = ?, '
            'notification_attempts = notification_attempts + ? '
            "WHERE id = ? AND notification_claim_token = ? AND notification_state = 'pending'",
            ('sent' if delivered else 'pending', retry_at, 0 if delivered else 1, claim.message.id, claim.token),
        )
        return bool(cur.rowcount)


def pending_notification_count(path: str = DB_PATH) -> int:
    init_db(path)
    with sqlite3.connect(path) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM inbound WHERE notification_state = 'pending'"
        ).fetchone()[0])


def _row_to_message(row: sqlite3.Row) -> InboundMessage:
    return InboundMessage(
        id=row['id'],
        to_number=row['to_number'],
        from_number=row['from_number'],
        body=row['body'],
        received_at=row['received_at'],
        provider_message_id=row['provider_message_id'],
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
    query += ' ORDER BY received_at DESC, id DESC LIMIT ?'
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
    query += ' ORDER BY received_at DESC, id DESC LIMIT ?'
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
