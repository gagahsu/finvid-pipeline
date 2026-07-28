"""背景 pipeline 執行器：單一 worker thread + FIFO 佇列。

為什麼要有這一層：轉錄一支影片動輒數十分鐘，放在 HTTP request 裡跑一定超時，
所以 API 只負責「入列並立刻回應」，真正的執行交給常駐的 worker。

幾個刻意的設計決定：

1. **同時只跑一支影片。** faster-whisper 吃 GPU 記憶體，兩支並行會互相搶到
   OOM，個人用途也沒有平行的需求，所以 worker 只有一條、佇列嚴格 FIFO。
2. **階段實作直接重用 app.cli 的 `_stage_*`。** 那些函式是 CLI 與 API 共同的
   單一事實來源，複製一份出來遲早會兩邊行為不一致（例如 audio 已存在時的
   略過規則）。狀態轉換一律走 `pipeline.run_stage()`，理由同上。
3. **跑完停在 REVIEW，絕不往 RENDERING 走。** 這條跟 cli.cmd_run 一致：
   REVIEW 是 CLAUDE.md 唯一的人工閘門，背景執行器自動跨過去等於閘門失效。
4. **佇列/執行中/最近結果只放記憶體。** 這些是「這個 process 現在在做什麼」，
   重啟後本來就該歸零；真正要留底的是 jobs 表，那個已經由 run_stage 寫了。
5. **worker thread 永遠不能死。** 單支影片失敗只記進 jobs 與 recent，
   迴圈照樣往下一支跑；worker 一旦掛掉，整個後台的觸發功能就靜默失效了。
"""

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app import pipeline
from app.cli import (
    DEFAULT_AUDIO_DIR,
    STAGES,
    _stage_apply,
    _stage_correct,
    _stage_download,
    _stage_summarize,
    _stage_transcribe,
    resolve_target,
)

__all__ = [
    "NotRunnable",
    "PipelineRunner",
    "QueuedRun",
    "RecentEntry",
    "RunningState",
    "get_runner",
    "resolve_target",
]

# recent 只是給前端看「剛剛發生什麼」，保留最近 20 筆（合約 A 節）。
RECENT_LIMIT = 20


class NotRunnable(Exception):
    """目前狀態不允許開跑（對應 HTTP 409）。"""


def _now() -> str:
    """跟 SQLite 的 datetime('now') 對齊：UTC、無時區後綴。

    記憶體狀態與 DB 欄位會在同一份 JSON 裡並排給前端看，格式不一致會很難讀。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class QueuedRun:
    """一筆待執行的請求。參數比照 cli run 的旗標。"""

    video_id: str
    from_stage: str | None = None
    skip_judge: bool = False
    restart: bool = False


@dataclass
class RunningState:
    video_id: str
    stage: str | None
    started_at: str


@dataclass
class RecentEntry:
    video_id: str
    stage: str
    status: str
    error_detail: str | None
    finished_at: str


class PipelineRunner:
    """單 worker 的執行佇列。所有共享狀態都在同一把 lock 底下。"""

    def __init__(self, audio_dir: Path | None = None) -> None:
        self._audio_dir = Path(audio_dir or DEFAULT_AUDIO_DIR).resolve()
        self._queue: deque[QueuedRun] = deque()
        self._running: RunningState | None = None
        self._recent: deque[RecentEntry] = deque(maxlen=RECENT_LIMIT)
        # Condition 而非 queue.Queue：API 要能列出「佇列裡有誰、排第幾」，
        # Queue 沒有可靠的窺看介面，deque + Condition 反而單純。
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None

    # --- 佇列操作 -------------------------------------------------------

    def start(self) -> None:
        """啟動 worker。重複呼叫是安全的（FastAPI reload 會重複 import）。"""
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            # daemon：worker 卡在轉錄時不該擋住 uvicorn 關閉。
            self._thread = threading.Thread(
                target=self._loop, name="pipeline-runner", daemon=True
            )
            self._thread.start()

    def submit(self, run: QueuedRun) -> tuple[bool, int]:
        """入列，回傳 (是否新入列, 佇列位置)。

        狀態檢查在這裡同步做（而不是丟給 worker），API 才有辦法立刻回 409；
        規則刻意跟 cli.cmd_run 一模一樣：FAILED 或 restart 才會被退回 PENDING，
        否則 REVIEW 之後的影片會被一個誤觸的按鈕打回原形重跑一輪。
        """
        current = pipeline.get_status(run.video_id)  # 影片不存在會拋 UnknownVideo
        if current not in (pipeline.PENDING, pipeline.FAILED) and not run.restart:
            raise NotRunnable(
                f"status is {current}; use restart=true to rerun from PENDING"
            )
        with self._cond:
            if self._running is not None and self._running.video_id == run.video_id:
                return False, 0
            for pos, item in enumerate(self._queue, start=1):
                if item.video_id == run.video_id:
                    return False, pos
            self._queue.append(run)
            position = len(self._queue)
            self._cond.notify()
        self.start()
        return True, position

    def state_of(self, video_id: str) -> str:
        """給 videos 列表用的 queue_state。"""
        with self._cond:
            if self._running is not None and self._running.video_id == video_id:
                return "running"
            if any(item.video_id == video_id for item in self._queue):
                return "queued"
        return "idle"

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "running": self._running,
                "queued": [item.video_id for item in self._queue],
                # 新的在前：前端要看的是最近發生什麼
                "recent": list(reversed(self._recent)),
            }

    # --- worker ---------------------------------------------------------

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                run = self._queue.popleft()
                self._running = RunningState(run.video_id, None, _now())
            try:
                self._execute(run)
            except Exception as exc:  # noqa: BLE001
                # 走到這裡代表連 _execute 的保護都沒接住（例如 DB 掛了）。
                # 這條 except 的存在只有一個目的：worker thread 不准死。
                self._record(run.video_id, run.from_stage or "run", "failed", _fmt(exc))
            finally:
                with self._cond:
                    self._running = None

    def _execute(self, run: QueuedRun) -> None:
        """跑完整條主線，行為對齊 cli.cmd_run。"""
        video_id = run.video_id
        try:
            current = pipeline.get_status(video_id)
            if current == pipeline.FAILED or run.restart:
                pipeline.rerun_from(video_id, pipeline.PENDING)
        except Exception as exc:  # noqa: BLE001
            self._record(video_id, "run", "failed", _fmt(exc))
            return

        start = STAGES.index(run.from_stage) if run.from_stage else 0

        # 被跳過的前置階段仍要走過狀態，否則後面的 advance() 會判定成亂跳。
        # job 記成 skipped，事後看得出這支沒有真的跑過下載/轉錄。
        try:
            for stage in STAGES[:start]:
                pipeline.advance(video_id, pipeline.STAGE_STATUS[stage])
                pipeline.finish_job(video_id, stage, "skipped")
                self._record(video_id, stage, "skipped", None)
        except Exception as exc:  # noqa: BLE001
            self._record(video_id, "run", "failed", _fmt(exc))
            return

        for stage in STAGES[start:]:
            self._set_stage(stage)
            try:
                with pipeline.run_stage(video_id, stage):
                    self._dispatch(stage, video_id, run.skip_judge)
            except Exception as exc:  # noqa: BLE001
                # run_stage 已經把影片打成 FAILED 並寫好 jobs.error_detail，
                # 這裡只補記憶體那份 recent，然後放棄後續階段。
                self._record(video_id, stage, "failed", _fmt(exc))
                return
            self._record(video_id, stage, "success", None)

        # 全部階段跑完停在 REVIEW —— 人工閘門之後的 RENDERING/PUBLISHING
        # 一律要人在審核台按下去，背景執行器不准自己跨過去。
        try:
            pipeline.advance(video_id, pipeline.REVIEW)
        except Exception as exc:  # noqa: BLE001
            self._record(video_id, "review", "failed", _fmt(exc))

    def _dispatch(self, stage: str, video_id: str, skip_judge: bool) -> None:
        if stage == "download":
            _stage_download(video_id, self._audio_dir)
        elif stage == "transcribe":
            _stage_transcribe(video_id, self._audio_dir)
        elif stage == "correct":
            _stage_correct(video_id, self._audio_dir, skip_judge)
        elif stage == "apply":
            _stage_apply(video_id, self._audio_dir)
        elif stage == "summarize":
            _stage_summarize(video_id)
        else:
            raise ValueError(f"unknown stage: {stage}")

    # --- 記憶體狀態 -----------------------------------------------------

    def _set_stage(self, stage: str) -> None:
        with self._cond:
            if self._running is not None:
                self._running.stage = stage

    def _record(
        self, video_id: str, stage: str, status: str, error_detail: str | None
    ) -> None:
        with self._cond:
            self._recent.append(
                RecentEntry(video_id, stage, status, error_detail, _now())
            )


def _fmt(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


# --- lazy singleton -----------------------------------------------------
#
# FastAPI（尤其開 reload 時）會重複 import 模組，在 import 時就開 thread 會
# 每次多開一條。改成第一次真的要用才建立，並用 lock 擋住併發初始化。

_runner: PipelineRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> PipelineRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = PipelineRunner()
    return _runner
