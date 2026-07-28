"""Pipeline 控制 API：從審核後台登記影片、觸發／重跑階段、看 jobs 失敗原因。

跟 review.py / summaries.py 的分工：那兩支是「內容」的 API（逐字稿、摘要），
這支是「流程」的 API —— 讀 videos/jobs 兩張表，寫入一律透過 app.pipeline
的狀態機函式，不自己 UPDATE videos.status。

執行本身不在這裡發生：轉錄要跑幾十分鐘，HTTP request 撐不住，所以 run 只是
把影片丟進 app.runner 的背景佇列後立刻回應。前端要看進度就輪詢 videos/queue。

路徑刻意都掛在 /api/pipeline 底下：review.py 已經佔用了 /api/videos，
那組回的是「有逐字稿的影片」，跟這裡的「videos 表全部」是不同集合。
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import pipeline
from app.cli import STAGES
from app.db import connect
from app.runner import NotRunnable, QueuedRun, get_runner, resolve_target

router = APIRouter(prefix="/api", tags=["pipeline"])

PipelineStatus = Literal[
    "PENDING",
    "DOWNLOADING",
    "TRANSCRIBING",
    "CORRECTING",
    "SUMMARIZING",
    "REVIEW",
    "RENDERING",
    "PUBLISHING",
    "PUBLISHED",
    "FAILED",
]
Stage = Literal["download", "transcribe", "correct", "apply", "summarize", "render"]


# --- 型別 -----------------------------------------------------------------


class JobInfo(BaseModel):
    stage: str
    status: str
    retry_count: int
    error_detail: str | None = None
    updated_at: str | None = None


class JobDetail(JobInfo):
    started_at: str | None = None


class VideoItem(BaseModel):
    video_id: str
    title: str | None = None
    url: str | None = None
    published_at: str | None = None
    status: str
    source_id: int | None = None
    source_name: str | None = None
    updated_at: str | None = None
    has_transcript: bool
    has_summary: bool
    card_count: int
    queue_state: Literal["idle", "queued", "running"]
    latest_job: JobInfo | None = None


class VideoDetail(VideoItem):
    jobs: list[JobDetail] = Field(default_factory=list)


class RegisterRequest(BaseModel):
    target: str
    title: str | None = None


class RegisterResponse(BaseModel):
    video_id: str
    status: str
    created: bool


class RunRequest(BaseModel):
    from_stage: Stage | None = None
    skip_judge: bool = False
    restart: bool = False


class RunResponse(BaseModel):
    video_id: str
    queued: bool
    status: str
    position: int


class RerunRequest(BaseModel):
    status: PipelineStatus


class StatusResponse(BaseModel):
    video_id: str
    status: str


class RunningInfo(BaseModel):
    video_id: str
    stage: str | None = None
    started_at: str


class RecentInfo(BaseModel):
    video_id: str
    stage: str
    status: str
    error_detail: str | None = None
    finished_at: str


class QueueResponse(BaseModel):
    running: RunningInfo | None = None
    queued: list[str] = Field(default_factory=list)
    recent: list[RecentInfo] = Field(default_factory=list)


# --- 內部工具 -------------------------------------------------------------

# 一次把三個「有沒有產出」的旗標算掉。分開查會變成 N+1，影片一多就明顯，
# 而這三個值前端每次列表都要用來決定按鈕能不能按。
_VIDEO_SQL = """
SELECT v.video_id, v.title, v.url, v.published_at, v.status, v.source_id,
       v.updated_at,
       s.name AS source_name,
       EXISTS(SELECT 1 FROM transcripts t WHERE t.video_id = v.video_id)
           AS has_transcript,
       EXISTS(SELECT 1 FROM summaries su WHERE su.video_id = v.video_id)
           AS has_summary,
       (SELECT COUNT(*) FROM media_assets m
          JOIN summaries su2 ON su2.id = m.summary_id
         WHERE su2.video_id = v.video_id) AS card_count
FROM videos v
LEFT JOIN sources s ON s.id = v.source_id
"""


def _latest_jobs(conn, video_id: str | None = None) -> dict[str, JobInfo]:
    """每支影片挑一筆「最近動過」的 job 當摘要用。

    jobs 一支影片一個 stage 只有一列（db.py 的設計），所以這裡不是取歷史最後
    一次執行，而是取目前最新被更新的那個階段 —— 剛好就是流程走到哪裡。
    """
    sql = "SELECT * FROM jobs"
    params: list = []
    if video_id is not None:
        sql += " WHERE video_id = ?"
        params.append(video_id)
    sql += " ORDER BY updated_at, id"
    latest: dict[str, JobInfo] = {}
    for r in conn.execute(sql, params):
        latest[r["video_id"]] = JobInfo(
            stage=r["stage"],
            status=r["status"],
            retry_count=r["retry_count"] or 0,
            error_detail=r["error_detail"],
            updated_at=r["updated_at"],
        )
    return latest


def _to_item(row, latest: JobInfo | None) -> VideoItem:
    return VideoItem(
        video_id=row["video_id"],
        title=row["title"],
        url=row["url"],
        published_at=row["published_at"],
        status=row["status"],
        source_id=row["source_id"],
        source_name=row["source_name"],
        updated_at=row["updated_at"],
        has_transcript=bool(row["has_transcript"]),
        has_summary=bool(row["has_summary"]),
        card_count=row["card_count"] or 0,
        queue_state=get_runner().state_of(row["video_id"]),
        latest_job=latest,
    )


# --- 端點 -----------------------------------------------------------------


@router.get("/pipeline/videos", response_model=list[VideoItem])
def list_videos() -> list[VideoItem]:
    """videos 表全部，最近更新在前。"""
    with connect() as conn:
        # updated_at 在舊 DB 遷移路徑上可能是 NULL（db.py 的 _add_columns 註解），
        # 讓它排到最後而不是被 SQLite 當成最小值排到最前面。
        rows = conn.execute(
            _VIDEO_SQL + " ORDER BY v.updated_at IS NULL, v.updated_at DESC"
        ).fetchall()
        latest = _latest_jobs(conn)
    return [_to_item(r, latest.get(r["video_id"])) for r in rows]


@router.get("/pipeline/videos/{video_id}", response_model=VideoDetail)
def get_video(video_id: str) -> VideoDetail:
    """單支影片，外加完整 jobs 清單（審核台要看是哪個階段失敗、錯在哪）。"""
    with connect() as conn:
        row = conn.execute(
            _VIDEO_SQL + " WHERE v.video_id = ?", (video_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"找不到影片：{video_id}")
        latest = _latest_jobs(conn, video_id).get(video_id)
        jobs = [
            JobDetail(
                stage=j["stage"],
                status=j["status"],
                retry_count=j["retry_count"] or 0,
                error_detail=j["error_detail"],
                started_at=j["started_at"],
                updated_at=j["updated_at"],
            )
            for j in conn.execute(
                "SELECT * FROM jobs WHERE video_id = ? ORDER BY id", (video_id,)
            )
        ]
    return VideoDetail(**_to_item(row, latest).model_dump(), jobs=jobs)


@router.post("/pipeline/videos", response_model=RegisterResponse, status_code=201)
def register_video(body: RegisterRequest) -> RegisterResponse:
    """登記一支影片，不開跑。

    只登記不執行，跟 cli poll 的理由一樣：轉錄很吃資源，要不要跑、什麼時候跑
    是另一個決定。已存在時不動它的 status（register_video 的既有語意）。
    """
    try:
        video_id, url = resolve_target(body.target.strip())
    except SystemExit as exc:
        # resolve_target 是 CLI 出身，解析不出來是拋 SystemExit
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not video_id:
        raise HTTPException(status_code=400, detail=f"無法解析 video_id：{body.target}")

    with connect() as conn:
        existed = conn.execute(
            "SELECT 1 FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()
    status = pipeline.register_video(video_id, url=url, title=body.title)
    return RegisterResponse(video_id=video_id, status=status, created=not existed)


@router.post("/pipeline/videos/{video_id}/run", response_model=RunResponse)
def run_video(video_id: str, body: RunRequest) -> RunResponse:
    """丟進背景佇列，立刻回應（不等執行完）。"""
    if body.from_stage is not None and body.from_stage not in STAGES:
        # render 不在主線上：它在 REVIEW 之後，只能由人在審核台單獨觸發
        raise HTTPException(
            status_code=400, detail=f"不能從 {body.from_stage} 階段開始"
        )
    runner = get_runner()
    try:
        queued, position = runner.submit(
            QueuedRun(
                video_id=video_id,
                from_stage=body.from_stage,
                skip_judge=body.skip_judge,
                restart=body.restart,
            )
        )
    except pipeline.UnknownVideo as exc:
        raise HTTPException(status_code=404, detail=f"找不到影片：{video_id}") from exc
    except (NotRunnable, pipeline.InvalidTransition) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunResponse(
        video_id=video_id,
        queued=queued,
        status=pipeline.get_status(video_id),
        position=position,
    )


@router.post("/pipeline/videos/{video_id}/rerun", response_model=StatusResponse)
def rerun_video(video_id: str, body: RerunRequest) -> StatusResponse:
    """退回較早的狀態以重跑該階段（只改狀態，不自動開跑）。"""
    try:
        status = pipeline.rerun_from(video_id, body.status)
    except pipeline.UnknownVideo as exc:
        raise HTTPException(status_code=404, detail=f"找不到影片：{video_id}") from exc
    except pipeline.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StatusResponse(video_id=video_id, status=status)


@router.post("/pipeline/videos/{video_id}/review", response_model=StatusResponse)
def send_back_to_review(video_id: str) -> StatusResponse:
    """從 REVIEW 之後的任一狀態退回人工審核。"""
    try:
        status = pipeline.back_to_review(video_id)
    except pipeline.UnknownVideo as exc:
        raise HTTPException(status_code=404, detail=f"找不到影片：{video_id}") from exc
    except pipeline.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StatusResponse(video_id=video_id, status=status)


@router.get("/pipeline/queue", response_model=QueueResponse)
def get_queue() -> QueueResponse:
    """背景佇列現況。全部來自 runner 的記憶體，重啟後歸零。"""
    snap = get_runner().snapshot()
    running = snap["running"]
    return QueueResponse(
        running=(
            RunningInfo(
                video_id=running.video_id,
                stage=running.stage,
                started_at=running.started_at,
            )
            if running
            else None
        ),
        queued=snap["queued"],
        recent=[
            RecentInfo(
                video_id=r.video_id,
                stage=r.stage,
                status=r.status,
                error_detail=r.error_detail,
                finished_at=r.finished_at,
            )
            for r in snap["recent"]
        ],
    )
