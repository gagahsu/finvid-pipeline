"""YouTube RSS 輪詢：把訂閱頻道的新影片登記進 videos 表。

用 YouTube 內建的 Atom feed，不用官方 Data API（CLAUDE.md 技術選型）：
Data API 要申請 key、有每日配額，而我們只是要「有沒有新影片」這一件事，
RSS 就夠了，而且完全免費、不需要認證。

三個已知限制決定了這支模組的設計：

1. feed 只回最新約 15 支影片。新訂閱的頻道要另外用 ``backfill_source()``
   （yt-dlp ``--flat-playlist``）把歷史清單灌進來。
2. 標題事後會被改，所以新舊一律用 ``yt:videoId`` 判斷，不碰標題。
3. 輪詢頻率抓 15-30 分鐘，抓太頻繁沒有意義（影片不會分鐘級更新）也容易被擋。

進入點：

    python -m app.rss poll                     # 跑一輪就結束
    python -m app.rss backfill UC... --limit 30
    python -m app.rss serve --interval 20      # APScheduler 常駐

console 一律只輸出 ASCII（同 app/cli.py）：Windows 中文環境 console 是 cp950，
印中文標題會拋 UnicodeEncodeError。
"""

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from app import pipeline
from app.db import connect, init_schema

FEED_BASE = "https://www.youtube.com/feeds/videos.xml"

# type -> feed 的 query 參數名。CLAUDE.md 規格只列這兩種來源。
FEED_PARAM = {"channel": "channel_id", "playlist": "playlist_id"}

# Atom + YouTube 擴充命名空間。ElementTree 不吃 feed 自帶的前綴宣告，
# 一定要自己帶完整 URI 才找得到 yt:videoId。
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# 預設 20 分鐘：落在 CLAUDE.md 建議的 15-30 分鐘區間中間。
DEFAULT_INTERVAL_MINUTES = int(os.getenv("RSS_POLL_INTERVAL_MINUTES", "20"))
HTTP_TIMEOUT = float(os.getenv("RSS_HTTP_TIMEOUT", "30"))

# feeds server 間歇性亂回 404/500，重試次數要給得比一般 HTTP 呼叫大方（見 fetch_feed）。
DEFAULT_FETCH_ATTEMPTS = int(os.getenv("RSS_FETCH_ATTEMPTS", "10"))
MAX_BACKOFF_SECONDS = 8

# backfill 預設只拉 30 支。頻道動輒上千支影片，一次全灌進來等於叫 pipeline
# 去下載轉錄幾百小時音訊，所以預設保守，要更多請明講。
DEFAULT_BACKFILL_LIMIT = 30


class FeedError(RuntimeError):
    """單一 feed 抓取或解析失敗。"""


@dataclass
class FeedEntry:
    video_id: str
    title: str | None
    published: str | None
    url: str


@dataclass
class SourceResult:
    """單一來源的輪詢結果。失敗時 error 有值，其餘欄位為空。"""

    channel_id: str
    name: str | None = None
    total: int = 0
    new_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class PollReport:
    results: list[SourceResult] = field(default_factory=list)

    @property
    def new_ids(self) -> list[str]:
        return [vid for r in self.results for vid in r.new_ids]

    @property
    def failed(self) -> list[SourceResult]:
        return [r for r in self.results if not r.ok]


# --- feed URL 與解析 ----------------------------------------------------


def feed_url(channel_id: str, type_: str = "channel") -> str:
    """依來源類型組出 feed URL。"""
    param = FEED_PARAM.get(type_)
    if param is None:
        raise ValueError(f"unknown source type: {type_}")
    return f"{FEED_BASE}?{param}={channel_id}"


def parse_feed(xml_text: str) -> tuple[str | None, list[FeedEntry]]:
    """解析 Atom feed，回傳 (頻道名稱, 影片清單)。

    只用標準庫 xml.etree：feed 結構固定且單純，為此多裝 feedparser 不划算。

    缺 yt:videoId 的 entry 直接跳過而不是報錯 —— feed 偶爾會夾雜非影片項目
    （例如已下架的內容），為了一筆壞資料放棄整個頻道並不合理。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FeedError(f"malformed XML: {exc}") from exc

    feed_title = root.findtext("atom:title", namespaces=NS)

    entries: list[FeedEntry] = []
    for node in root.findall("atom:entry", NS):
        video_id = node.findtext("yt:videoId", namespaces=NS)
        if not video_id:
            continue
        link = node.find("atom:link[@rel='alternate']", NS)
        url = (link.get("href") if link is not None else None) or (
            f"https://www.youtube.com/watch?v={video_id}"
        )
        entries.append(
            FeedEntry(
                video_id=video_id.strip(),
                title=(node.findtext("atom:title", namespaces=NS) or None),
                published=(node.findtext("atom:published", namespaces=NS) or None),
                url=url,
            )
        )
    return feed_title, entries


def fetch_feed(url: str, attempts: int = DEFAULT_FETCH_ATTEMPTS) -> str:
    """抓 feed，帶退避重試。

    實測 YouTube 的 feeds server 會對合法的 channel_id 間歇回 404 或 500，
    所以連 404 也要重試 —— 把它當成「頻道不存在」而直接放棄，會讓好好的訂閱
    莫名其妙漏掉新片。真的不存在的頻道最多就是多打幾次，代價很小。

    失敗率比想像中高：實測理財達人秀的 feed 連打 8 次才拿到第一個 200
    （前 7 次 404、第 8 次 500），所以預設試 10 次。退避上限壓在 8 秒，
    純指數退避到後面單次就要等兩分鐘，一輪輪詢會拖太久。
    """
    last = ""
    for i in range(attempts):
        try:
            response = httpx.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
            if response.status_code == 200:
                return response.text
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = f"request failed: {exc}"
        if i < attempts - 1:
            time.sleep(min(2 ** i, MAX_BACKOFF_SECONDS))
    raise FeedError(f"{last} for {url} (after {attempts} attempts)")


# --- sources 讀取 -------------------------------------------------------


def list_active_sources() -> list[dict]:
    """讀出 active 的訂閱來源。"""
    init_schema()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, channel_id, type, name FROM sources WHERE active = 1 ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def _known_video_ids(video_ids: list[str]) -> set[str]:
    """挑出 videos 表已經有的 id。

    要先查再登記，不能靠 register_video 的回傳值判斷新舊：它刻意不重設 status，
    已存在的影片回傳的是「當前狀態」而非「有沒有新增」，分不出新舊。
    """
    if not video_ids:
        return set()
    placeholders = ",".join("?" * len(video_ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT video_id FROM videos WHERE video_id IN ({placeholders})",
            video_ids,
        ).fetchall()
    return {r["video_id"] for r in rows}


# --- 輪詢 ---------------------------------------------------------------


def poll_source(source: dict, max_new: int | None = None) -> SourceResult:
    """抓單一來源的 feed 並登記新影片。

    max_new 限制這輪最多登記幾支（feed 本來就只回約 15 支，主要是給
    「久未輪詢、一次冒出一堆」的情況留個煞車）。
    """
    result = SourceResult(channel_id=source["channel_id"], name=source.get("name"))
    try:
        xml_text = fetch_feed(feed_url(source["channel_id"], source.get("type") or "channel"))
        feed_title, entries = parse_feed(xml_text)
    except (FeedError, ValueError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.name = source.get("name") or feed_title
    result.total = len(entries)

    known = _known_video_ids([e.video_id for e in entries])
    # feed 是新到舊，倒過來登記，讓 created_at 的先後跟影片發布順序一致。
    fresh = [e for e in reversed(entries) if e.video_id not in known]
    if max_new is not None:
        # 要砍就砍舊的，留最新的幾支。
        fresh = fresh[-max_new:]

    for entry in fresh:
        try:
            pipeline.register_video(
                entry.video_id,
                title=entry.title,
                url=entry.url,
                published_at=entry.published,
                source_id=source.get("id"),
            )
        except Exception as exc:  # 單支影片寫入失敗不該拖垮整個來源
            result.error = f"register {entry.video_id} failed: {type(exc).__name__}: {exc}"
            break
        result.new_ids.append(entry.video_id)
    return result


def poll_all(max_new: int | None = None) -> PollReport:
    """輪詢所有 active 來源。

    單一 feed 失敗（網路斷線、404、XML 壞掉）只記錄在該來源的 error 上，
    不會中斷整輪 —— 一個頻道被刪掉不該讓其他頻道整天收不到新片。
    """
    report = PollReport()
    for source in list_active_sources():
        try:
            report.results.append(poll_source(source, max_new=max_new))
        except Exception as exc:  # 防呆：任何沒預料到的例外也只影響這個來源
            report.results.append(
                SourceResult(
                    channel_id=source["channel_id"],
                    name=source.get("name"),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return report


# --- 新來源初始化 -------------------------------------------------------


def fetch_history(url: str, limit: int = DEFAULT_BACKFILL_LIMIT) -> list[str]:
    """用 yt-dlp 拉頻道/播放清單的歷史 video_id（新到舊）。

    RSS 只回最新約 15 支，新訂閱的頻道要靠這個補完（CLAUDE.md）。
    ``--flat-playlist`` 不會去解析每支影片的串流，只列 id，所以很快也不吃頻寬。

    走 subprocess 而不是 import yt_dlp：這是一次性的初始化工具，
    平常的音訊下載走 app/downloader.py 的 pytubefix，兩者不共用。
    """
    if limit <= 0:
        return []
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--print",
        "id",
        "--playlist-end",
        str(limit),
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError as exc:
        raise FeedError(
            "yt-dlp not found on PATH; install it to backfill a new source"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FeedError(f"yt-dlp timed out: {url}") from exc
    if proc.returncode != 0:
        raise FeedError(f"yt-dlp failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()][:limit]


def backfill_source(
    channel_id: str,
    *,
    type_: str = "channel",
    limit: int = DEFAULT_BACKFILL_LIMIT,
    source_id: int | None = None,
) -> list[str]:
    """把某來源的歷史影片登記進 videos 表，回傳實際新增的 video_id。

    只登記 id 與 url，標題留給日後 RSS 或 download 階段補 ——
    ``--flat-playlist`` 也拿得到標題，但多要一個欄位就多一種解析失敗的可能，
    而標題本來就不是判斷依據（會被改）。
    """
    if type_ not in FEED_PARAM:
        raise ValueError(f"unknown source type: {type_}")
    page = (
        f"https://www.youtube.com/channel/{channel_id}/videos"
        if type_ == "channel"
        else f"https://www.youtube.com/playlist?list={channel_id}"
    )
    video_ids = fetch_history(page, limit=limit)
    known = _known_video_ids(video_ids)
    added: list[str] = []
    for video_id in reversed(video_ids):  # 舊的先進，維持發布順序
        if video_id in known:
            continue
        pipeline.register_video(
            video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
            source_id=source_id,
        )
        added.append(video_id)
    return added


# --- 排程 ---------------------------------------------------------------


def run_scheduler(interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> None:
    """APScheduler 常駐輪詢。Ctrl-C 結束。"""
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler()
    scheduler.add_job(
        _scheduled_poll,
        "interval",
        minutes=interval_minutes,
        # coalesce + max_instances=1：機器休眠醒來後不要把積欠的觸發一次補跑完，
        # 也不允許前一輪還沒跑完就又開一輪（會重複打同一批 feed）。
        coalesce=True,
        max_instances=1,
        # 加點抖動，避免每次都在整點打 YouTube。
        jitter=60,
        id="rss_poll",
    )
    print(f"polling every {interval_minutes} min; Ctrl-C to stop", flush=True)
    _scheduled_poll()  # 啟動時先跑一輪，不用等第一個間隔
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("stopped", flush=True)


def _scheduled_poll() -> None:
    """排程用的包裝：任何例外都吞掉並印出來。

    讓例外冒到 APScheduler 只會讓那一輪被記成 error，這裡自己處理是為了
    輸出格式跟手動 poll 一致，也確保常駐程式不會因為單次失敗而看起來沒動靜。
    """
    try:
        _print_report(poll_all())
    except Exception as exc:
        print(f"poll failed: {_ascii(type(exc).__name__)}: {_ascii(exc)}", flush=True)


# --- CLI ----------------------------------------------------------------


def _ascii(text: object) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


def _print_report(report: PollReport) -> None:
    for r in report.results:
        label = _ascii(r.name or r.channel_id)
        if not r.ok:
            print(f"  {label}: ERROR {_ascii(r.error)}", flush=True)
            continue
        print(f"  {label}: {len(r.new_ids)} new / {r.total} in feed", flush=True)
        for vid in r.new_ids:
            print(f"    + {vid}", flush=True)
    print(
        f"total: {len(report.new_ids)} new, {len(report.failed)} source(s) failed",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="app.rss", description="YouTube RSS poller")
    sub = p.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("poll", help="poll all active sources once")
    po.add_argument("--max-new", type=int, help="cap new videos registered per source")

    bf = sub.add_parser("backfill", help="seed a new source from its history via yt-dlp")
    bf.add_argument("channel_id")
    bf.add_argument("--type", dest="type_", default="channel", choices=list(FEED_PARAM))
    bf.add_argument("--limit", type=int, default=DEFAULT_BACKFILL_LIMIT)

    sv = sub.add_parser("serve", help="run the APScheduler daemon")
    sv.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MINUTES)

    args = p.parse_args(argv)
    init_schema()

    if args.cmd == "poll":
        report = poll_all(max_new=args.max_new)
        _print_report(report)
        return 1 if report.failed else 0

    if args.cmd == "backfill":
        try:
            added = backfill_source(args.channel_id, type_=args.type_, limit=args.limit)
        except (FeedError, ValueError) as exc:
            print(f"{type(exc).__name__}: {_ascii(exc)}", file=sys.stderr)
            return 1
        for vid in added:
            print(f"  + {vid}", flush=True)
        print(f"total: {len(added)} new", flush=True)
        return 0

    run_scheduler(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
