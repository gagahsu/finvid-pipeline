"""發布階段（PUBLISHING）的業務邏輯，不含 HTTP。

三個平台走三條不同的路，但都不會真的把東西送出去：

- vocus / medium：官方都沒有可用的發文 API（CLAUDE.md 已知限制），
  能做的就是把 summaries.content 落地成 `data/drafts/{video_id}_{platform}.md`，
  posts.status 設 `ready` 表示「草稿備妥、等人貼上」，external_url 由人工回填。
- instagram：**刻意不呼叫 Graph API**。Graph API 建立 media container 時只吃
  `image_url`，而且那個 URL 必須是 Meta 的伺服器連得到的公開網址；純本機沒有
  對外主機可以放圖，要嘛架 tunnel 要嘛把圖丟上雲端儲存 —— 兩者都違反
  CLAUDE.md「純本機、不上雲」的核心原則。所以這裡只做「圖卡是否已渲染」的
  確認：有圖就標 `ready`，沒圖就標 `blocked` 並把原因寫進 error 欄位。
  絕不能改成假裝發成功 —— posts.status=published 是「這篇真的在線上」的意思，
  騙自己的狀態會讓事後對帳完全失效。

posts.status 的語意（這張表沒有 CHECK 約束，語意由這個模組定義）：

    draft     summarizer 開好的初始列，什麼都還沒做
    ready     產出物已備妥，等人工執行最後一步（貼上 / 手動發圖）
    blocked   前置條件不成立（例如圖卡還沒渲染），error 欄位寫明原因
    published 人工確認真的發出去了，external_url 通常有值
    failed    嘗試過但失敗

三個平台都變成 published 時，影片才 advance 到 PUBLISHED —— 發布完成的定義是
「三個通路都上線」，少一個就還沒完。
"""

import sqlite3
from pathlib import Path

from app import pipeline
from app.db import connect
# NoSummary 一併帶出來：呼叫端（API 層）要接的是「這支影片沒摘要」這件事，
# 不該為了一個例外型別再去 import renderer
from app.renderer import NoSummary, load_summary

__all__ = [
    "NoSummary",
    "UnknownPlatform",
    "PLATFORMS",
    "DRAFT_PLATFORMS",
    "resolve_summary",
    "export_draft",
    "read_draft",
    "list_posts",
    "get_post",
    "update_post",
    "publish",
    "maybe_finish",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DRAFTS_DIR = DATA_DIR / "drafts"

# posts 表固定就這三列（summarizer.store 開的），順序即前端顯示順序
PLATFORMS = ("instagram", "vocus", "medium")

# 走「產出 Markdown 草稿、人工貼上」的平台
DRAFT_PLATFORMS = ("vocus", "medium")

IG_BLOCKED_REASON = (
    "圖卡尚未渲染，Instagram 無法發布。請先執行 RENDERING 產出圖卡。"
)


class UnknownPlatform(ValueError):
    """不是 instagram / vocus / medium。"""


# --- 查詢 ---------------------------------------------------------------


def resolve_summary(video_id: str, version: int | None = None) -> dict:
    """取指定版本（或最新版）的摘要；沒有就拋 NoSummary。

    直接沿用 renderer.load_summary：圖卡與草稿必須來自同一份摘要，
    兩邊各寫一份取版本的邏輯遲早會漂移。
    """
    return load_summary(video_id, version)


def card_count(conn: sqlite3.Connection, summary_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM media_assets WHERE summary_id = ?", (summary_id,)
    ).fetchone()
    return row["n"]


def ensure_post_rows(conn: sqlite3.Connection, summary_id: int) -> None:
    """補齊三個平台的 posts 列。

    summarizer.store() 已經會開，但摘要也可能來自更早期的資料或被手動塞進 DB，
    少一列的話那個平台就永遠發不出去也看不見，所以發布前先補。
    """
    for platform in PLATFORMS:
        conn.execute(
            """INSERT INTO posts (summary_id, platform, status)
               VALUES (?, ?, 'draft')
               ON CONFLICT(summary_id, platform) DO NOTHING""",
            (summary_id, platform),
        )


def list_posts(summary_id: int) -> list[sqlite3.Row]:
    """回傳某份摘要的 posts，順序固定為 PLATFORMS。

    照 id 排會受「哪個平台先被 insert」影響，前端每次刷新順序都可能不同。
    """
    with connect() as conn:
        ensure_post_rows(conn, summary_id)
        rows = conn.execute(
            "SELECT * FROM posts WHERE summary_id = ?", (summary_id,)
        ).fetchall()
    by_platform = {r["platform"]: r for r in rows}
    ordered = [by_platform[p] for p in PLATFORMS if p in by_platform]
    # 萬一有 PLATFORMS 以外的平台被寫進來，仍要看得到，不要靜靜吞掉
    ordered += [r for r in rows if r["platform"] not in PLATFORMS]
    return ordered


def get_post(post_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


# --- 草稿匯出 -----------------------------------------------------------


def draft_path(video_id: str, platform: str) -> Path:
    """草稿檔路徑。檔名不帶版本號 —— 這是「目前要貼出去的那一份」，
    重跑就覆寫；歷史版本本來就完整留在 summaries 表裡，檔案沒必要再留一套。
    """
    return DRAFTS_DIR / f"{video_id}_{platform}.md"


def draft_content(summary: dict) -> str:
    """草稿內容就是 summaries.content 原文。

    不在這裡替各平台做格式加工：content 已經由 summarizer.build_markdown 組好
    標題、來源連結與免責聲明，Vocus 與 Medium 的編輯器都吃標準 Markdown，
    多做一層轉換只會多一個出錯的地方。
    """
    return summary["content"] or ""


def export_draft(video_id: str, platform: str, version: int | None = None) -> Path:
    """把 Markdown 草稿寫到 data/drafts/，回傳檔案路徑。"""
    if platform not in DRAFT_PLATFORMS:
        raise UnknownPlatform(platform)
    summary = resolve_summary(video_id, version)
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    path = draft_path(video_id, platform)
    path.write_text(draft_content(summary), encoding="utf-8")
    return path


def read_draft(video_id: str, platform: str, version: int | None = None) -> tuple[str, Path | None]:
    """取草稿內容供前端「複製全文」，回傳 (內容, 已落地的檔案路徑或 None)。

    檔案存在就以檔案為準：人可能在匯出後直接改過那個檔，回 DB 版本會讓
    「複製全文」拿到跟他手上不一樣的東西。
    """
    if platform not in DRAFT_PLATFORMS:
        raise UnknownPlatform(platform)
    summary = resolve_summary(video_id, version)
    path = draft_path(video_id, platform)
    if path.is_file():
        return path.read_text(encoding="utf-8"), path
    return draft_content(summary), None


# --- posts 狀態 ---------------------------------------------------------


def _set_post(
    conn: sqlite3.Connection,
    summary_id: int,
    platform: str,
    status: str,
    external_url: str | None = None,
    error: str | None = None,
) -> None:
    """寫入單一平台的發布結果。

    published_at 只在 status=published 時蓋章；退回其他狀態時清掉，
    否則會出現「狀態是 blocked 卻有發布時間」這種對不起來的紀錄。
    """
    conn.execute(
        """UPDATE posts
           SET status = ?, external_url = ?, error = ?,
               published_at = CASE WHEN ? = 'published' THEN datetime('now') ELSE NULL END,
               updated_at = datetime('now')
           WHERE summary_id = ? AND platform = ?""",
        (status, external_url, error, status, summary_id, platform),
    )


def video_of_summary(conn: sqlite3.Connection, summary_id: int) -> str | None:
    row = conn.execute(
        "SELECT video_id FROM summaries WHERE id = ?", (summary_id,)
    ).fetchone()
    return row["video_id"] if row else None


def all_published(conn: sqlite3.Connection, summary_id: int) -> bool:
    """三個平台是否都已 published。

    以 PLATFORMS 為準而不是「表裡沒有非 published 的列」：少一列時後者會誤判成
    全部完成，而少一列正是「那個平台根本還沒處理」的情況。
    """
    rows = conn.execute(
        "SELECT platform, status FROM posts WHERE summary_id = ?", (summary_id,)
    ).fetchall()
    status = {r["platform"]: r["status"] for r in rows}
    return all(status.get(p) == "published" for p in PLATFORMS)


def maybe_finish(summary_id: int) -> str | None:
    """三個平台都 published 就把影片推進 PUBLISHED，回傳新狀態（沒推進回 None）。

    advance() 只允許 PUBLISHING → PUBLISHED，影片若還停在別的狀態（例如有人
    手動把 posts 改成 published 但影片根本還在 REVIEW）就吞掉 InvalidTransition：
    這是使用者操作順序的問題，不該讓一次 PATCH 直接回 500，狀態留在原地
    等他走正常流程即可。
    """
    with connect() as conn:
        if not all_published(conn, summary_id):
            return None
        video_id = video_of_summary(conn, summary_id)
    if not video_id:
        return None
    try:
        return pipeline.advance(video_id, pipeline.PUBLISHED)
    except (pipeline.InvalidTransition, pipeline.UnknownVideo):
        return None


def update_post(
    post_id: int,
    status: str | None = None,
    external_url: str | None = None,
    error: str | None = None,
    *,
    fields: set[str] | None = None,
) -> sqlite3.Row | None:
    """局部更新一筆 post，回傳更新後的列（找不到回 None）。

    `fields` 指名這次真正要動的欄位 —— 呼叫端傳 external_url=None 可能是
    「清空這個欄位」也可能是「沒帶這個欄位」，光看值分不出來，所以由呼叫端
    明講。沒給就退回「值不是 None 才更新」。
    """
    sets: list[str] = []
    params: list = []
    touched = fields if fields is not None else {
        k for k, v in (("status", status), ("external_url", external_url), ("error", error))
        if v is not None
    }
    if "status" in touched:
        sets.append("status = ?")
        params.append(status)
        # 人工把狀態改成 published 時補上發布時間；改回其他狀態就清掉
        sets.append(
            "published_at = CASE WHEN ? = 'published' THEN COALESCE(published_at, datetime('now')) ELSE NULL END"
        )
        params.append(status)
    if "external_url" in touched:
        sets.append("external_url = ?")
        params.append(external_url)
    if "error" in touched:
        sets.append("error = ?")
        params.append(error)

    with connect() as conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None:
            return None
        if sets:
            sets.append("updated_at = datetime('now')")
            conn.execute(
                f"UPDATE posts SET {', '.join(sets)} WHERE id = ?", (*params, post_id)
            )
        summary_id = row["summary_id"]

    maybe_finish(summary_id)

    with connect() as conn:
        return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


# --- 發布 ---------------------------------------------------------------


def publish(
    video_id: str, platforms: list[str] | None = None, version: int | None = None
) -> dict:
    """把影片推進 PUBLISHING，對指定平台產出可發布的東西並更新 posts。

    回傳 {"summary_id", "version", "status", "posts"}。

    狀態轉換交給 pipeline.advance 判斷（只允許 RENDERING → PUBLISHING），
    不合法就讓 InvalidTransition 往上拋 —— 沒渲染就發布等於繞過圖卡產出，
    這裡不該自己放行。
    """
    targets = list(platforms) if platforms else list(PLATFORMS)
    unknown = [p for p in targets if p not in PLATFORMS]
    if unknown:
        raise UnknownPlatform(", ".join(unknown))

    summary = resolve_summary(video_id, version)
    summary_id = summary["id"]

    # 先確認摘要存在再動狀態：狀態推進了卻發現沒摘要，影片會卡在 PUBLISHING
    status = pipeline.advance(video_id, pipeline.PUBLISHING)

    # 草稿寫檔在 DB 交易之外先做完：寫檔失敗就不該留下 ready 的狀態
    drafts = {
        p: export_draft(video_id, p, summary["version"])
        for p in targets
        if p in DRAFT_PLATFORMS
    }

    with connect() as conn:
        ensure_post_rows(conn, summary_id)
        has_cards = card_count(conn, summary_id) > 0
        for platform in targets:
            if platform in drafts:
                _set_post(conn, summary_id, platform, "ready", error=None)
            elif platform == "instagram":
                # 有圖卡也只到 ready 為止，理由見模組開頭：Graph API 這條路
                # 在純本機環境走不通，狀態不能謊報成 published
                if has_cards:
                    _set_post(conn, summary_id, platform, "ready", error=None)
                else:
                    _set_post(conn, summary_id, platform, "blocked", error=IG_BLOCKED_REASON)

    return {
        "summary_id": summary_id,
        "version": summary["version"],
        "status": status,
        "posts": list_posts(summary_id),
    }
