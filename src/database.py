"""SQLite storage.

Holds login sessions, the installation -> Fiken customer mapping and the
status of invoice drafts per installation and month. No customer names or
other customer details are stored: only Fiken's contact id.
"""

import hashlib
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

_db_path: str | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    user TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_mapping (
    installation_id TEXT PRIMARY KEY,
    fiken_contact_id INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_draft (
    installation_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    draft_uuid TEXT NOT NULL,
    fiken_contact_id INTEGER,
    kwh REAL NOT NULL,
    unit_price_ore INTEGER NOT NULL,
    status TEXT NOT NULL,
    fiken_draft_id INTEGER,
    error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (installation_id, year, month)
);
"""


def init_db(path: str) -> None:
    global _db_path
    _db_path = path
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with get_db() as conn:
        # Sessions from older versions had no expiry; they are disposable.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if columns and "expires_at" not in columns:
            conn.execute("DROP TABLE sessions")
        conn.executescript(SCHEMA)


@contextmanager
def get_db():
    if _db_path is None:
        raise RuntimeError("init_db() has not been called")
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- sessions -------------------------------------------------------------


def _hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def create_session(session_id: str, access_token: str, user: str, max_age: int):
    now = time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)",
            (_hash(session_id), access_token, user, now, now + max_age),
        )


def get_session(session_id: str | None) -> sqlite3.Row | None:
    """Return the session (access_token, user) or None if missing or expired."""
    if not session_id:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT access_token, user, expires_at FROM sessions WHERE id_hash = ?",
            (_hash(session_id),),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < time.time():
            conn.execute("DELETE FROM sessions WHERE id_hash = ?", (_hash(session_id),))
            return None
        return row


def delete_session(session_id: str) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE id_hash = ?", (_hash(session_id),))


def delete_expired_sessions() -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))


# --- customer mapping -----------------------------------------------------


def get_customer_mappings() -> dict[str, int]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT installation_id, fiken_contact_id FROM customer_mapping"
        ).fetchall()
    return {r["installation_id"]: r["fiken_contact_id"] for r in rows}


def set_customer_mapping(installation_id: str, fiken_contact_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO customer_mapping VALUES (?, ?, ?)",
            (installation_id, fiken_contact_id, time.time()),
        )


def delete_customer_mapping(installation_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM customer_mapping WHERE installation_id = ?",
            (installation_id,),
        )


# --- invoice drafts -------------------------------------------------------


@dataclass
class DraftRecord:
    installation_id: str
    year: int
    month: int
    draft_uuid: str
    fiken_contact_id: int | None
    kwh: float
    unit_price_ore: int
    status: str
    fiken_draft_id: int | None = None
    error: str | None = None


def upsert_draft(record: DraftRecord) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO invoice_draft
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.installation_id,
                record.year,
                record.month,
                record.draft_uuid,
                record.fiken_contact_id,
                record.kwh,
                record.unit_price_ore,
                record.status,
                record.fiken_draft_id,
                record.error,
                time.time(),
            ),
        )


def get_drafts(year: int, month: int) -> dict[str, DraftRecord]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT installation_id, year, month, draft_uuid, fiken_contact_id, kwh,
                   unit_price_ore, status, fiken_draft_id, error
            FROM invoice_draft WHERE year = ? AND month = ?
            """,
            (year, month),
        ).fetchall()
    return {r["installation_id"]: DraftRecord(**dict(r)) for r in rows}


def fail_unfinished_drafts() -> None:
    """Mark drafts left 'pending' by a restart as failed so they can be retried."""
    with get_db() as conn:
        conn.execute(
            "UPDATE invoice_draft SET status = 'failed', error = ?, updated_at = ? "
            "WHERE status = 'pending'",
            ("Avbrutt fordi tjenesten ble startet på nytt. Prøv igjen.", time.time()),
        )
