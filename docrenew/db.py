"""SQLite access: connection, schema, transactions and the append-only action log."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit; explicit BEGIN in tx()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _short(value, limit: int = 1500) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def log_event(conn, user_id, event_type, tool=None, inp=None, out=None, outcome="ok",
              cycle_id=None, sim_date=None, idem_key=None, latency_ms=None) -> None:
    """Append one row to the action log. Committed immediately so it survives later failures."""
    conn.execute(
        "INSERT INTO action_log (ts, sim_date, pid, user_id, cycle_id, event_type, tool, input_json,"
        " output_summary, outcome, latency_ms, idem_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_utc(), sim_date, os.getpid(), user_id, cycle_id, event_type, tool,
         _short(inp), _short(out), outcome, latency_ms, idem_key),
    )


# ---- scoped getters: every one requires user_id (isolation lives here) -----------------------
def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_document(conn, user_id, doc_id):
    """Returns the document ONLY if it belongs to user_id, else None."""
    return conn.execute(
        "SELECT * FROM documents WHERE user_id=? AND doc_id=?", (user_id, doc_id)).fetchone()


def get_current_source(conn, user_id, kind):
    return conn.execute(
        "SELECT * FROM sources WHERE user_id=? AND kind=? AND status='current'", (user_id, kind)).fetchone()


def get_pref(conn, user_id, key, default=None):
    row = conn.execute("SELECT value FROM preferences WHERE user_id=? AND key=?", (user_id, key)).fetchone()
    return row["value"] if row else default


def set_pref(conn, user_id, key, value, source):
    conn.execute(
        "INSERT INTO preferences (user_id,key,value,source,updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value, source=excluded.source, updated_at=excluded.updated_at",
        (user_id, key, str(value), source, now_utc()))
