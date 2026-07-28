"""摘要審核 API。

資料來源是 SUMMARIZING 階段寫進 summaries 表的內容（app/summarizer.py）。

兩個設計決定：

1. **人工編輯存成新版本，不覆寫。** 這不是這支 API 自己發明的規則，是 db.py
   對 summaries 表的定義（`UNIQUE(video_id, version)`，「重跑摘要階段、或人工在
   審核台改完想留底，都是新增一列」）。所以存檔走 POST 而非 PATCH，版本號由
   `summarizer.next_version()` 取最大值 + 1，寫入直接重用 `summarizer.store()`，
   連帶把 posts 的三列 draft 也開好 —— 發布階段吃的是 summary_id，
   人工版本若沒有對應的 posts 列就等於發不出去。

2. **逐字重疊只回報、不擋。** summarizer 生成時是硬性失敗（model 不可信，
   照抄了就不能靜靜寫進 DB），但這裡送出的人是審核者本人，且審核閘門就是
   為了讓人做最終判斷。硬擋會出現「人明知那段是自己寫的、系統就是不讓存」
   的死結，所以改成把重疊片段放進回應的 `verbatim_overlap`，由前端提示。
"""

import sqlite3

from fastapi import APIRouter, HTTPException

from app.api.schemas import SummaryDetail, SummaryEditRequest, SummaryVersion
from app.db import connect
from app.summarizer import check_verbatim, store

router = APIRouter(prefix="/api", tags=["summaries"])

# 人工編輯版本在 model 欄位留的標記。沿用 model 欄位而不另外加欄位：
# 這一欄的語意就是「這份內容哪來的」，人工也是一種來源，不必動 schema。
HUMAN_MODEL = "human-edit"


# --- 內部工具 -------------------------------------------------------------


def _to_version(row: sqlite3.Row) -> SummaryVersion:
    return SummaryVersion(
        id=row["id"],
        video_id=row["video_id"],
        version=row["version"],
        title=row["title"],
        model=row["model"],
        created_at=row["created_at"],
        content_chars=len(row["content"] or ""),
        ig_caption_chars=len(row["ig_caption"] or ""),
        human_edited=row["model"] == HUMAN_MODEL,
    )


def _transcript_text(conn: sqlite3.Connection, video_id: str) -> str | None:
    """取校正後逐字稿，沒有就退回 raw_text（規則與 summarizer.load_transcript 一致）。"""
    row = conn.execute(
        "SELECT corrected_text, raw_text FROM transcripts WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    if row is None:
        return None
    return row["corrected_text"] or row["raw_text"]


def _to_detail(conn: sqlite3.Connection, row: sqlite3.Row) -> SummaryDetail:
    """組出完整版本內容，順便算一次逐字重疊。

    重疊是即時算的而不是存欄位：逐字稿本身可能因為修正被接受／還原而改變，
    存下來的檢查結果過一陣子就不再成立。
    """
    transcript = _transcript_text(conn, row["video_id"])
    overlap = None
    if transcript:
        for text in (row["content"] or "", row["ig_caption"] or ""):
            overlap = check_verbatim(text, transcript)
            if overlap:
                break

    base = _to_version(row)
    return SummaryDetail(
        **base.model_dump(),
        content=row["content"] or "",
        ig_caption=row["ig_caption"],
        verbatim_overlap=overlap,
    )


def _fetch(conn: sqlite3.Connection, summary_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM summaries WHERE id = ?", (summary_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"找不到摘要：{summary_id}")
    return row


# --- 端點 -----------------------------------------------------------------


@router.get("/videos/{video_id}/summaries", response_model=list[SummaryVersion])
def list_summaries(video_id: str) -> list[SummaryVersion]:
    """列出一支影片的所有摘要版本，最新的在前。

    沒有摘要不算錯誤，回空陣列即可 —— 影片可能只是還沒跑到 SUMMARIZING。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM summaries WHERE video_id = ? ORDER BY version DESC",
            (video_id,),
        ).fetchall()
    return [_to_version(r) for r in rows]


@router.get("/videos/{video_id}/summaries/latest", response_model=SummaryDetail)
def get_latest_summary(video_id: str) -> SummaryDetail:
    """取最新版本的完整內容。

    前端進畫面時不必先拉清單再拉內容，少一次來回。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM summaries WHERE video_id = ? ORDER BY version DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"這支影片還沒有摘要：{video_id}")
        return _to_detail(conn, row)


@router.get("/summaries/{summary_id}", response_model=SummaryDetail)
def get_summary(summary_id: int) -> SummaryDetail:
    """取單一版本的完整內容。"""
    with connect() as conn:
        return _to_detail(conn, _fetch(conn, summary_id))


@router.post("/videos/{video_id}/summaries", response_model=SummaryDetail, status_code=201)
def create_summary_version(video_id: str, body: SummaryEditRequest) -> SummaryDetail:
    """把人工編輯的內容存成新的一個版本。

    舊版本原封不動留著，這樣改壞了可以直接回去看上一版，也留下「人改了什麼」
    的稽核軌跡（與 corrections 表保留完整修正紀錄是同一個理由）。
    """
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM summaries WHERE video_id = ? LIMIT 1", (video_id,)
        ).fetchone()
        if exists is None:
            # 摘要階段還沒跑過就沒有東西可編輯。這裡不放行「憑空建立第一版」：
            # 標題／來源連結／免責聲明都是 summarizer 組出來的，繞過它會少東西。
            raise HTTPException(status_code=404, detail=f"這支影片還沒有摘要：{video_id}")

    title = (body.title or "").strip()
    if not title:
        # 標題留空就沿用最新版的，免得人只改正文卻把標題清掉
        with connect() as conn:
            row = conn.execute(
                "SELECT title FROM summaries WHERE video_id = ? ORDER BY version DESC LIMIT 1",
                (video_id,),
            ).fetchone()
        title = (row["title"] if row else None) or f"影片重點整理 {video_id}"

    summary_id = store(
        video_id=video_id,
        title=title,
        content=body.content,
        ig_caption=body.ig_caption or "",
        model=HUMAN_MODEL,
    )

    with connect() as conn:
        return _to_detail(conn, _fetch(conn, summary_id))
