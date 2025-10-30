import sqlite3
from contextlib import contextmanager

DB_PATH = "sessions.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create sessions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            user TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def create_session(session_id, access_token, user):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO sessions (id, access_token, user) VALUES (?, ?, ?)",
            (session_id, access_token, user),
        )
        conn.commit()


def get_session(session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT access_token, user FROM sessions WHERE id = ?", (session_id,)
        )
        row = cursor.fetchone()
        return row


def delete_session(session_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()


# Initialize database on import
init_db()
