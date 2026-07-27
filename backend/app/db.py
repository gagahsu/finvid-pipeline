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

-- status：
--   auto         信心足夠，直接套用（人工審核時仍看得到，可退回）
--   needs_review 有修正建議但信心不足，必須人工確認才算數
--   rejected     LLM 判斷不需替換
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
    status TEXT NOT NULL DEFAULT 'needs_review',
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
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """補上既有資料庫缺少的欄位。

    CREATE TABLE IF NOT EXISTS 不會動到已存在的表，所以新欄位要另外加。
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(corrections)")}
    if "status" not in existing:
        conn.execute(
            "ALTER TABLE corrections ADD COLUMN status TEXT NOT NULL DEFAULT 'needs_review'"
        )


if __name__ == "__main__":
    init_schema()
    print(f"Schema initialised at {DB_PATH}")
