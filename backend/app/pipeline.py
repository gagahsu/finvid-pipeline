"""Pipeline 狀態機與 job 記錄。

狀態流程（CLAUDE.md）：

    PENDING → DOWNLOADING → TRANSCRIBING → CORRECTING
            → SUMMARIZING → REVIEW → RENDERING → PUBLISHING
            → PUBLISHED | FAILED

設計上把「正常前進」跟「人為退回」拆成兩組 API：

- ``advance()`` 只允許往前走一格，亂跳會直接拋 ``InvalidTransition``。
  財經內容出錯成本高，狀態被跳過等於審核閘門被繞過，所以寧可讓程式炸掉。
- ``back_to_review()`` / ``rerun_from()`` 是明示的退回，呼叫端必須自己講清楚
  要退到哪，不會有「不小心倒退」這種事。

FAILED 是任何狀態都能進的終點，但不是死路：要重跑得走 ``rerun_from()``。
"""

import sqlite3
from contextlib import contextmanager

from app.db import connect, init_schema

PENDING = "PENDING"
DOWNLOADING = "DOWNLOADING"
TRANSCRIBING = "TRANSCRIBING"
CORRECTING = "CORRECTING"
SUMMARIZING = "SUMMARIZING"
REVIEW = "REVIEW"
RENDERING = "RENDERING"
PUBLISHING = "PUBLISHING"
PUBLISHED = "PUBLISHED"
FAILED = "FAILED"

# 主線順序。索引大小即「先後」，退回/前進的判斷都靠它。
ORDER = [
    PENDING,
    DOWNLOADING,
    TRANSCRIBING,
    CORRECTING,
    SUMMARIZING,
    REVIEW,
    RENDERING,
    PUBLISHING,
    PUBLISHED,
]
STATUSES = frozenset(ORDER) | {FAILED}

# 終點狀態：不能再往前，只能被明示退回重跑
TERMINAL = frozenset({PUBLISHED, FAILED})

# 每個 CLI 階段跑完後該進入的狀態。
# apply 沒有自己的狀態 —— 它是 CORRECTING 的後半段（把 corrections 套回逐字稿），
# 不是獨立階段，但仍然要有自己的 job 紀錄才看得出是哪一半失敗。
STAGE_STATUS = {
    "download": DOWNLOADING,
    "transcribe": TRANSCRIBING,
    "correct": CORRECTING,
    "apply": CORRECTING,
}


class InvalidTransition(Exception):
    """不合法的狀態轉換。"""


class UnknownVideo(Exception):
    """videos 表裡沒有這支影片。"""


def _index(status: str) -> int:
    try:
        return ORDER.index(status)
    except ValueError:
        return -1


# --- videos / sources ---------------------------------------------------


def upsert_source(channel_id: str, name: str | None = None, type_: str = "channel") -> int:
    """新增或更新訂閱來源，回傳 source id。"""
    init_schema()
    with connect() as conn:
        conn.execute(
            """INSERT INTO sources (channel_id, type, name)
               VALUES (?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   type = excluded.type,
                   name = COALESCE(excluded.name, sources.name)""",
            (channel_id, type_, name),
        )
        row = conn.execute(
            "SELECT id FROM sources WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    return row["id"]


def register_video(
    video_id: str,
    *,
    title: str | None = None,
    url: str | None = None,
    published_at: str | None = None,
    source_id: int | None = None,
) -> str:
    """登記影片（已存在則只補資料，不動 status），回傳當前 status。

    刻意不重設 status：RSS 輪詢會重複看到同一支影片，若每次都重設成 PENDING，
    已經跑到 REVIEW 的影片會被打回原形重跑一輪。
    """
    init_schema()
    with connect() as conn:
        conn.execute(
            """INSERT INTO videos (video_id, title, url, published_at, source_id, status)
               VALUES (?, ?, ?, ?, ?, 'PENDING')
               ON CONFLICT(video_id) DO UPDATE SET
                   title = COALESCE(excluded.title, videos.title),
                   url = COALESCE(excluded.url, videos.url),
                   published_at = COALESCE(excluded.published_at, videos.published_at),
                   source_id = COALESCE(excluded.source_id, videos.source_id),
                   updated_at = datetime('now')""",
            (video_id, title, url, published_at, source_id),
        )
        row = conn.execute(
            "SELECT status FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    return row["status"]


def get_video(video_id: str) -> sqlite3.Row:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    if row is None:
        raise UnknownVideo(video_id)
    return row


def get_status(video_id: str) -> str:
    return get_video(video_id)["status"]


def list_videos(status: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM videos"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC"
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


# --- 狀態轉換 -----------------------------------------------------------


def can_advance(current: str, target: str) -> bool:
    """只允許沿主線往前走一格，或任何狀態掉到 FAILED。"""
    if target == FAILED:
        return True
    if current in TERMINAL:
        return False
    ci, ti = _index(current), _index(target)
    if ci < 0 or ti < 0:
        return False
    return ti == ci + 1


def _set_status(video_id: str, status: str) -> None:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE videos SET status = ?, updated_at = datetime('now') WHERE video_id = ?",
            (status, video_id),
        )
        if cur.rowcount == 0:
            raise UnknownVideo(video_id)


def advance(video_id: str, target: str) -> str:
    """往前推進一格。不合法就拋 InvalidTransition。"""
    if target not in STATUSES:
        raise InvalidTransition(f"unknown status: {target}")
    current = get_status(video_id)
    if current == target:
        # 同一階段重跑（例如 correct 失敗後再跑一次）不算轉換，直接放行
        return current
    if not can_advance(current, target):
        raise InvalidTransition(f"{video_id}: {current} -> {target}")
    _set_status(video_id, target)
    return target


def fail(video_id: str, error: str | None = None, stage: str | None = None) -> str:
    """標記為 FAILED；有給 stage 就一併把該 job 記成 failed。"""
    if stage:
        finish_job(video_id, stage, "failed", error)
    _set_status(video_id, FAILED)
    return FAILED


def back_to_review(video_id: str) -> str:
    """從 REVIEW 之後的任一狀態退回 REVIEW。

    CLAUDE.md：REVIEW 是唯一的人工閘門，可以從任何後續狀態退回重跑。
    FAILED 也允許 —— 發布失敗後最常見的處置就是回審核台改內容再發一次。
    """
    current = get_status(video_id)
    if current != FAILED and _index(current) < _index(REVIEW):
        raise InvalidTransition(f"{video_id}: {current} has not reached REVIEW yet")
    _set_status(video_id, REVIEW)
    return REVIEW


def rerun_from(video_id: str, target: str) -> str:
    """退回較早的狀態以重跑該階段。

    只能往回，不能往前 —— 往前跳等於略過中間階段，那是 advance() 的職責。
    FAILED 例外：它不在主線上，退回任何階段都合法。
    """
    if target not in STATUSES or target in TERMINAL:
        raise InvalidTransition(f"cannot rerun from {target}")
    current = get_status(video_id)
    if current != FAILED and _index(target) > _index(current):
        raise InvalidTransition(f"{video_id}: {current} -> {target} is not a rollback")
    _set_status(video_id, target)
    return target


# --- jobs ---------------------------------------------------------------


def start_job(video_id: str, stage: str) -> None:
    """標記某階段開始執行；同一階段再跑一次就累加 retry_count。

    retry_count 從 0 起算（第一次執行不算重試），所以這裡是「已存在才 +1」。
    """
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs (video_id, stage, status, retry_count, error_detail,
                                 started_at, updated_at)
               VALUES (?, ?, 'running', 0, NULL, datetime('now'), datetime('now'))
               ON CONFLICT(video_id, stage) DO UPDATE SET
                   status = 'running',
                   retry_count = jobs.retry_count + 1,
                   error_detail = NULL,
                   updated_at = datetime('now')""",
            (video_id, stage),
        )


def finish_job(
    video_id: str, stage: str, status: str, error_detail: str | None = None
) -> None:
    with connect() as conn:
        conn.execute(
            """INSERT INTO jobs (video_id, stage, status, error_detail, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(video_id, stage) DO UPDATE SET
                   status = excluded.status,
                   error_detail = excluded.error_detail,
                   updated_at = datetime('now')""",
            (video_id, stage, status, error_detail),
        )


def list_jobs(video_id: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE video_id = ? ORDER BY id", (video_id,)
        ).fetchall()


@contextmanager
def run_stage(video_id: str, stage: str):
    """跑一個階段：先推進狀態，成功記 success，失敗記 failed 並把影片打成 FAILED。

    狀態在階段「開始時」就推進（DOWNLOADING 意思是「正在下載」而非「下載完」），
    所以中途掛掉時 videos.status 會停在 FAILED，而 jobs 留著是哪個 stage 失敗、
    錯誤內容是什麼，兩者合起來才知道該從哪重跑。
    """
    target = STAGE_STATUS.get(stage)
    if target is None:
        raise ValueError(f"unknown stage: {stage}")
    advance(video_id, target)
    start_job(video_id, stage)
    try:
        yield
    except Exception as exc:
        fail(video_id, f"{type(exc).__name__}: {exc}", stage=stage)
        raise
    finish_job(video_id, stage, "success")
