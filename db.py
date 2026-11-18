"""The database: courses (jobs), the work queue, team logins and small settings.

Uses Postgres when DATABASE_URL is set (Render Postgres, with Render's own backups).
Without it, it uses a SQLite file on the disk ($DATA_DIR/modulo.db), so the service
keeps working before the database is added, and for local testing.

Course files (briefs, designs, storyboards, Word files, built courses) stay on the
disk under $DATA_DIR/jobs/<id>/, as before. Each course's job.json there is kept as
a mirror of its database row, so a course folder still describes itself.

SQL in this file and its callers uses "?" placeholders; they are turned into "%s"
for Postgres.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("modulo.db")
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", HERE / "data"))

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        created DOUBLE PRECISION NOT NULL,
        updated DOUBLE PRECISION NOT NULL,
        data TEXT NOT NULL,
        claimed_by TEXT,
        claimed_at DOUBLE PRECISION,
        attempts INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status)",
    "CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created)",
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        role TEXT NOT NULL,
        pw_hash TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created DOUBLE PRECISION NOT NULL,
        invite_hash TEXT,
        invite_expires DOUBLE PRECISION,
        last_login DOUBLE PRECISION
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created DOUBLE PRECISION NOT NULL,
        expires DOUBLE PRECISION NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id)",
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
]

_pool = None
_sqlite: sqlite3.Connection | None = None
_sqlite_lock = threading.RLock()
_ready = False


def url() -> str:
    u = os.environ.get("DATABASE_URL", "").strip()
    return u


def is_postgres() -> bool:
    return bool(url())


def kind() -> str:
    return "Postgres" if is_postgres() else "SQLite file on the disk"


def sqlite_path() -> Path:
    return DATA_DIR / "modulo.db"


class _Conn:
    """A thin wrapper so callers write one kind of SQL for both databases."""

    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        cur = self.raw.execute(self._sql(sql), tuple(params))
        return cur.rowcount

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        cur = self.raw.execute(self._sql(sql), tuple(params))
        if cur.description is None:
            return []
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None


def _open():
    global _pool, _sqlite
    if is_postgres():
        if _pool is None:
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(url(), min_size=1, max_size=int(os.environ.get("DB_POOL_SIZE", "5")),
                                   kwargs={"autocommit": False}, open=True, timeout=20)
        return _pool
    if _sqlite is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _sqlite = sqlite3.connect(sqlite_path(), check_same_thread=False, isolation_level=None, timeout=30)
        _sqlite.execute("PRAGMA journal_mode=WAL")
        _sqlite.execute("PRAGMA busy_timeout=30000")
    return _sqlite


@contextmanager
def tx():
    """One transaction. Commits at the end, rolls back on an error."""
    if not _ready:
        init()
    if is_postgres():
        with _open().connection() as raw:  # the pool commits on success and rolls back on error
            yield _Conn(raw, True)
    else:
        with _sqlite_lock:
            raw = _open()
            raw.execute("BEGIN IMMEDIATE")
            try:
                yield _Conn(raw, False)
                raw.execute("COMMIT")
            except BaseException:
                raw.execute("ROLLBACK")
                raise


def execute(sql: str, params: tuple | list = ()) -> int:
    with tx() as c:
        return c.execute(sql, params)


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    with tx() as c:
        return c.query(sql, params)


def one(sql: str, params: tuple | list = ()) -> dict | None:
    with tx() as c:
        return c.one(sql, params)


def init() -> None:
    global _ready
    _ready = True
    try:
        with tx() as c:
            for stmt in SCHEMA:
                c.execute(stmt if is_postgres() else stmt.replace("DOUBLE PRECISION", "REAL"))
    except Exception:
        _ready = False
        raise
    log.info("Database ready: %s", kind())


def close() -> None:
    global _pool, _sqlite, _ready
    if _pool is not None:
        _pool.close()
        _pool = None
    if _sqlite is not None:
        _sqlite.close()
        _sqlite = None
    _ready = False


def ping() -> bool:
    try:
        one("SELECT 1 AS ok")
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Database check failed: %s", e)
        return False


# ---------------------------------------------------------------- small settings

def get_meta(key: str, default: str | None = None) -> str | None:
    row = one("SELECT value FROM meta WHERE key = ?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value))


# ---------------------------------------------------------------- moving from SQLite to Postgres

def copy_sqlite_into_postgres() -> int:
    """If the service ran on the SQLite file before Postgres was added, bring team logins and
    settings across once. Courses come across from their job.json mirrors (jobs.import_from_disk).
    Returns how many rows were copied. Never deletes anything."""
    if not is_postgres() or not sqlite_path().exists():
        return 0
    if one("SELECT COUNT(*) AS n FROM users")["n"]:
        return 0
    src = sqlite3.connect(sqlite_path())
    src.row_factory = sqlite3.Row
    copied = 0
    try:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with tx() as c:
            if "users" in tables:
                for r in src.execute("SELECT * FROM users"):
                    d = dict(r)
                    cols = ", ".join(d)
                    c.execute(f"INSERT INTO users ({cols}) VALUES ({', '.join('?' * len(d))}) ON CONFLICT DO NOTHING",
                              list(d.values()))
                    copied += 1
            if "meta" in tables:
                for r in src.execute("SELECT key, value FROM meta"):
                    c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT DO NOTHING", (r["key"], r["value"]))
                    copied += 1
    finally:
        src.close()
    if copied:
        log.info("Copied %d rows of logins and settings from the SQLite file into Postgres", copied)
    return copied


def export_all() -> dict:
    """Everything in the database except sign-in sessions, for the nightly backup."""
    out = {}
    for table in ("jobs", "users", "meta"):
        rows = query(f"SELECT * FROM {table}")
        if table == "jobs":
            for r in rows:
                r["data"] = json.loads(r["data"])
        out[table] = rows
    return out
