"""Pipeline CLI 入口。

依序驅動 download -> transcribe -> correct -> apply -> summarize，
每個階段更新 videos.status 並在 jobs 留下紀錄，最後停在 REVIEW 等人工審核。

會用到 OpenRouter 配額的是 correct（約 18 個 request）與 summarize（1 個）。
--skip-judge 只跳過 correct 那段。

用法：

    python -m app.cli run <url|video_id> [--from correct] [--skip-judge]
    python -m app.cli status <video_id>
    python -m app.cli list [--status REVIEW]
    python -m app.cli review <video_id>          # 退回人工審核
    python -m app.cli render <video_id>          # 審核通過後產 IG 圖卡
    python -m app.cli add-source <channel_id>    # 訂閱頻道
    python -m app.cli init-source <channel_id>   # 訂閱 + yt-dlp 拉歷史
    python -m app.cli poll                       # 抓一輪 RSS 找新影片
    python -m app.cli rerun <video_id> CORRECTING

console 一律只輸出 ASCII：Windows 中文環境的 console 是 cp950，
print 逐字稿或含中文的錯誤訊息會拋 UnicodeEncodeError。
詳細內容（含中文）寫到 --report 指定的檔案，預設不寫。
"""

import argparse
import json
import re
import sys
from pathlib import Path

from app import pipeline
from app.db import init_schema

DEFAULT_AUDIO_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "audio"

STAGES = ["download", "transcribe", "correct", "apply", "summarize"]

# 取 YouTube video_id：watch?v=、youtu.be/、/shorts/ 三種形式
_ID_PATTERNS = [
    re.compile(r"[?&]v=([\w-]{6,})"),
    re.compile(r"youtu\.be/([\w-]{6,})"),
    re.compile(r"/shorts/([\w-]{6,})"),
]


def _ascii(text: object) -> str:
    """把任意字串壓成 console 印得出來的 ASCII。"""
    return str(text).encode("ascii", "replace").decode("ascii")


def resolve_target(target: str) -> tuple[str, str | None]:
    """回傳 (video_id, url)。給 video_id 時 url 為 None。"""
    if not target.startswith("http"):
        return target, None
    for pat in _ID_PATTERNS:
        m = pat.search(target)
        if m:
            return m.group(1), target
    raise SystemExit(f"cannot parse video_id from: {_ascii(target)}")


# --- 各階段實作（都只負責做事，狀態由 run_stage 管） --------------------


def _stage_download(video_id: str, audio_dir: Path) -> Path:
    from app.downloader import download_audio

    audio = audio_dir / f"{video_id}.m4a"
    if audio.exists():
        return audio
    url = pipeline.get_video(video_id)["url"]
    if not url:
        raise RuntimeError(f"no url recorded for {video_id}, and no local audio file")
    return download_audio(url, audio_dir)


def _stage_transcribe(video_id: str, audio_dir: Path) -> Path:
    from app.transcriber import transcribe

    out = audio_dir / f"{video_id}.json"
    if out.exists():
        return out
    audio = audio_dir / f"{video_id}.m4a"
    if not audio.exists():
        raise RuntimeError(f"audio not found: {audio}")
    return transcribe(audio)


def _stage_correct(video_id: str, audio_dir: Path, skip_judge: bool) -> dict:
    from app.corrector import detect, judge_and_store, load_tickers

    transcript = _load_transcript(video_id, audio_dir)
    tickers = load_tickers()
    suspects = detect(transcript["segments"], tickers)
    result = {
        "segments": len(transcript["segments"]),
        "suspects": len(suspects),
        "unique_terms": len({s.text for s in suspects}),
    }
    if skip_judge:
        # LLM 判斷是唯一會花配額的一步，跳過時 corrections 不會有新紀錄，
        # apply 階段自然變成 no-op，狀態流程仍然照走完整條線。
        result["judged"] = "skipped"
        return result
    result["judge"] = judge_and_store(video_id, suspects)
    return result


def _stage_apply(video_id: str, audio_dir: Path) -> dict:
    from app.applier import apply_to_transcript

    return apply_to_transcript(video_id, _load_transcript(video_id, audio_dir))


def _stage_summarize(video_id: str) -> dict:
    from app.summarizer import summarize

    return summarize(video_id)


def _stage_render(video_id: str, version: int | None) -> dict:
    from app.renderer import render

    paths = render(video_id, version)
    return {"cards": len(paths), "dir": str(paths[0].parent)}


def _load_transcript(video_id: str, audio_dir: Path) -> dict:
    path = audio_dir / f"{video_id}.json"
    if not path.exists():
        raise RuntimeError(f"transcript not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# --- 指令 ---------------------------------------------------------------


def cmd_run(args) -> int:
    audio_dir = Path(args.audio_dir).resolve()
    video_id, url = resolve_target(args.target)
    pipeline.register_video(video_id, url=url, title=args.title)

    start = STAGES.index(args.start) if args.start else 0

    # FAILED 不在主線上，advance() 會擋，所以重跑前一定要先退回起點。
    # 已經跑到後面階段的影片則要求 --restart：run 指令不該無聲把 REVIEW 之後
    # 的影片打回原形，那是 rerun/review 子指令的職責。
    current = pipeline.get_status(video_id)
    if current == pipeline.FAILED or args.restart:
        pipeline.rerun_from(video_id, pipeline.PENDING)
    elif current != pipeline.PENDING:
        print(f"status: {current} (use --restart to rerun from PENDING)", flush=True)
        return 2

    # 被 --from 跳過的前置階段仍要走過狀態，否則後面 advance 會判定亂跳。
    # job 記成 skipped，事後看得出這支影片沒有真的跑過下載/轉錄。
    for stage in STAGES[:start]:
        pipeline.advance(video_id, pipeline.STAGE_STATUS[stage])
        pipeline.finish_job(video_id, stage, "skipped")
        print(f"{stage}: skipped", flush=True)

    report: dict = {"video_id": video_id}
    for stage in STAGES[start:]:
        try:
            with pipeline.run_stage(video_id, stage):
                if stage == "download":
                    out = str(_stage_download(video_id, audio_dir))
                elif stage == "transcribe":
                    out = str(_stage_transcribe(video_id, audio_dir))
                elif stage == "correct":
                    out = _stage_correct(video_id, audio_dir, args.skip_judge)
                elif stage == "apply":
                    out = _stage_apply(video_id, audio_dir)
                else:
                    out = _stage_summarize(video_id)
            report[stage] = out
            print(f"{stage}: ok", flush=True)
        except Exception as exc:
            report[stage] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"{stage}: FAILED ({_ascii(type(exc).__name__)})", flush=True)
            _write_report(args.report, report)
            print(f"status: {pipeline.get_status(video_id)}", flush=True)
            return 1

    # 全部階段跑完就推進到 REVIEW，pipeline 在此停下等人。
    # REVIEW 之後的 RENDERING / PUBLISHING 一律由人在審核台按下去才會走，
    # CLI 不會自己往下跑 —— 那是唯一的人工閘門，不能被批次執行繞過。
    pipeline.advance(video_id, pipeline.REVIEW)
    print(f"status: {pipeline.get_status(video_id)}", flush=True)
    _write_report(args.report, report)
    return 0


def _write_report(path: str | None, report: dict) -> None:
    if not path:
        return
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def cmd_status(args) -> int:
    video = pipeline.get_video(args.video_id)
    print(f"video_id: {video['video_id']}")
    print(f"status:   {video['status']}")
    for j in pipeline.list_jobs(args.video_id):
        err = " err" if j["error_detail"] else ""
        print(f"  {j['stage']:<11} {j['status']:<8} retry={j['retry_count']}{err}")
    return 0


def cmd_list(args) -> int:
    rows = pipeline.list_videos(args.status)
    for r in rows:
        print(f"{r['video_id']:<16} {r['status']}")
    print(f"total: {len(rows)}")
    return 0


def cmd_review(args) -> int:
    print(f"status: {pipeline.back_to_review(args.video_id)}")
    return 0


def cmd_render(args) -> int:
    """把最新版摘要渲染成 IG 圖卡。

    刻意不併進 run 的 STAGES：RENDERING 在 REVIEW 之後，run 一律停在人工閘門前，
    圖卡要等人審完摘要才值得產。
    """
    from app.renderer import FontNotFound, NoSummary

    try:
        with pipeline.run_stage(args.video_id, "render"):
            out = _stage_render(args.video_id, args.version)
    except (FontNotFound, NoSummary) as exc:
        # 這兩個錯誤訊息含中文，直接讓它冒到 traceback 會在 cp950 console 再炸一次
        print(f"{type(exc).__name__}: {_ascii(exc)}", file=sys.stderr)
        return 1
    print(f"cards: {out['cards']}")
    print(f"dir:   {_ascii(out['dir'])}")
    return 0


def cmd_poll(args) -> int:
    """抓一輪 RSS，把新影片登記進 videos。

    只登記不跑 pipeline：轉錄很吃資源，要跑哪一支由人（或另一個排程）決定。
    """
    from app import rss

    report = rss.poll_all(max_new=args.max_new)
    for r in report.results:
        label = _ascii(r.name or r.channel_id)
        if r.ok:
            print(f"{label}: +{len(r.new_ids)} new / {r.total}")
        else:
            print(f"{label}: FAILED {_ascii(r.error)}")
    print(f"new: {len(report.new_ids)}")
    # 有來源失敗就回非零，排程器/工作排程器才看得出這輪不完整
    return 1 if report.failed else 0


def cmd_add_source(args) -> int:
    sid = pipeline.upsert_source(args.channel_id, name=args.name, type_=args.type_)
    print(f"source id: {sid}")
    return 0


def cmd_init_source(args) -> int:
    """新訂閱一次做完：登記來源 + yt-dlp 拉歷史清單。

    RSS 只回最新約 15 支（CLAUDE.md），新頻道不 backfill 會漏掉整個歷史。
    """
    from app import rss

    sid = pipeline.upsert_source(args.channel_id, name=args.name, type_=args.type_)
    added = rss.backfill_source(
        args.channel_id, type_=args.type_, limit=args.limit, source_id=sid
    )
    print(f"source id:  {sid}")
    print(f"backfilled: {len(added)}")
    return 0


def cmd_rerun(args) -> int:
    print(f"status: {pipeline.rerun_from(args.video_id, args.status)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.cli", description="finvid pipeline runner")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run pipeline stages")
    run.add_argument("target", help="YouTube URL or video_id")
    run.add_argument("--title")
    run.add_argument("--audio-dir", default=str(DEFAULT_AUDIO_DIR))
    run.add_argument("--from", dest="start", choices=STAGES, help="start at this stage")
    run.add_argument(
        "--skip-judge",
        action="store_true",
        help="skip the LLM judging call (no API quota used)",
    )
    run.add_argument("--restart", action="store_true", help="reset to PENDING first")
    run.add_argument("--report", help="write a UTF-8 JSON report to this path")
    run.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="show video status and jobs")
    st.add_argument("video_id")
    st.set_defaults(func=cmd_status)

    ls = sub.add_parser("list", help="list videos")
    ls.add_argument("--status")
    ls.set_defaults(func=cmd_list)

    rv = sub.add_parser("review", help="send back to REVIEW")
    rv.add_argument("video_id")
    rv.set_defaults(func=cmd_review)

    rd = sub.add_parser("render", help="render IG cards from a summary")
    rd.add_argument("video_id")
    rd.add_argument("--version", type=int, help="summary version (default: latest)")
    rd.set_defaults(func=cmd_render)

    # RSS 相關。--type / --limit 的預設值向 rss 模組拿，避免兩邊各寫一份。
    from app import rss

    po = sub.add_parser("poll", help="poll subscribed RSS feeds for new videos")
    po.add_argument("--max-new", type=int, help="cap new videos registered per source")
    po.set_defaults(func=cmd_poll)

    ad = sub.add_parser("add-source", help="subscribe to a channel or playlist")
    ad.add_argument("channel_id")
    ad.add_argument("--type", dest="type_", choices=list(rss.FEED_PARAM), default="channel")
    ad.add_argument("--name")
    ad.set_defaults(func=cmd_add_source)

    it = sub.add_parser("init-source", help="subscribe and backfill history via yt-dlp")
    it.add_argument("channel_id")
    it.add_argument("--type", dest="type_", choices=list(rss.FEED_PARAM), default="channel")
    it.add_argument("--name")
    it.add_argument("--limit", type=int, default=rss.DEFAULT_BACKFILL_LIMIT)
    it.set_defaults(func=cmd_init_source)

    rr = sub.add_parser("rerun", help="roll back to an earlier status")
    rr.add_argument("video_id")
    rr.add_argument("status", choices=pipeline.ORDER)
    rr.set_defaults(func=cmd_rerun)
    return p


def main(argv: list[str] | None = None) -> int:
    init_schema()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (pipeline.InvalidTransition, pipeline.UnknownVideo) as exc:
        print(f"{type(exc).__name__}: {_ascii(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
