"""SQLite storage.

Holds login sessions, each user's Fiken connection (OAuth tokens, encrypted),
the installation -> Fiken customer mapping and the status of invoice drafts.
Mappings and drafts belong to a Fiken company. No customer names or other
customer details are stored: only Fiken's contact id.
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

CREATE TABLE IF NOT EXISTS fiken_connection (
    zaptec_user TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,   -- encrypted
    refresh_token TEXT NOT NULL,  -- encrypted
    expires_at REAL NOT NULL,
    company_slug TEXT,
    company_name TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_mapping (
    company_slug TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    fiken_contact_id INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (company_slug, installation_id)
);

CREATE TABLE IF NOT EXISTS invoice_draft (
    company_slug TEXT NOT NULL,
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
    PRIMARY KEY (company_slug, installation_id, year, month)
);

CREATE TABLE IF NOT EXISTS invoice_settings (
    company_slug TEXT PRIMARY KEY,
    income_account TEXT NOT NULL,
    vat_type TEXT NOT NULL,
    days_until_due INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_total (
    installation_id TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    kwh REAL NOT NULL,
    session_count INTEGER NOT NULL,
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
        # The personal-token version had no company column. It was never
        # released, so its rows are dropped rather than migrated.
        for table in ("customer_mapping", "invoice_draft"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if columns and "company_slug" not in columns:
                conn.execute(f"DROP TABLE {table}")
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


# --- Fiken connection ----------------------------------------------------


@dataclass
class FikenConnection:
    zaptec_user: str
    access_token: str  # encrypted
    refresh_token: str  # encrypted
    expires_at: float
    company_slug: str | None
    company_name: str | None


def get_fiken_connection(zaptec_user: str) -> FikenConnection | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT zaptec_user, access_token, refresh_token, expires_at,
                   company_slug, company_name
            FROM fiken_connection WHERE zaptec_user = ?
            """,
            (zaptec_user.lower(),),
        ).fetchone()
    return FikenConnection(**dict(row)) if row else None


def save_fiken_tokens(
    zaptec_user: str, access_token: str, refresh_token: str, expires_at: float
) -> None:
    """Store new tokens, keeping the chosen company if there is one."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO fiken_connection
                (zaptec_user, access_token, refresh_token, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (zaptec_user) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (zaptec_user.lower(), access_token, refresh_token, expires_at, time.time()),
        )


def set_fiken_company(zaptec_user: str, slug: str, name: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE fiken_connection SET company_slug = ?, company_name = ?, "
            "updated_at = ? WHERE zaptec_user = ?",
            (slug, name, time.time(), zaptec_user.lower()),
        )


def delete_fiken_connection(zaptec_user: str) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM fiken_connection WHERE zaptec_user = ?", (zaptec_user.lower(),)
        )


# --- customer mapping -----------------------------------------------------


def get_customer_mappings(company_slug: str) -> dict[str, int]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT installation_id, fiken_contact_id FROM customer_mapping "
            "WHERE company_slug = ?",
            (company_slug,),
        ).fetchall()
    return {r["installation_id"]: r["fiken_contact_id"] for r in rows}


def set_customer_mapping(
    company_slug: str, installation_id: str, fiken_contact_id: int
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO customer_mapping VALUES (?, ?, ?, ?)",
            (company_slug, installation_id, fiken_contact_id, time.time()),
        )


def delete_customer_mapping(company_slug: str, installation_id: str) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM customer_mapping WHERE company_slug = ? AND installation_id = ?",
            (company_slug, installation_id),
        )


# --- invoice drafts -------------------------------------------------------


@dataclass
class DraftRecord:
    company_slug: str
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.company_slug,
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


def get_drafts(company_slug: str, year: int, month: int) -> dict[str, DraftRecord]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT company_slug, installation_id, year, month, draft_uuid,
                   fiken_contact_id, kwh, unit_price_ore, status, fiken_draft_id, error
            FROM invoice_draft WHERE company_slug = ? AND year = ? AND month = ?
            """,
            (company_slug, year, month),
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


# --- usage totals cache ---------------------------------------------------
# Totals per installation and closed month. Plain numbers, no personal data.


def get_usage_totals(year: int, month: int) -> dict[str, tuple[float, int]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT installation_id, kwh, session_count FROM usage_total "
            "WHERE year = ? AND month = ?",
            (year, month),
        ).fetchall()
    return {r["installation_id"]: (r["kwh"], r["session_count"]) for r in rows}


def set_usage_total(
    installation_id: str, year: int, month: int, kwh: float, session_count: int
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO usage_total VALUES (?, ?, ?, ?, ?, ?)",
            (installation_id, year, month, kwh, session_count, time.time()),
        )


def delete_usage_totals(year: int, month: int, installation_ids: list[str]) -> None:
    with get_db() as conn:
        conn.executemany(
            "DELETE FROM usage_total WHERE installation_id = ? AND year = ? AND month = ?",
            [(i, year, month) for i in installation_ids],
        )


# --- invoice settings -----------------------------------------------------


def get_invoice_settings(company_slug: str) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT income_account, vat_type, days_until_due FROM invoice_settings "
            "WHERE company_slug = ?",
            (company_slug,),
        ).fetchone()


def set_invoice_settings(
    company_slug: str, income_account: str, vat_type: str, days_until_due: int
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO invoice_settings VALUES (?, ?, ?, ?, ?)",
            (company_slug, income_account, vat_type, days_until_due, time.time()),
        )
