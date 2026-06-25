"""
SQLite database setup.

On Render: add a persistent disk at /data in your service settings,
then set the env var DB_PATH=/data/research-agent.db
This is what makes logins survive restarts and redeployments.

Locally: defaults to research-agent.db in the project root.
"""
import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent.parent / "research-agent.db"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables on first startup. Safe to call multiple times."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL,
            email         TEXT    UNIQUE NOT NULL,
            password_hash TEXT    NOT NULL,
            created_at    TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            openai_key      TEXT NOT NULL DEFAULT '',
            pinecone_key    TEXT NOT NULL DEFAULT '',
            serpapi_key     TEXT NOT NULL DEFAULT '',
            pinecone_index  TEXT NOT NULL DEFAULT 'langgraph-research-agent',
            pinecone_cloud  TEXT NOT NULL DEFAULT 'aws',
            pinecone_region TEXT NOT NULL DEFAULT 'us-east-1',
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()
