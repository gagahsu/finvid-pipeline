"""訂閱來源管理 API。

對應 CLAUDE.md 的「YouTube 來源監控」：sources 表存的是 RSS feed 的訂閱設定，
真正的抓取邏輯全部在 app/rss.py，這支只負責 CRUD 與「手動觸發一次」的入口，
不重寫任何 feed 解析或 yt-dlp 呼叫（rss.py 對這支模組是唯讀依賴）。

三個設計決定：

1. **輪詢失敗回 200 帶 error，不回 500。** rss.SourceResult 本來就把單一來源的
   失敗當成正常結果（一個頻道掛掉不該拖垮整輪），這支 API 照樣傳遞這個語意 ——
   前端要顯示得出「這個來源這輪失敗了」，而不是整頁跳錯誤。

2. **poll 端點是慢的，而且不打算讓它變快。** rss.fetch_feed 預設重試 10 次，
   因為 YouTube feeds server 會對合法 channel_id 間歇回 404/500（實測連打 8 次
   才拿到第一個 200）。最壞情況一個來源要 40 秒左右。把重試調小只會換來
   「好好的訂閱莫名其妙漏掉新片」，所以這裡不動它，由前端負責提示使用者等待。

3. **DELETE 只在沒有影片時放行。** videos.source_id 是指向 sources(id) 的外鍵，
   SQLite 預設沒開 foreign_keys 強制，硬刪會留下指到不存在來源的孤兒影片，
   之後 join 出來的 source_name 全變 null。有影片時回 409，建議改用
   PATCH active=false（停止輪詢但保留關聯）。
"""

import re
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import pipeline, rss
from app.db import connect, init_schema

router = APIRouter(prefix="/api", tags=["sources"])

# yt-dlp 解析 handle/網址的逾時。這是一次性的 metadata 查詢（--flat-playlist
# 只列 id 不解析串流），正常兩三秒內回來；給 60 秒是留給網路慢的情況，
# 但一定要有上限 —— 沒 timeout 的 subprocess 會讓整個 HTTP request 無限期卡住。
RESOLVE_TIMEOUT_SECONDS = 60

# 直接可用的 id 形式。UC 開頭是頻道，PL 開頭是播放清單（CLAUDE.md 只列這兩種）。
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PLAYLIST_ID_RE = re.compile(r"^PL[A-Za-z0-9_-]{16,}$")

# 從網址直接摳得出來的兩種形式，能免掉一次 yt-dlp 呼叫。
URL_CHANNEL_RE = re.compile(r"/channel/(UC[A-Za-z0-9_-]{22})")
URL_PLAYLIST_RE = re.compile(r"[?&]list=(PL[A-Za-z0-9_-]{16,})")


# --- 請求／回應模型 -------------------------------------------------------
#
# 刻意定義在本檔而不是 app/api/schemas.py：這些型別只有這支 API 用得到，
# 放進共用 schemas 只會增加跟其他 API 的耦合。


class SourceOut(BaseModel):
    """GET / PATCH 回傳的完整來源，附上影片統計與 feed URL 方便前端直接連過去。"""

    id: int
    channel_id: str
    type: str
    name: str | None = None
    active: bool
    created_at: str | None = None
    video_count: int = 0
    latest_video_published_at: str | None = None
    feed_url: str | None = None


class SourceCreated(BaseModel):
    """POST 的回應。只回身分欄位，統計數字這時候一定是 0，沒有意義。"""

    id: int
    channel_id: str
    type: str
    name: str | None = None
    active: bool


class SourceCreateRequest(BaseModel):
    target: str
    type: str = "channel"
    name: str | None = None


class SourceUpdateRequest(BaseModel):
    """PATCH：只改有給的欄位，靠 model_fields_set 分辨「沒給」與「給了 null」。"""

    active: bool | None = None
    name: str | None = None


class PollRequest(BaseModel):
    max_new: int | None = None


class PollResult(BaseModel):
    """對應 rss.SourceResult。error 有值代表這輪失敗，其餘欄位是空的。"""

    channel_id: str
    name: str | None = None
    total: int = 0
    new_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class PollAllResult(BaseModel):
    results: list[PollResult] = Field(default_factory=list)
    new_ids: list[str] = Field(default_factory=list)


class BackfillRequest(BaseModel):
    limit: int = rss.DEFAULT_BACKFILL_LIMIT


class BackfillResult(BaseModel):
    added: list[str] = Field(default_factory=list)
    count: int = 0


# --- 內部工具 -------------------------------------------------------------


def _feed_url(channel_id: str, type_: str | None) -> str | None:
    """組 feed URL；type 是舊資料留下的怪值時回 null 而不是讓整個列表爆掉。"""
    try:
        return rss.feed_url(channel_id, type_ or "channel")
    except ValueError:
        return None


def _select_sources(where: str = "", params: tuple = ()) -> list[SourceOut]:
    """讀 sources 並帶上影片統計。

    用相關子查詢而不是 LEFT JOIN + GROUP BY：來源數量是個位數，
    子查詢讀起來直接，也不用處理 join 後 count 掉到 0 的 null 判斷。
    """
    init_schema()
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT s.id, s.channel_id, s.type, s.name, s.active, s.created_at,
                       (SELECT COUNT(*) FROM videos v WHERE v.source_id = s.id)
                           AS video_count,
                       (SELECT MAX(v.published_at) FROM videos v WHERE v.source_id = s.id)
                           AS latest_video_published_at
                FROM sources s
                {where}
                ORDER BY s.id""",
            params,
        ).fetchall()
    return [
        SourceOut(
            id=r["id"],
            channel_id=r["channel_id"],
            type=r["type"] or "channel",
            name=r["name"],
            active=bool(r["active"]),
            created_at=r["created_at"],
            video_count=r["video_count"] or 0,
            latest_video_published_at=r["latest_video_published_at"],
            feed_url=_feed_url(r["channel_id"], r["type"]),
        )
        for r in rows
    ]


def _get_source_or_404(source_id: int) -> SourceOut:
    items = _select_sources("WHERE s.id = ?", (source_id,))
    if not items:
        raise HTTPException(status_code=404, detail=f"找不到訂閱來源：{source_id}")
    return items[0]


def _resolve_via_ytdlp(url: str) -> str:
    """用 yt-dlp 把頻道網址／@handle 解析成 channel_id。

    寫法比照 rss.fetch_history：走 subprocess 而不是 import yt_dlp，
    ``--flat-playlist`` 不解析串流所以很快，``--playlist-end 1`` 只抓一筆就夠。

    template 前綴一定要是 ``playlist:``：@handle 網址在 yt-dlp 眼中是一個
    「頻道分頁」playlist，channel_id 只存在於 playlist 層，entry 層印出來是 NA
    （實測 ``--print "%(channel_id)s"`` 對 @GoogleDevelopers 回的就是 NA）。
    巢狀分頁會印出多行，取第一行長得像 channel_id 的即可。

    一定要帶 timeout：這是 HTTP request 執行緒裡跑的外部程序，
    yt-dlp 卡在網路上不回來的話會把整個連線一起吊死。
    """
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--playlist-end",
        "1",
        "--print",
        "playlist:%(channel_id)s",
        url,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=RESOLVE_TIMEOUT_SECONDS
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "yt-dlp 不在 PATH 上，無法解析頻道網址／handle；"
            "請直接提供 UC... 開頭的 channel_id"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"yt-dlp 解析逾時（{RESOLVE_TIMEOUT_SECONDS}s）：{url}") from exc
    if proc.returncode != 0:
        raise ValueError(f"yt-dlp 解析失敗（{proc.returncode}）：{proc.stderr.strip()[:300]}")

    for line in proc.stdout.splitlines():
        candidate = line.strip()
        if CHANNEL_ID_RE.match(candidate):
            return candidate
    raise ValueError(f"yt-dlp 沒有回傳可用的 channel_id：{url}")


def _resolve_target(target: str, type_: str) -> str:
    """把使用者輸入的 target 正規化成 channel_id / playlist_id。

    容許三種輸入（合約 C 節）：直接的 UC.../PL... id、頻道或播放清單網址、
    以及 @handle。前兩種能純字串處理就不要動用 yt-dlp —— 少一個外部相依、
    少一次網路往返，也就少一種失敗模式。
    """
    target = (target or "").strip()
    if not target:
        raise ValueError("target 不可為空")
    if type_ not in rss.FEED_PARAM:
        raise ValueError(f"unknown source type: {type_}（只接受 channel / playlist）")

    if CHANNEL_ID_RE.match(target) or PLAYLIST_ID_RE.match(target):
        return target

    if type_ == "playlist":
        # 播放清單一律只從網址的 list= 參數取，不丟給 yt-dlp：
        # yt-dlp 對播放清單回的是所屬頻道的 channel_id，拿去當 playlist_id
        # 會組出一個永遠抓不到東西的 feed URL，錯得很難察覺。
        match = URL_PLAYLIST_RE.search(target)
        if match:
            return match.group(1)
        raise ValueError(f"無法從 target 解析出播放清單 id（PL... 或含 list= 的網址）：{target}")

    match = URL_CHANNEL_RE.search(target)
    if match:
        return match.group(1)

    # 剩下的是 @handle、/c/xxx、/user/xxx 這類要真的去查才知道的形式。
    url = target
    if target.startswith("@"):
        url = f"https://www.youtube.com/{target}"
    elif not target.startswith(("http://", "https://", "www.")):
        raise ValueError(f"無法辨識的 target 格式：{target}（請給 UC... / 頻道網址 / @handle）")
    return _resolve_via_ytdlp(url)


def _to_poll_result(result: rss.SourceResult) -> PollResult:
    return PollResult(
        channel_id=result.channel_id,
        name=result.name,
        total=result.total,
        new_ids=list(result.new_ids),
        error=result.error,
    )


def _poll_one(source: SourceOut, max_new: int | None) -> PollResult:
    """輪詢單一來源，任何例外都收斂成 error 欄位。

    rss.poll_source 已經把 FeedError/ValueError 吃掉了，這層再包一次是為了
    網路層以外的意外（例如 DB 被鎖住）也不要變成 500 —— 合約要求前端
    看得到失敗訊息，而不是收到一個沒有內容的錯誤頁。
    """
    payload = {
        "id": source.id,
        "channel_id": source.channel_id,
        "type": source.type,
        "name": source.name,
    }
    try:
        return _to_poll_result(rss.poll_source(payload, max_new=max_new))
    except Exception as exc:  # noqa: BLE001 - 刻意攔全部，見 docstring
        return PollResult(
            channel_id=source.channel_id,
            name=source.name,
            error=f"{type(exc).__name__}: {exc}",
        )


# --- 端點 -----------------------------------------------------------------


@router.get("/sources", response_model=list[SourceOut])
def list_sources() -> list[SourceOut]:
    """列出所有訂閱來源（含停用的）。"""
    return _select_sources()


@router.post("/sources", response_model=SourceCreated, status_code=201)
def create_source(body: SourceCreateRequest) -> SourceCreated:
    """新增訂閱來源。target 可以是 UC.../PL... id、頻道網址或 @handle。"""
    try:
        channel_id = _resolve_target(body.target, body.type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 重用 pipeline.upsert_source：同一個 channel_id 重複新增時更新既有列而非
    # 噴 UNIQUE 錯誤，跟 CLI 的行為一致。
    source_id = pipeline.upsert_source(channel_id, name=body.name, type_=body.type)
    source = _get_source_or_404(source_id)
    return SourceCreated(
        id=source.id,
        channel_id=source.channel_id,
        type=source.type,
        name=source.name,
        active=source.active,
    )


@router.patch("/sources/{source_id}", response_model=SourceOut)
def update_source(source_id: int, body: SourceUpdateRequest) -> SourceOut:
    """改名／啟停。只更新請求裡真的有出現的欄位。"""
    _get_source_or_404(source_id)

    fields = body.model_fields_set
    assignments: list[str] = []
    params: list[object] = []
    if "active" in fields and body.active is not None:
        assignments.append("active = ?")
        params.append(1 if body.active else 0)
    if "name" in fields:
        assignments.append("name = ?")
        params.append(body.name)

    if assignments:
        params.append(source_id)
        with connect() as conn:
            conn.execute(
                f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?", params
            )
    return _get_source_or_404(source_id)


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: int) -> None:
    """硬刪來源。底下還有影片時擋下來（見模組 docstring 的孤兒指標問題）。"""
    source = _get_source_or_404(source_id)
    if source.video_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"這個來源底下還有 {source.video_count} 支影片，刪除會讓它們的 "
                "source_id 變成孤兒指標。請改用 PATCH active=false 停用。"
            ),
        )
    with connect() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


@router.post("/sources/poll", response_model=PollAllResult)
def poll_all_sources(body: PollRequest | None = None) -> PollAllResult:
    """輪詢全部 active 來源。

    **這支很慢**：每個來源最壞要 40 秒（rss.fetch_feed 的重試策略），
    N 個來源就是 N 倍。前端請當成長時間動作處理。
    """
    max_new = body.max_new if body else None
    active = [s for s in _select_sources("WHERE s.active = 1")]
    results = [_poll_one(s, max_new) for s in active]
    return PollAllResult(
        results=results,
        new_ids=[vid for r in results for vid in r.new_ids],
    )


@router.post("/sources/{source_id}/poll", response_model=PollResult)
def poll_source(source_id: int, body: PollRequest | None = None) -> PollResult:
    """只輪詢這一個來源（停用的來源也可以手動輪詢，方便測試新增的訂閱）。

    抓取失敗不會回 500，訊息在 error 欄位。回應時間最壞約 40 秒。
    """
    source = _get_source_or_404(source_id)
    return _poll_one(source, body.max_new if body else None)


@router.post("/sources/{source_id}/backfill", response_model=BackfillResult)
def backfill(source_id: int, body: BackfillRequest | None = None) -> BackfillResult:
    """用 yt-dlp 補歷史影片。RSS 只回最新約 15 支，新訂閱的來源要靠這個補完。

    yt-dlp 不在 PATH、逾時或執行失敗都回 400（環境問題，不是伺服器壞掉），
    detail 帶 rss.FeedError 的原訊息方便排查。
    """
    source = _get_source_or_404(source_id)
    limit = body.limit if body else rss.DEFAULT_BACKFILL_LIMIT
    try:
        added = rss.backfill_source(
            source.channel_id,
            type_=source.type,
            limit=limit,
            source_id=source.id,
        )
    except (rss.FeedError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
    return BackfillResult(added=added, count=len(added))
