"""SQLite 連線與 schema。"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "finvid.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    symbol TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    name_zh TEXT NOT NULL,
    name_en TEXT,
    aliases TEXT,
    phonetic TEXT
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    segment_index INTEGER,
    context TEXT,
    original TEXT NOT NULL,
    corrected TEXT,
    confidence REAL,
    candidates TEXT,
    reason TEXT,
    human_reviewed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_corrections_video ON corrections(video_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


if __name__ == "__main__":
    init_schema()
    print(f"Schema initialised at {DB_PATH}")
