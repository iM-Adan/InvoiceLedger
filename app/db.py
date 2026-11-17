"""SQLite/Postgres persistence: runs, stage events, and the PO billing ledger."""
from __future__ import annotations

import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    filename TEXT, stored_path TEXT, mime TEXT, sha256 TEXT, source TEXT,
    engine_requested TEXT, engine_used TEXT,
    status TEXT NOT NULL,            -- queued | running | done | error
    decision TEXT,                   -- APPROVE | HOLD | REJECT
    headline TEXT,
    vendor_id TEXT, vendor_name TEXT,
    invoice_number TEXT, invoice_number_norm TEXT, invoice_date TEXT,
    total REAL, currency TEXT, po_number TEXT,
    duration_ms INTEGER,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS run_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, ts TEXT NOT NULL,
    stage TEXT NOT NULL, kind TEXT NOT NULL, status TEXT, title TEXT,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, source TEXT NOT NULL,
    vendor_id TEXT, invoice_number TEXT, invoice_number_norm TEXT, invoice_date TEXT,
    po_number TEXT, subtotal REAL, total REAL, lines_json TEXT, note TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_ledger_run_id ON ledger(run_id) WHERE run_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_postgres() -> bool:
    return bool(config.DATABASE_URL)


def _sql(sql: str) -> str:
    return sql.replace("?", "%s") if _is_postgres() else sql


def _initialize_seed_data() -> None:
    if _is_postgres():
        with _conn.transaction():
            inserted = _conn.execute(
                "INSERT INTO app_meta(key) VALUES ('seed-ledger') ON CONFLICT(key) DO NOTHING RETURNING key"
            ).fetchone()
            if inserted:
                seed_ledger()
        return

    _conn.execute("BEGIN IMMEDIATE")
    try:
        inserted = _conn.execute(
            "INSERT INTO app_meta(key) VALUES ('seed-ledger') ON CONFLICT(key) DO NOTHING"
        )
        if inserted.rowcount:
            seed_ledger()
        _conn.execute("COMMIT")
    except Exception:
        _conn.execute("ROLLBACK")
        raise


def conn():
    global _conn
    with _lock:
        if _conn is not None and not getattr(_conn, "closed", False):
            return _conn
        if config.VERCEL_RUNTIME and not config.DATABASE_URL:
            raise RuntimeError("DATABASE_URL must be configured for Vercel deployments.")
        if _is_postgres():
            import psycopg
            from psycopg.rows import dict_row

            _conn = psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=10)
            schema = SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
            schema = schema.replace(" REAL", " DOUBLE PRECISION")
            with _conn.transaction():
                _conn.execute("SELECT pg_advisory_xact_lock(9413219)")
                for statement in schema.split(";"):
                    if statement.strip():
                        _conn.execute(statement)
        else:
            config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
        _initialize_seed_data()
    return _conn


def execute(sql: str, params: tuple = ()):
    with _lock:
        return conn().execute(_sql(sql), params)


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(_sql(sql), params).fetchall()]


@contextmanager
def transaction():
    with _lock:
        c = conn()
        if _is_postgres():
            with c.transaction():
                yield c
            return
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def seed_ledger() -> None:
    from .normalize import norm_invoice_number  # local import to avoid cycle

    path = config.DATA_DIR / "seed_ledger.csv"
    lines = {r["po_number"]: r for r in csv.DictReader(open(config.DATA_DIR / "po_lines.csv"))}
    for row in csv.DictReader(open(path)):
        po_line = lines.get(row["po_number"])
        seed_lines = []
        if po_line:
            unit = float(po_line["unit_price"])
            sub = float(row["subtotal"])
            qty = sub / unit if po_line["billing_type"] == "quantity" else 0
            seed_lines.append({"po_line": int(po_line["line_no"]), "qty": qty, "amount": sub})
        _conn.execute(_sql(
            "INSERT INTO ledger(run_id, source, vendor_id, invoice_number, invoice_number_norm, invoice_date, po_number, subtotal, total, lines_json, note, created_at)"
            " VALUES (NULL,'seed',?,?,?,?,?,?,?,?,?,?)"),
            (row["vendor_id"], row["invoice_number"], norm_invoice_number(row["invoice_number"]), row["invoice_date"],
             row["po_number"], float(row["subtotal"]), float(row["total"]), json.dumps(seed_lines), row["note"], now_iso()),
        )


def reset() -> None:
    with transaction() as c:
        c.execute("DELETE FROM run_jobs")
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM ledger")
        seed_ledger()
    if not config.USE_BLOB_STORAGE:
        for p in config.UPLOAD_DIR.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass


def add_event(run_id: str, stage: str, kind: str, status: str | None = None, title: str | None = None, payload=None) -> None:
    execute(
        "INSERT INTO events(run_id, ts, stage, kind, status, title, payload_json) VALUES (?,?,?,?,?,?,?)",
        (run_id, now_iso(), stage, kind, status, title, json.dumps(payload, default=str) if payload is not None else None),
    )


def events_after(run_id: str, after_id: int) -> list[dict]:
    rows = query("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id))
    for r in rows:
        r["payload"] = json.loads(r.pop("payload_json")) if r.get("payload_json") else None
    return rows


def update_run(run_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))


def get_run(run_id: str) -> dict | None:
    rows = query("SELECT * FROM runs WHERE id=?", (run_id,))
    if not rows:
        return None
    r = rows[0]
    r["result"] = json.loads(r.pop("result_json")) if r.get("result_json") else None
    return r


def enqueue_run(run_id: str) -> None:
    execute("INSERT INTO run_jobs(run_id) VALUES (?)", (run_id,))


def claim_next_run() -> str | None:
    with transaction() as c:
        if _is_postgres():
            row = c.execute(
                "SELECT j.run_id FROM run_jobs j JOIN runs r ON r.id=j.run_id "
                "WHERE r.status='queued' ORDER BY j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED"
            ).fetchone()
        else:
            row = c.execute(
                "SELECT j.run_id FROM run_jobs j JOIN runs r ON r.id=j.run_id "
                "WHERE r.status='queued' ORDER BY j.id LIMIT 1"
            ).fetchone()
        if not row:
            return None
        run_id = row["run_id"]
        c.execute(_sql("UPDATE runs SET status='running' WHERE id=? AND status='queued'"), (run_id,))
        c.execute(_sql("DELETE FROM run_jobs WHERE run_id=?"), (run_id,))
        return run_id


def mark_interrupted_runs(
    headline: str = "Interrupted by server restart - please re-run",
    include_queued: bool = False,
) -> None:
    with transaction() as c:
        status_filter = "status IN ('queued','running')" if include_queued else "status='running'"
        c.execute(_sql("UPDATE runs SET status='error', decision='HOLD', headline=? WHERE " + status_filter), (headline,))
