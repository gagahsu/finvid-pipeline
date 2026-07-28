import { Component, OnDestroy, OnInit, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { PipelineApi } from '../../core/pipeline-api';
import {
  PipelineJob,
  PipelineStatus,
  PipelineVideoDetail,
  QueueSnapshot,
  Stage,
} from '../../core/models';
import {
  RERUN_TARGETS,
  STAGE_ORDER,
  apiErrorMessage,
  queueLabel,
  stageLabel,
  statusLabel,
} from '../../core/pipeline-ui';

/** 佇列輪詢間隔。太密會一直打 API，太疏看不出在動，4 秒是折衷。 */
const POLL_MS = 4000;

/** 主線每一格的顯示狀態。沒有 job 紀錄的階段是 'todo'，不是失敗。 */
interface StageRow {
  stage: Stage;
  label: string;
  job: PipelineJob | null;
  state: 'todo' | 'success' | 'failed' | 'running' | 'other';
}

/**
 * 單支影片的 pipeline 詳情：stage 進度、失敗原因、重跑入口。
 *
 * 失敗的 error_detail 一律完整顯示、不截斷 —— 這是使用者唯一能看到
 * 「為什麼卡住」的地方，摺疊或省略等於把問題藏起來。
 */
@Component({
  selector: 'app-pipeline-detail',
  imports: [RouterLink],
  templateUrl: './pipeline-detail.html',
  styleUrl: './pipeline-detail.scss',
})
export class PipelineDetail implements OnInit, OnDestroy {
  /** 路由參數（withComponentInputBinding） */
  readonly videoId = input.required<string>();

  private readonly api = inject(PipelineApi);

  protected readonly detail = signal<PipelineVideoDetail | null>(null);
  protected readonly queue = signal<QueueSnapshot | null>(null);
  protected readonly loading = signal(true);
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);

  /** run 的選項 */
  protected readonly fromStage = signal<Stage | ''>('');
  protected readonly skipJudge = signal(false);
  protected readonly restart = signal(false);

  /** rerun 的目標狀態 */
  protected readonly rerunTarget = signal<PipelineStatus>('CORRECTING');

  protected readonly stageOptions = STAGE_ORDER;
  protected readonly rerunTargets = RERUN_TARGETS;
  protected readonly stageLabel = stageLabel;
  protected readonly statusLabel = statusLabel;
  protected readonly queueLabel = queueLabel;

  private timer: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    this.load();
    this.loadQueue();
    this.timer = setInterval(() => this.poll(), POLL_MS);
  }

  ngOnDestroy(): void {
    // 離開頁面一定要停掉，否則 interval 會在背景一直打 API
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  /** 主線 stage 進度。照 STAGE_ORDER 排，後端沒回的階段補成 todo。 */
  protected readonly stageRows = computed<StageRow[]>(() => {
    const jobs = this.detail()?.jobs ?? [];
    return STAGE_ORDER.map((stage) => {
      // 同一個 stage 可能有多筆（重跑），取最後更新的那筆
      const matches = jobs.filter((j) => j.stage === stage);
      const job = matches.length
        ? matches.reduce((a, b) => ((b.updated_at ?? '') >= (a.updated_at ?? '') ? b : a))
        : null;
      return { stage, label: stageLabel(stage), job, state: this.stageState(job) };
    });
  });

  private stageState(job: PipelineJob | null): StageRow['state'] {
    if (!job) return 'todo';
    if (job.status === 'success') return 'success';
    if (job.status === 'failed' || job.status === 'error') return 'failed';
    if (job.status === 'running') return 'running';
    return 'other';
  }

  /** 不在主線 STAGE_ORDER 裡的 job（後端若新增 stage 也不會被吃掉） */
  protected readonly extraJobs = computed<PipelineJob[]>(() =>
    (this.detail()?.jobs ?? []).filter((j) => !STAGE_ORDER.includes(j.stage)),
  );

  protected readonly failedJobs = computed<PipelineJob[]>(() =>
    (this.detail()?.jobs ?? []).filter((j) => !!j.error_detail),
  );

  /** 這支影片正在佇列裡（排隊或執行中）—— 決定要不要繼續輪詢 */
  protected readonly active = computed(() => {
    const d = this.detail();
    if (d && d.queue_state !== 'idle') return true;
    const q = this.queue();
    if (!q) return false;
    return q.running?.video_id === this.videoId() || q.queued.includes(this.videoId());
  });

  protected readonly queuePosition = computed(() => {
    const q = this.queue();
    if (!q) return -1;
    return q.queued.indexOf(this.videoId());
  });

  private load(): void {
    this.api.getVideo(this.videoId()).subscribe({
      next: (detail) => {
        this.detail.set(detail);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(`載入失敗：${apiErrorMessage(err)}`);
        this.loading.set(false);
      },
    });
  }

  private loadQueue(): void {
    this.api.getQueue().subscribe({
      next: (q) => this.queue.set(q),
      // 佇列拿不到不該把整頁的錯誤欄洗掉（詳情本身可能是好的），靜默即可
      error: () => this.queue.set(null),
    });
  }

  /** 輪詢：跑的時候連詳情一起重抓，閒置時只更新佇列。 */
  private poll(): void {
    const wasActive = this.active();
    this.loadQueue();
    if (wasActive) this.load();
  }

  protected refresh(): void {
    this.load();
    this.loadQueue();
  }

  protected onFromStageChange(event: Event): void {
    this.fromStage.set((event.target as HTMLSelectElement).value as Stage | '');
  }

  protected onSkipJudgeChange(event: Event): void {
    this.skipJudge.set((event.target as HTMLInputElement).checked);
  }

  protected onRestartChange(event: Event): void {
    this.restart.set((event.target as HTMLInputElement).checked);
  }

  protected onRerunTargetChange(event: Event): void {
    this.rerunTarget.set((event.target as HTMLSelectElement).value as PipelineStatus);
  }

  protected run(): void {
    const stage = this.fromStage();
    if (this.restart()) {
      // restart 會把已經跑完的階段整個重來，逐字稿與校正紀錄都會被覆寫
      const ok = confirm(
        `restart 會從頭重跑 ${this.videoId()}，既有的逐字稿與校正結果會被覆寫。確定要繼續嗎？`,
      );
      if (!ok) return;
    }

    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    this.api
      .run(this.videoId(), {
        from_stage: stage === '' ? null : stage,
        skip_judge: this.skipJudge(),
        restart: this.restart(),
      })
      .subscribe({
        next: (res) => {
          this.busy.set(false);
          this.notice.set(
            res.queued
              ? `已排入佇列，前面還有 ${res.position} 支。狀態 ${statusLabel(res.status)}。`
              : '已經在佇列中，這次沒有重複排入。',
          );
          this.refresh();
        },
        error: (err) => {
          this.busy.set(false);
          this.error.set(`無法開跑：${apiErrorMessage(err)}`);
        },
      });
  }

  protected rerun(): void {
    const target = this.rerunTarget();
    const ok = confirm(
      `會把 ${this.videoId()} 退回 ${statusLabel(target)}（${target}），` +
        '該階段之後的產出會被重跑覆寫。確定要繼續嗎？',
    );
    if (!ok) return;

    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    this.api.rerun(this.videoId(), { status: target }).subscribe({
      next: (res) => {
        this.busy.set(false);
        this.notice.set(`已退回 ${statusLabel(res.status)}。要重新執行請按「開始跑」。`);
        this.refresh();
      },
      error: (err) => {
        this.busy.set(false);
        this.error.set(`退回失敗：${apiErrorMessage(err)}`);
      },
    });
  }

  /** 退回 REVIEW 不會丟掉產出，只是把閘門重新打開，不需要 confirm。 */
  protected backToReview(): void {
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    this.api.backToReview(this.videoId()).subscribe({
      next: (res) => {
        this.busy.set(false);
        this.notice.set(`已退回 ${statusLabel(res.status)}。`);
        this.refresh();
      },
      error: (err) => {
        this.busy.set(false);
        this.error.set(`退回審核失敗：${apiErrorMessage(err)}`);
      },
    });
  }

  protected canPublish(): boolean {
    const s = this.detail()?.status;
    return s === 'REVIEW' || s === 'RENDERING' || s === 'PUBLISHING' || s === 'PUBLISHED';
  }

  protected copyError(text: string): void {
    navigator.clipboard.writeText(text).then(
      () => this.notice.set('錯誤訊息已複製到剪貼簿。'),
      () => this.error.set('複製失敗，請手動選取。'),
    );
  }

  protected dismissError(): void {
    this.error.set(null);
  }

  protected dismissNotice(): void {
    this.notice.set(null);
  }
}
