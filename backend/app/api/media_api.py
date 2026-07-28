"""圖卡與發布 API。

三塊職責：

1. **圖卡** —— 列出 media_assets、把 PNG 本身餵給前端、觸發渲染（含不落 DB 的預覽）。
2. **發布** —— 呼叫 app/publisher.py，狀態機與草稿落地都在那裡，這層只做
   HTTP 轉譯（例外 → status code）。
3. **草稿** —— 給前端「複製全文」用。

檔案外送的安全界線（這支 API 唯一會把磁碟內容吐出去的地方）：

- 前端只能給 `asset_id`（DB 主鍵）或 preview token，**永遠不能給路徑**。
- media_assets.file_path 是相對於專案根目錄的字串，但 DB 欄位沒有約束，
  將來若有任何寫入端塞進 `../../` 或絕對路徑，這支端點就會變成任意檔案讀取。
  所以解析出絕對路徑後一律再驗證它落在專案 data/ 底下，不在就當作找不到。
- preview 的圖同樣渲染到 data/ 底下（不是系統 temp），讓上面那條檢查對兩條
  路徑一體適用，不必為預覽開後門。

Pydantic model 就近定義在本檔：app/api/schemas.py 是審核後台既有的共用模型，
這些是新功能自己的形狀，混進去只會讓兩邊互相牽動。
"""

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel

from app import publisher, renderer
from app.db import connect
from app.pipeline import InvalidTransition, UnknownVideo
from app.renderer import FontNotFound, NoSummary

router = APIRouter(prefix="/api", tags=["media"])

PROJECT_ROOT = renderer.PROJECT_ROOT
DATA_DIR = (PROJECT_ROOT / "data").resolve()

# 預覽圖放這裡而不是系統 temp：目錄穿越檢查只認 data/ 底下的路徑，
# 預覽若走系統 temp 就得額外開一個例外，那個例外正是最容易被寫錯的地方。
PREVIEW_DIR = DATA_DIR / "cards" / "_preview"

PREVIEW_TTL = 30 * 60  # 秒；合約規定 token 保留 30 分鐘

# token → (檔案路徑, 到期時間)。純記憶體，重啟就沒了 —— 預覽本來就是一次性的，
# 存 DB 只會多一堆沒人清的垃圾列。
_previews: dict[str, tuple[Path, float]] = {}


# --- 回應模型 -------------------------------------------------------------

Platform = Literal["instagram", "vocus", "medium"]
DraftPlatform = Literal["vocus", "medium"]
PostStatus = Literal["published", "ready", "draft", "failed"]


class Card(BaseModel):
    """一張圖卡。預覽模式沒有 media_assets 列，所以 id 可為 null。"""

    id: int | None = None
    type: str
    file_path: str
    width: int | None = None
    height: int | None = None
    url: str


class CardSet(BaseModel):
    video_id: str
    summary_id: int
    version: int
    cards: list[Card]


class RenderRequest(BaseModel):
    version: int | None = None
    preview: bool = False


class Post(BaseModel):
    id: int
    summary_id: int
    platform: str
    status: str
    external_url: str | None = None
    published_at: str | None = None
    error: str | None = None
    updated_at: str | None = None


class PublishRequest(BaseModel):
    platforms: list[Platform] = ["vocus", "medium", "instagram"]
    version: int | None = None


class PostPatchRequest(BaseModel):
    status: PostStatus | None = None
    external_url: str | None = None
    error: str | None = None


class Draft(BaseModel):
    platform: str
    content: str
    file_path: str | None = None


# --- 內部工具 -------------------------------------------------------------


def _rel(path: Path) -> str:
    """轉成相對專案根目錄的 posix 路徑（media_assets.file_path 的格式）。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def safe_media_path(file_path: str) -> Path | None:
    """把 media_assets.file_path 解析成絕對路徑，並確認它仍在專案 data/ 底下。

    回傳 None 代表「不可外送」，呼叫端一律當 404 處理，不告訴前端是路徑
    被擋下還是檔案不存在 —— 對外區分這兩者等於幫人探測檔案系統。

    `..` 之類的相對片段由 resolve() 攤平後才比對，所以字串裡怎麼繞都沒用。
    """
    if not file_path:
        return None
    candidate = Path(file_path)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(DATA_DIR):
        return None
    return resolved


def _latest_summary(conn: sqlite3.Connection, video_id: str, version: int | None) -> sqlite3.Row:
    sql = "SELECT * FROM summaries WHERE video_id = ?"
    params: list = [video_id]
    if version is not None:
        sql += " AND version = ?"
        params.append(version)
    sql += " ORDER BY version DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    if row is None:
        detail = f"這支影片還沒有摘要：{video_id}"
        if version is not None:
            detail = f"找不到摘要版本：{video_id} v{version}"
        raise HTTPException(status_code=404, detail=detail)
    return row


def _cards_of(conn: sqlite3.Connection, summary_id: int) -> list[Card]:
    rows = conn.execute(
        "SELECT * FROM media_assets WHERE summary_id = ? ORDER BY id", (summary_id,)
    ).fetchall()
    return [
        Card(
            id=r["id"],
            type=r["type"],
            file_path=r["file_path"],
            width=r["width"],
            height=r["height"],
            url=f"/api/media/{r['id']}",
        )
        for r in rows
    ]


def _to_post(row: sqlite3.Row) -> Post:
    return Post(
        id=row["id"],
        summary_id=row["summary_id"],
        platform=row["platform"],
        status=row["status"],
        external_url=row["external_url"],
        published_at=row["published_at"],
        error=row["error"],
        updated_at=row["updated_at"],
    )


def _sweep_previews() -> None:
    """清掉過期的預覽 token 與它們的圖檔。

    沒有背景排程，就在每次產生新預覽時順手掃一遍 —— 預覽是低頻操作，
    多掃幾次的成本遠低於為它多養一個 scheduler。
    """
    now = time.time()
    expired = [t for t, (_, exp) in _previews.items() if exp <= now]
    for token in expired:
        path, _ = _previews.pop(token)
        # 同一次渲染的多張圖共用一個目錄，目錄空了才刪
        try:
            path.unlink(missing_ok=True)
            parent = path.parent
            if parent.is_relative_to(PREVIEW_DIR) and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


def _register_preview(path: Path) -> str:
    token = secrets.token_urlsafe(16)
    _previews[token] = (path.resolve(), time.time() + PREVIEW_TTL)
    return token


# --- 圖卡 -----------------------------------------------------------------


@router.get("/videos/{video_id}/cards", response_model=CardSet)
def get_cards(video_id: str, version: int | None = Query(default=None)) -> CardSet:
    """列出已渲染的圖卡。沒摘要 → 404；有摘要但沒渲染 → cards 空陣列。"""
    with connect() as conn:
        summary = _latest_summary(conn, video_id, version)
        cards = _cards_of(conn, summary["id"])
    return CardSet(
        video_id=video_id,
        summary_id=summary["id"],
        version=summary["version"],
        cards=cards,
    )


@router.get("/media/preview/{token}")
def get_preview(token: str):
    """回傳預覽圖。token 由 render 端點發放，前端不能自己構造路徑。"""
    entry = _previews.get(token)
    if entry is None:
        raise HTTPException(status_code=404, detail="預覽已過期或不存在")
    path, expires = entry
    if expires <= time.time():
        _previews.pop(token, None)
        raise HTTPException(status_code=404, detail="預覽已過期或不存在")
    # token 是自己發的，但檔案路徑仍照同一條規則驗一次：發放與取用之間
    # 隔了 30 分鐘，中間任何東西動過這個 dict 都不該讓檔案外流
    safe = safe_media_path(str(path))
    if safe is None or not safe.is_file():
        raise HTTPException(status_code=404, detail="預覽已過期或不存在")
    return FileResponse(safe, media_type="image/png")


@router.get("/media/{asset_id}")
def get_media(asset_id: int):
    """回傳 media_assets 指向的 PNG 本身。

    路徑一律由 DB 取得後再驗證落在 data/ 底下（見 safe_media_path），
    query 裡任何路徑參數都不接受。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM media_assets WHERE id = ?", (asset_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"找不到圖卡：{asset_id}")
    path = safe_media_path(row["file_path"])
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail=f"圖卡檔案不存在：{asset_id}")
    return FileResponse(path, media_type="image/png")


@router.post("/videos/{video_id}/cards/render", response_model=CardSet)
def render_cards(video_id: str, body: RenderRequest) -> CardSet:
    """渲染圖卡。Pillow 只要幾秒，同步跑即可，不必為它開背景佇列。

    preview=True 走 store=False：只出圖不動 media_assets，讓人在正式覆蓋
    現有圖卡之前先看一眼。
    """
    try:
        if body.preview:
            _sweep_previews()
            out_dir = PREVIEW_DIR / secrets.token_hex(8)
            paths = renderer.render(
                video_id, body.version, out_dir=out_dir, store=False
            )
        else:
            paths = renderer.render(video_id, body.version)
    except NoSummary as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FontNotFound as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with connect() as conn:
        summary = _latest_summary(conn, video_id, body.version)
        if not body.preview:
            cards = _cards_of(conn, summary["id"])

    if body.preview:
        cards = []
        for path in paths:
            with Image.open(path) as im:
                w, h = im.size
            token = _register_preview(path)
            cards.append(
                Card(
                    id=None,
                    # type 沿用檔名後半段（renderer 的 `{idx}_{type}.png` 命名）
                    type=path.stem.split("_", 1)[-1],
                    file_path=_rel(path),
                    width=w,
                    height=h,
                    url=f"/api/media/preview/{token}",
                )
            )

    return CardSet(
        video_id=video_id,
        summary_id=summary["id"],
        version=summary["version"],
        cards=cards,
    )


# --- 發布 -----------------------------------------------------------------


@router.get("/videos/{video_id}/posts", response_model=list[Post])
def get_posts(video_id: str) -> list[Post]:
    """回傳最新版摘要的三個平台發布狀態。

    只看最新版：舊版本的 posts 仍留在表裡（版本是不覆寫的），全部回傳的話
    前端會看到同一個平台出現好幾列，分不清哪一列才是現在要發的那份。
    還沒有摘要不算錯誤，回空陣列。
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM summaries WHERE video_id = ? ORDER BY version DESC LIMIT 1",
            (video_id,),
        ).fetchone()
    if row is None:
        return []
    return [_to_post(r) for r in publisher.list_posts(row["id"])]


@router.post("/videos/{video_id}/publish", response_model=list[Post])
def publish_video(video_id: str, body: PublishRequest) -> list[Post]:
    """把影片推進 PUBLISHING 並產出各平台的發布物。

    Instagram 不會真的送出去 —— 理由見 app/publisher.py 模組說明。
    """
    try:
        result = publisher.publish(video_id, list(body.platforms), body.version)
    except NoSummary as exc:
        raise HTTPException(status_code=404, detail=f"這支影片還沒有摘要：{exc}") from exc
    except UnknownVideo as exc:
        raise HTTPException(status_code=404, detail=f"找不到影片：{exc}") from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except publisher.UnknownPlatform as exc:
        raise HTTPException(status_code=400, detail=f"不支援的平台：{exc}") from exc
    return [_to_post(r) for r in result["posts"]]


@router.patch("/posts/{post_id}", response_model=Post)
def patch_post(post_id: int, body: PostPatchRequest) -> Post:
    """人工回填發布結果。三個平台都變成 published 時自動推進影片到 PUBLISHED。

    用 model_fields_set 判斷「這次到底送了哪些欄位」：external_url=null 可能是
    「清掉連結」，跟「沒帶這個欄位」是兩件事，只看值分不出來。
    """
    fields = set(body.model_fields_set)
    row = publisher.update_post(
        post_id,
        status=body.status,
        external_url=body.external_url,
        error=body.error,
        fields=fields,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"找不到發布紀錄：{post_id}")
    return _to_post(row)


@router.get("/videos/{video_id}/drafts/{platform}", response_model=Draft)
def get_draft(
    video_id: str, platform: str, version: int | None = Query(default=None)
) -> Draft:
    """回傳 Vocus／Medium 草稿全文，給前端「複製全文」按鈕用。

    Instagram 不在這裡：它的產出是圖卡不是 Markdown，走 cards 端點。
    """
    try:
        content, path = publisher.read_draft(video_id, platform, version)
    except publisher.UnknownPlatform as exc:
        raise HTTPException(
            status_code=400, detail=f"只有 vocus / medium 有 Markdown 草稿：{exc}"
        ) from exc
    except NoSummary as exc:
        raise HTTPException(status_code=404, detail=f"這支影片還沒有摘要：{exc}") from exc
    return Draft(
        platform=platform,
        content=content,
        file_path=_rel(path) if path else None,
    )
