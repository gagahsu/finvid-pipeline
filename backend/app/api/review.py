"""逐字稿審核 API。

資料來源是 pipeline 寫進 SQLite 的 transcripts 與 corrections 兩張表。

一個要注意的地方：`transcripts.segments` 存的是**校正後**的 segments
（app/applier.py 寫入時就已經把修正套進去了），原始 segments 沒有另外保存。
所以左右對照的「原始」那一欄要靠 corrections 反推回去 —— 把該 segment 內
已套用的 (original, corrected) 反向替換回來，再拿還原結果跟 `raw_text`
比對確認無誤（結果放在 `raw_exact` 欄位，前端可據此提醒）。

右欄則不直接讀 `corrected_text`，而是依「目前的 status / human_reviewed」
即時重算。這樣人工按下接受／還原之後畫面立刻反映，不必等 pipeline 重跑。
"""

import json
import sqlite3

from fastapi import APIRouter, HTTPException, Query

from app.api.schemas import (
    Correction,
    CorrectionStatus,
    ReviewRequest,
    SegmentDiff,
    TranscriptDetail,
    VideoSummary,
)
from app.db import connect

router = APIRouter(prefix="/api", tags=["review"])


# --- 內部工具 -------------------------------------------------------------


def _is_applied(row: sqlite3.Row | dict) -> bool:
    """依目前狀態判斷這筆修正會不會被套進逐字稿。

    人工駁回（status=rejected 且 human_reviewed=1）一律不套用，優先於 auto。
    """
    if not row["corrected"]:
        return False
    if row["status"] == "rejected":
        return False
    return row["status"] == "auto" or bool(row["human_reviewed"])


def _parse_candidates(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _to_correction(row: sqlite3.Row) -> Correction:
    return Correction(
        id=row["id"],
        video_id=row["video_id"],
        segment_index=row["segment_index"],
        context=row["context"],
        original=row["original"],
        corrected=row["corrected"],
        confidence=row["confidence"],
        candidates=_parse_candidates(row["candidates"]),
        reason=row["reason"],
        status=row["status"],
        human_reviewed=bool(row["human_reviewed"]),
        created_at=row["created_at"],
        applied=_is_applied(row),
    )


def _video_titles(conn: sqlite3.Connection) -> dict[str, str]:
    """videos 表是後續階段才會有的，沒有就當作沒有標題。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'videos'"
    ).fetchone()
    if not exists:
        return {}
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "title" not in columns or "video_id" not in columns:
        return {}
    return {
        r["video_id"]: r["title"]
        for r in conn.execute("SELECT video_id, title FROM videos")
    }


def _fetch_corrections(conn: sqlite3.Connection, video_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM corrections
           WHERE video_id = ?
           ORDER BY segment_index IS NULL, segment_index, id""",
        (video_id,),
    ).fetchall()


def _reverse_apply(
    segments: list[dict], corrections: list[sqlite3.Row]
) -> list[str]:
    """把已套用的修正反向替換掉，還原成原始 segment 文字。

    同一句內較長的詞先還原，理由與 applier.py 相同：短詞先動會把長詞切壞。
    """
    by_segment: dict[int, list[tuple[str, str]]] = {}
    for row in corrections:
        if row["corrected"] and row["segment_index"] is not None:
            by_segment.setdefault(row["segment_index"], []).append(
                (row["original"], row["corrected"])
            )
    for pairs in by_segment.values():
        pairs.sort(key=lambda p: -len(p[1]))

    restored = []
    for idx, seg in enumerate(segments):
        text = seg.get("text", "")
        for original, corrected in by_segment.get(idx, []):
            if corrected in text:
                text = text.replace(corrected, original)
        restored.append(text)
    return restored


def _forward_apply(
    raw_segments: list[str], corrections: list[sqlite3.Row]
) -> tuple[list[str], dict[int, list[int]]]:
    """依目前狀態，把該套用的修正套回原始 segments。

    回傳（校正後文字, 每個 segment 實際套用到的 correction id）。
    """
    by_segment: dict[int, list[sqlite3.Row]] = {}
    for row in corrections:
        if _is_applied(row) and row["segment_index"] is not None:
            by_segment.setdefault(row["segment_index"], []).append(row)
    for rows in by_segment.values():
        rows.sort(key=lambda r: -len(r["original"]))

    result = []
    applied_ids: dict[int, list[int]] = {}
    for idx, text in enumerate(raw_segments):
        ids = []
        for row in by_segment.get(idx, []):
            if row["original"] in text:
                text = text.replace(row["original"], row["corrected"])
                ids.append(row["id"])
        result.append(text)
        if ids:
            applied_ids[idx] = ids
    return result, applied_ids


# --- 端點 -----------------------------------------------------------------


@router.get("/videos", response_model=list[VideoSummary])
def list_videos() -> list[VideoSummary]:
    """列出所有已有逐字稿的影片，附上審核進度統計。"""
    with connect() as conn:
        titles = _video_titles(conn)
        transcripts = conn.execute(
            """SELECT video_id, segments, applied_count, updated_at
               FROM transcripts ORDER BY updated_at DESC"""
        ).fetchall()

        stats = {
            r["video_id"]: r
            for r in conn.execute(
                """SELECT video_id,
                          COUNT(*) AS total,
                          SUM(status = 'auto') AS auto_count,
                          SUM(status = 'needs_review') AS needs_review_count,
                          SUM(status = 'rejected') AS rejected_count,
                          SUM(human_reviewed = 1) AS human_reviewed_count,
                          SUM(human_reviewed = 0 AND status != 'rejected') AS pending
                   FROM corrections GROUP BY video_id"""
            )
        }

    result = []
    for t in transcripts:
        try:
            segment_count = len(json.loads(t["segments"] or "[]"))
        except json.JSONDecodeError:
            segment_count = 0
        s = stats.get(t["video_id"])
        result.append(
            VideoSummary(
                video_id=t["video_id"],
                title=titles.get(t["video_id"]),
                updated_at=t["updated_at"],
                segment_count=segment_count,
                applied_count=t["applied_count"] or 0,
                total_corrections=s["total"] if s else 0,
                auto_count=s["auto_count"] if s else 0,
                needs_review_count=s["needs_review_count"] if s else 0,
                rejected_count=s["rejected_count"] if s else 0,
                human_reviewed_count=s["human_reviewed_count"] if s else 0,
                pending_count=s["pending"] if s else 0,
            )
        )
    return result


@router.get("/videos/{video_id}/transcript", response_model=TranscriptDetail)
def get_transcript(video_id: str) -> TranscriptDetail:
    """取得單支影片的逐字稿，segments 以左右對照的形式回傳。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM transcripts WHERE video_id = ?", (video_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"找不到逐字稿：{video_id}")
        corrections = _fetch_corrections(conn, video_id)
        title = _video_titles(conn).get(video_id)

    try:
        stored_segments = json.loads(row["segments"] or "[]")
    except json.JSONDecodeError:
        stored_segments = []

    raw_segments = _reverse_apply(stored_segments, corrections)
    corrected_segments, applied_ids = _forward_apply(raw_segments, corrections)

    # 哪些 segment 有待人工確認的建議（即使目前沒套用也要標出來）
    needs_review_segments = {
        r["segment_index"]
        for r in corrections
        if r["status"] == "needs_review" and not r["human_reviewed"]
    }

    segments = [
        SegmentDiff(
            index=idx,
            start=stored_segments[idx].get("start"),
            end=stored_segments[idx].get("end"),
            raw_text=raw_text,
            corrected_text=corrected_segments[idx],
            changed=raw_text != corrected_segments[idx],
            correction_ids=applied_ids.get(idx, []),
            has_needs_review=idx in needs_review_segments,
        )
        for idx, raw_text in enumerate(raw_segments)
    ]

    return TranscriptDetail(
        video_id=video_id,
        title=title,
        updated_at=row["updated_at"],
        applied_count=row["applied_count"] or 0,
        raw_text=row["raw_text"],
        corrected_text="".join(corrected_segments),
        raw_exact="".join(raw_segments) == (row["raw_text"] or ""),
        segments=segments,
    )


@router.get("/videos/{video_id}/corrections", response_model=list[Correction])
def list_corrections(
    video_id: str,
    status: CorrectionStatus | None = Query(
        default=None, description="只取指定 status 的修正"
    ),
    human_reviewed: bool | None = Query(
        default=None, description="只取（未）經人工覆核的修正"
    ),
) -> list[Correction]:
    """取得單支影片的修正清單。"""
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM transcripts WHERE video_id = ?", (video_id,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail=f"找不到逐字稿：{video_id}")
        rows = _fetch_corrections(conn, video_id)

    items = [_to_correction(r) for r in rows]
    if status is not None:
        items = [c for c in items if c.status == status]
    if human_reviewed is not None:
        items = [c for c in items if c.human_reviewed == human_reviewed]
    return items


@router.patch("/corrections/{correction_id}", response_model=Correction)
def review_correction(correction_id: int, body: ReviewRequest) -> Correction:
    """人工接受／駁回／重置一筆修正。

    accept 之後 applier 會把它套進逐字稿；reject 則永遠不套用，
    但整筆紀錄保留下來供事後稽核。
    """
    if body.action == "accept":
        status, reviewed = "auto", 1
    elif body.action == "reject":
        status, reviewed = "rejected", 1
    else:  # reset
        status, reviewed = "needs_review", 0

    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM corrections WHERE id = ?", (correction_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"找不到修正：{correction_id}")
        if body.action == "accept" and not row["corrected"]:
            raise HTTPException(
                status_code=400, detail="這筆修正沒有建議替換的內容，無法接受"
            )
        conn.execute(
            "UPDATE corrections SET status = ?, human_reviewed = ? WHERE id = ?",
            (status, reviewed, correction_id),
        )
        updated = conn.execute(
            "SELECT * FROM corrections WHERE id = ?", (correction_id,)
        ).fetchone()

    return _to_correction(updated)
