import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { PipelineApi } from '../../core/pipeline-api';
import { PipelineVideo } from '../../core/models';
import { apiErrorMessage, queueLabel, stageLabel, statusLabel } from '../../core/pipeline-ui';

/**
 * 影片總覽：列出 videos 表全部影片與 pipeline 狀態。
 *
 * 刻意讀 /api/pipeline/videos 而不是 /api/videos —— 後者的來源是 transcripts 表，
 * RSS 剛抓進來、還沒轉錄的影片在那邊完全看不到，等於漏掉最需要被觸發的那一批。
 */
@Component({
  selector: 'app-video-list',
  imports: [RouterLink],
  templateUrl: './video-list.html',
  styleUrl: './video-list.scss',
})
export class VideoList implements OnInit {
  private readonly api = inject(PipelineApi);

  protected readonly videos = signal<PipelineVideo[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);

  /** 新增影片表單 */
  protected readonly target = signal('');
  protected readonly submitting = signal(false);
  /** 剛登記成功、還沒開跑的 video_id —— 讓「開始跑」按鈕直接出現在表單旁邊 */
  protected readonly justAdded = signal<string | null>(null);

  /** 正在送 run 請求的 video_id，避免連點重複排隊 */
  protected readonly busyId = signal<string | null>(null);

  protected readonly stageLabel = stageLabel;
  protected readonly statusLabel = statusLabel;
  protected readonly queueLabel = queueLabel;

  protected readonly activeCount = computed(
    () => this.videos().filter((v) => v.queue_state !== 'idle').length,
  );

  protected readonly failedCount = computed(
    () => this.videos().filter((v) => v.status === 'FAILED').length,
  );

  ngOnInit(): void {
    this.load();
  }

  protected load(): void {
    this.loading.set(true);
    this.api.listVideos().subscribe({
      next: (videos) => {
        this.videos.set(videos);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(`載入失敗：${apiErrorMessage(err)}`);
        this.loading.set(false);
      },
    });
  }

  protected onTargetInput(event: Event): void {
    this.target.set((event.target as HTMLInputElement).value);
  }

  protected register(): void {
    const target = this.target().trim();
    if (!target) {
      this.error.set('請先輸入 YouTube 網址或 video_id。');
      return;
    }
    this.submitting.set(true);
    this.error.set(null);
    this.notice.set(null);
    this.api.registerVideo({ target, title: null }).subscribe({
      next: (res) => {
        this.submitting.set(false);
        this.target.set('');
        this.justAdded.set(res.video_id);
        this.notice.set(
          res.created
            ? `已登記 ${res.video_id}，狀態 ${statusLabel(res.status)}。可以按「開始跑」。`
            : `${res.video_id} 之前就登記過了（目前狀態 ${statusLabel(res.status)}），沒有變更。`,
        );
        this.load();
      },
      error: (err) => {
        this.submitting.set(false);
        this.error.set(`登記失敗：${apiErrorMessage(err)}`);
      },
    });
  }

  /** 從目前狀態接續跑。restart 由詳情頁負責，這裡只做最單純的觸發。 */
  protected run(videoId: string): void {
    this.busyId.set(videoId);
    this.error.set(null);
    this.notice.set(null);
    this.api.run(videoId, { from_stage: null, skip_judge: false, restart: false }).subscribe({
      next: (res) => {
        this.busyId.set(null);
        this.justAdded.set(null);
        this.notice.set(
          res.queued
            ? `${res.video_id} 已排入佇列，前面還有 ${res.position} 支。`
            : `${res.video_id} 已經在佇列中了。`,
        );
        this.load();
      },
      error: (err) => {
        this.busyId.set(null);
        this.error.set(`無法開跑：${apiErrorMessage(err)}`);
      },
    });
  }

  /** 到 REVIEW 之後才有東西可以發布，更早的階段連圖卡都還沒渲染。 */
  protected canPublish(v: PipelineVideo): boolean {
    return (
      v.status === 'REVIEW' ||
      v.status === 'RENDERING' ||
      v.status === 'PUBLISHING' ||
      v.status === 'PUBLISHED'
    );
  }

  protected dismissError(): void {
    this.error.set(null);
  }

  protected dismissNotice(): void {
    this.notice.set(null);
  }
}
