from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


DB_PATH = os.getenv('INBOUND_DB_PATH', '').strip() or 'inbound.db'


def enabled(name: str) -> bool:
    return os.getenv(name, '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        value_dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if value_dt.tzinfo is None:
            value_dt = value_dt.replace(tzinfo=timezone.utc)
        return value_dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def create_tables(conn: sqlite3.Connection) -> None:
    """Called inside store.init_db's schema transaction."""
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS audio_feature_state (
            feature TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL,
            activated_at TEXT
        )'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS audio_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            inbound_id INTEGER,
            recording_id TEXT,
            connection_id TEXT,
            to_number TEXT,
            from_number TEXT,
            recording_url TEXT,
            audio BLOB,
            audio_format TEXT,
            created_at TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            claim_token TEXT,
            claimed_at TEXT,
            retry_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT ''
        )'''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audio_pending ON audio_jobs(state, retry_at, claimed_at, id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audio_created_at ON audio_jobs(created_at)')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS audio_calls (
            call_key TEXT PRIMARY KEY,
            connection_id TEXT NOT NULL,
            to_number TEXT NOT NULL,
            from_number TEXT NOT NULL,
            created_at TEXT NOT NULL
        )'''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_audio_calls_created ON audio_calls(created_at)')


def configure_features(*, sms: bool, calls: bool, imports: bool, path: str = DB_PATH) -> None:
    """Track opt-in activation and cancel unsent work for disabled features.

    A stable activation time across restarts lets polling recover messages from
    downtime without speaking messages received before the feature was enabled.
    """
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        create_tables(conn)
        row = conn.execute(
            "SELECT enabled, activated_at FROM audio_feature_state WHERE feature = 'sms'"
        ).fetchone()
        activated_at = row[1] if row and row[0] and row[1] else _now().isoformat()
        conn.execute(
            "INSERT INTO audio_feature_state(feature, enabled, activated_at) VALUES ('sms', ?, ?) "
            'ON CONFLICT(feature) DO UPDATE SET enabled = excluded.enabled, activated_at = excluded.activated_at',
            (int(sms), activated_at if sms else None),
        )
        for kind, is_enabled in (('sms', sms), ('call', calls), ('import', imports)):
            if not is_enabled:
                # A later opt-in must not unexpectedly replay previously queued
                # audio. Sent and cancelled keys remain as deduplication markers.
                conn.execute(
                    "UPDATE audio_jobs SET state = 'cancelled', audio = NULL, recording_url = NULL, "
                    "claim_token = NULL, claimed_at = NULL, last_error = 'feature_disabled' "
                    "WHERE kind = ? AND state IN ('pending', 'sending', 'uncertain', 'failed')",
                    (kind,),
                )


def sms_needs_audio(conn: sqlite3.Connection, *, inbound_id: int, received_at: str) -> bool:
    if not enabled('SMS_VOICE_ENABLED'):
        return False
    feature = conn.execute(
        "SELECT enabled, activated_at FROM audio_feature_state WHERE feature = 'sms'"
    ).fetchone()
    received = _parse_time(received_at)
    activated = _parse_time(feature[1]) if feature and feature[0] else None
    if not received or not activated or received < activated:
        return False
    row = conn.execute('SELECT 1 FROM audio_jobs WHERE dedup_key = ?', (f'sms:{inbound_id}',)).fetchone()
    return row is None


def enqueue_sms_in_transaction(
    conn: sqlite3.Connection, *, inbound_id: int, received_at: str, trusted: bool,
) -> bool:
    if not trusted or not sms_needs_audio(conn, inbound_id=inbound_id, received_at=received_at):
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO audio_jobs(dedup_key, kind, inbound_id, created_at) VALUES (?, 'sms', ?, ?)",
        (f'sms:{inbound_id}', inbound_id, _now().isoformat()),
    )
    return bool(cur.rowcount)


def _call_key(connection_id: str, call_sid: str) -> str:
    # A call-control ID can also control the call; retain only a hash.
    return hashlib.sha256((connection_id + '\0' + call_sid).encode('utf-8')).hexdigest()


def register_call(connection_id: str, call_sid: str, to_number: str, from_number: str, path: str = DB_PATH) -> bool:
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        key = _call_key(connection_id, call_sid)
        conn.execute(
            'INSERT OR IGNORE INTO audio_calls(call_key, connection_id, to_number, from_number, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (key, connection_id, to_number, from_number, _now().isoformat()),
        )
        row = conn.execute(
            'SELECT connection_id, to_number, from_number FROM audio_calls WHERE call_key = ?', (key,)
        ).fetchone()
        return bool(row and row == (connection_id, to_number, from_number))


def find_call(connection_id: str, call_sid: str, path: str = DB_PATH) -> tuple[str, str] | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            'SELECT to_number, from_number FROM audio_calls WHERE call_key = ? AND connection_id = ?',
            (_call_key(connection_id, call_sid), connection_id),
        ).fetchone()
    return (row[0], row[1]) if row else None


def enqueue_recording(
    *, kind: str, connection_id: str, recording_id: str, to_number: str, from_number: str,
    url: str = '', path: str = DB_PATH,
) -> bool:
    if kind not in {'call', 'import'}:
        raise ValueError('Invalid audio job kind')
    key = f'recording:{connection_id}:{recording_id}'
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.execute(
            'INSERT OR IGNORE INTO audio_jobs '
            '(dedup_key, kind, recording_id, connection_id, to_number, from_number, recording_url, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (key, kind, recording_id, connection_id, to_number, from_number, url or None, _now().isoformat()),
        )
        if not cur.rowcount and url:
            # Duplicate signed callbacks sometimes carry a fresher short-lived URL.
            # Do not requeue sent, cancelled, or uncertain deliveries.
            conn.execute(
                "UPDATE audio_jobs SET recording_url = ? WHERE dedup_key = ? AND state = 'pending' AND audio IS NULL",
                (url, key),
            )
        return bool(cur.rowcount)


@dataclass(frozen=True)
class AudioClaim:
    id: int
    token: str = field(repr=False)
    kind: str
    inbound_id: int | None
    body: str | None = field(repr=False)
    recording_id: str | None
    connection_id: str | None
    to_number: str = field(repr=False)
    from_number: str = field(repr=False)
    recording_url: str | None = field(repr=False)
    audio: bytes | None = field(repr=False)
    audio_format: str | None
    created_at: str


def claim_next(kinds: tuple[str, ...], path: str = DB_PATH, lease_seconds: int = 120) -> AudioClaim | None:
    if not kinds:
        return None
    now = _now()
    expired = (now - timedelta(seconds=lease_seconds)).isoformat()
    token = uuid.uuid4().hex
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        # A crash or timeout after beginning a Telegram upload is ambiguous.
        # Never silently resend it: an owner can explicitly retry if desired.
        conn.execute(
            "UPDATE audio_jobs SET state = 'uncertain', claim_token = NULL, claimed_at = NULL, "
            "last_error = 'delivery_unconfirmed' WHERE state = 'sending' AND claimed_at < ?",
            (expired,),
        )
        placeholders = ','.join('?' for _ in kinds)
        row = conn.execute(
            'SELECT audio_jobs.*, inbound.body, '
            'COALESCE(audio_jobs.to_number, inbound.to_number, \'\') AS destination, '
            'COALESCE(audio_jobs.from_number, inbound.from_number, \'\') AS sender '
            'FROM audio_jobs LEFT JOIN inbound ON inbound.id = audio_jobs.inbound_id '
            f"WHERE audio_jobs.kind IN ({placeholders}) AND audio_jobs.state = 'pending' "
            'AND (audio_jobs.claimed_at IS NULL OR audio_jobs.claimed_at < ?) '
            'AND (audio_jobs.retry_at IS NULL OR audio_jobs.retry_at <= ?) '
            'ORDER BY CASE WHEN audio_jobs.attempts = 0 THEN 0 ELSE 1 END, audio_jobs.id LIMIT 1',
            (*kinds, expired, now.isoformat()),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            'UPDATE audio_jobs SET claim_token = ?, claimed_at = ? WHERE id = ?',
            (token, now.isoformat(), row['id']),
        )
    return AudioClaim(
        id=row['id'], token=token, kind=row['kind'], inbound_id=row['inbound_id'], body=row['body'],
        recording_id=row['recording_id'], connection_id=row['connection_id'], to_number=row['destination'],
        from_number=row['sender'], recording_url=row['recording_url'], audio=row['audio'],
        audio_format=row['audio_format'], created_at=row['created_at'],
    )


def save_audio(claim: AudioClaim, audio: bytes, audio_format: str, path: str = DB_PATH) -> bool:
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "UPDATE audio_jobs SET audio = ?, audio_format = ?, recording_url = NULL "
            "WHERE id = ? AND state = 'pending' AND claim_token = ?",
            (audio, audio_format, claim.id, claim.token),
        )
        return bool(cur.rowcount)


def begin_sending(claim: AudioClaim, path: str = DB_PATH) -> bool:
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "UPDATE audio_jobs SET state = 'sending', claimed_at = ? "
            "WHERE id = ? AND state = 'pending' AND claim_token = ? AND audio IS NOT NULL",
            (_now().isoformat(), claim.id, claim.token),
        )
        return bool(cur.rowcount)


def finish(
    claim: AudioClaim, outcome: str, error: str = '', *, path: str = DB_PATH, retry_seconds: int = 30,
) -> bool:
    if outcome not in {'sent', 'retry', 'uncertain', 'failed'}:
        raise ValueError('Invalid audio delivery outcome')
    state = 'pending' if outcome == 'retry' else outcome
    retry_at = (_now() + timedelta(seconds=max(0, retry_seconds))).isoformat() if outcome == 'retry' else None
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            'UPDATE audio_jobs SET state = ?, claim_token = NULL, claimed_at = NULL, retry_at = ?, '
            'attempts = attempts + ?, last_error = ?, '
            "audio = CASE WHEN ? = 'sent' THEN NULL ELSE audio END, "
            "recording_url = CASE WHEN ? = 'sent' THEN NULL ELSE recording_url END "
            "WHERE id = ? AND claim_token = ? AND state IN ('pending', 'sending')",
            (state, retry_at, 0 if outcome == 'sent' else 1, error, outcome, outcome, claim.id, claim.token),
        )
        return bool(cur.rowcount)


def retry_unconfirmed(kinds: tuple[str, ...], path: str = DB_PATH) -> int:
    if not kinds:
        return 0
    placeholders = ','.join('?' for _ in kinds)
    with sqlite3.connect(path) as conn:
        cur = conn.execute(
            "UPDATE audio_jobs SET state = 'pending', retry_at = NULL, claim_token = NULL, claimed_at = NULL, "
            "last_error = '' WHERE state IN ('uncertain', 'failed') AND kind IN (" + placeholders + ')',
            kinds,
        )
        return int(cur.rowcount)


def prune_in_transaction(conn: sqlite3.Connection, hours: int) -> None:
    cutoff = (_now() - timedelta(hours=max(1, int(hours)))).isoformat()
    call_cutoff = (_now() - timedelta(hours=3)).isoformat()
    # Do not remove an active send: its lease will resolve or become uncertain.
    conn.execute("DELETE FROM audio_jobs WHERE created_at < ? AND state != 'sending'", (cutoff,))
    conn.execute('DELETE FROM audio_calls WHERE created_at < ?', (call_cutoff,))


def prune(hours: int, path: str = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        prune_in_transaction(conn, hours)


def status(path: str = DB_PATH) -> dict[str, int]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute('SELECT state, COUNT(*) FROM audio_jobs GROUP BY state').fetchall()
    return {str(state): int(count) for state, count in rows}


def issue_counts(path: str = DB_PATH) -> dict[str, int]:
    # These names are fixed by our code, never provider error bodies or URLs.
    allowed = {
        'sms_too_long', 'speech_unavailable', 'recording_unavailable',
        'unsupported_recording', 'recording_too_large', 'recording_identity_mismatch',
        'telegram_rejected', 'delivery_unconfirmed', 'unsupported_audio', 'audio_processing_error',
    }
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT last_error, COUNT(*) FROM audio_jobs "
            "WHERE state IN ('pending', 'uncertain', 'failed') AND last_error != '' GROUP BY last_error"
        ).fetchall()
    return {str(error): int(count) for error, count in rows if error in allowed}
