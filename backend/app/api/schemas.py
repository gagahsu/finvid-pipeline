"""審核 API 的回應／請求模型。

只描述對外的資料形狀，資料庫存取一律走 app/db.py 的原生 sqlite3 連線。
"""

from typing import Literal

from pydantic import BaseModel, Field

# corrections.status 的三種值，語意見 app/db.py 的 schema 註解
CorrectionStatus = Literal["auto", "needs_review", "rejected"]

# 人工審核可以做的動作
#   accept 採用這筆修正（status=auto、human_reviewed=1）
#   reject 還原／不採用（status=rejected、human_reviewed=1）
#   reset  取消人工判定，退回 needs_review 讓人重看
ReviewAction = Literal["accept", "reject", "reset"]


class VideoSummary(BaseModel):
    """影片清單的一列，帶上審核進度統計。"""

    video_id: str
    title: str | None = None
    updated_at: str | None = None
    segment_count: int = 0
    applied_count: int = 0
    total_corrections: int = 0
    auto_count: int = 0
    needs_review_count: int = 0
    rejected_count: int = 0
    human_reviewed_count: int = 0
    pending_count: int = 0  # 還沒被人看過、且不是 rejected 的數量


class Candidate(BaseModel):
    """rapidfuzz 比對出來的候選股票。"""

    symbol: str | None = None
    name_zh: str | None = None
    score: float | None = None


class Correction(BaseModel):
    id: int
    video_id: str
    segment_index: int | None = None
    context: str | None = None
    original: str
    corrected: str | None = None
    confidence: float | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    reason: str | None = None
    status: CorrectionStatus
    human_reviewed: bool = False
    created_at: str | None = None
    applied: bool = False  # 依目前狀態，這筆是否會被套進逐字稿


class SegmentDiff(BaseModel):
    """單一 segment 的左右對照。"""

    index: int
    start: float | None = None
    end: float | None = None
    raw_text: str
    corrected_text: str
    changed: bool
    correction_ids: list[int] = Field(default_factory=list)
    has_needs_review: bool = False


class TranscriptDetail(BaseModel):
    video_id: str
    title: str | None = None
    updated_at: str | None = None
    applied_count: int = 0
    raw_text: str
    corrected_text: str
    raw_exact: bool  # 還原出來的原始 segments 是否與 raw_text 完全吻合
    segments: list[SegmentDiff]


class ReviewRequest(BaseModel):
    action: ReviewAction
