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


# --- 摘要 -----------------------------------------------------------------


class SummaryVersion(BaseModel):
    """摘要版本清單的一列。

    只帶「挑版本」需要的資訊，正文與 IG 文案要另外抓單一版本才會回傳：
    一支影片重跑幾次就有幾個版本，每個版本的 content 動輒數千字，
    全部塞進清單會讓選單頁面白等。字數改用 *_chars 先給個規模感。
    """

    id: int
    video_id: str
    version: int
    title: str | None = None
    model: str | None = None
    created_at: str | None = None
    content_chars: int = 0
    ig_caption_chars: int = 0
    # 這個版本是不是人工在審核台編輯後存下來的（依 model 欄位判斷）
    human_edited: bool = False


class SummaryDetail(SummaryVersion):
    """單一版本的完整內容。"""

    content: str
    ig_caption: str | None = None
    # 與逐字稿的逐字重疊片段（沒有就是 None）。人工存檔時不擋，只回報，
    # 理由見 app/api/summaries.py 的模組註解。
    verbatim_overlap: str | None = None


class SummaryEditRequest(BaseModel):
    """人工編輯後的存檔內容。

    依 db.py 的 summaries 註解，人工改完是「新增一個 version」而非覆寫，
    所以這裡不需要帶 version —— 由後端取目前最大值 + 1。
    """

    title: str | None = None
    content: str = Field(min_length=1)
    ig_caption: str | None = None
