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

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'channel',
    name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- status 是 pipeline 狀態機的當前狀態，合法值與轉換規則由 app/pipeline.py 管，
-- 不在 DB 層用 CHECK 約束：狀態機日後會增修，改 CHECK 得整表重建。
-- url 存下來是為了重跑：download 階段失敗或要重下載時不必再叫使用者提供網址。
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    url TEXT,
    published_at TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    source_id INTEGER REFERENCES sources(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);

-- 一支影片的每個 stage 只留一列，重跑時累加 retry_count。
-- 不用「每次執行插一列」的作法：CLAUDE.md 的欄位定義裡有 retry_count，
-- 表示這張表要的是「該階段目前狀態」而非完整執行歷史。
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_detail TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(video_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_jobs_video ON jobs(video_id);

-- 表定義裡已有 UNIQUE(video_id, stage)，這道索引是給「表已存在但沒帶約束」的
-- 舊 DB 補的：ON CONFLICT(video_id, stage) 要有 unique 約束或 unique index 才成立。
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_video_stage ON jobs(video_id, stage);

CREATE TABLE IF NOT EXISTS transcripts (
    video_id TEXT PRIMARY KEY,
    raw_text TEXT NOT NULL,
    corrected_text TEXT,
    segments TEXT,
    applied_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
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
        # 先遷移再建 schema：SCHEMA 裡的 CREATE INDEX 會參照新欄位（例如
        # videos.status），舊 DB 還沒有那些欄位時建索引會直接失敗。
        _migrate(conn)
        conn.executescript(SCHEMA)


def _migrate(conn: sqlite3.Connection) -> None:
    """補上既有資料庫缺少的欄位。

    CREATE TABLE IF NOT EXISTS 不會動到已存在的表，所以新欄位要另外加。
    """
    _add_columns(
        conn,
        "corrections",
        {"status": "TEXT NOT NULL DEFAULT 'needs_review'"},
    )
    _add_columns(
        conn,
        "videos",
        {
            "title": "TEXT",
            "url": "TEXT",
            "published_at": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'PENDING'",
            "source_id": "INTEGER",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        },
    )
    _add_columns(
        conn,
        "sources",
        {
            "type": "TEXT NOT NULL DEFAULT 'channel'",
            "name": "TEXT",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "TEXT",
        },
    )
    _add_columns(
        conn,
        "jobs",
        {
            "status": "TEXT NOT NULL DEFAULT 'running'",
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "error_detail": "TEXT",
            "started_at": "TEXT",
            "updated_at": "TEXT",
        },
    )


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """對既有表補欄位；表還不存在時直接略過（CREATE 已經帶了完整定義）。

    ALTER TABLE ADD COLUMN 不接受非常數 DEFAULT，所以 created_at/updated_at
    這類欄位在遷移路徑上只能給 NULL default，由寫入端負責填值。
    """
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return
    existing = {r["name"] for r in info}
    for col, ddl in columns.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


if __name__ == "__main__":
    init_schema()
    print(f"Schema initialised at {DB_PATH}")
