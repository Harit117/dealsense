"""SQLite storage. Mirrors the Supabase (Postgres) tables from the architecture slide."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    phone           TEXT,
    email           TEXT,
    source          TEXT NOT NULL,
    raw_messages    TEXT NOT NULL DEFAULT '[]',   -- JSON list of {at, text}
    budget_min      REAL,                          -- INR
    budget_max      REAL,                          -- INR
    localities      TEXT NOT NULL DEFAULT '[]',   -- JSON list
    bhk             INTEGER,
    property_type   TEXT,                          -- apartment | villa | plot
    urgency_days    INTEGER,                       -- wants to move within N days
    loan_preapproved INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    extraction      TEXT,                          -- llm | rules
    created_at      TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' -- active | closed | lost
);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);

CREATE TABLE IF NOT EXISTS properties (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    locality        TEXT NOT NULL,
    property_type   TEXT NOT NULL,
    bhk             INTEGER,
    area_sqft       INTEGER,
    price           REAL NOT NULL,
    seller_flexibility REAL NOT NULL DEFAULT 0.3, -- 0 firm .. 1 very negotiable
    listed_at       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'available'
);

CREATE TABLE IF NOT EXISTS engagements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES leads(id),
    property_id INTEGER REFERENCES properties(id),
    kind        TEXT NOT NULL,          -- view | reply | visit | call | message_sent
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eng_lead ON engagements(lead_id);

CREATE TABLE IF NOT EXISTS deals (
    lead_id     INTEGER NOT NULL REFERENCES leads(id),
    property_id INTEGER NOT NULL REFERENCES properties(id),
    score       REAL NOT NULL,          -- 0..100 = P(close) x 100
    p_close     REAL NOT NULL,
    deal_value  REAL NOT NULL,          -- broker commission in INR
    expected_value REAL NOT NULL,       -- p_close x deal_value
    features    TEXT NOT NULL,          -- JSON
    reasons     TEXT NOT NULL,          -- JSON list of {text, sign}
    route       TEXT NOT NULL,          -- alert | digest | nurture
    next_step   TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    outcome     TEXT,                   -- called | visited | closed | lost | override_up | override_down
    outcome_at  TEXT,
    PRIMARY KEY (lead_id, property_id)
);

CREATE TABLE IF NOT EXISTS outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER,
    property_id INTEGER,
    locality    TEXT,
    features    TEXT NOT NULL,          -- JSON snapshot used for training
    label       INTEGER NOT NULL,       -- 1 = progressed to visit/close, 0 = lost
    kind        TEXT NOT NULL,          -- historical | broker
    at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,          -- alert | digest | nurture
    channel     TEXT NOT NULL,          -- whatsapp-sim | telegram | slack
    lead_id     INTEGER,
    property_id INTEGER,
    score       REAL,
    title       TEXT,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued', -- queued | sent | failed | snoozed
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


@contextmanager
def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn, key, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def lead_from_row(row) -> dict:
    d = dict(row)
    d["localities"] = json.loads(d["localities"] or "[]")
    d["raw_messages"] = json.loads(d["raw_messages"] or "[]")
    d["loan_preapproved"] = bool(d["loan_preapproved"])
    return d
