import { HttpErrorResponse } from '@angular/common/http';

import { PipelineStatus, QueueState, Stage } from './models';

/** pipeline 主線的推進順序。詳情頁的 stage 進度依這個排，不照後端回傳的順序。 */
export const STAGE_ORDER: readonly Stage[] = [
  'download',
  'transcribe',
  'correct',
  'apply',
  'summarize',
  'render',
];

const STAGE_LABELS: Record<Stage, string> = {
  download: '下載音訊',
  transcribe: '語音轉錄',
  correct: '股票詞校正',
  apply: '套用修正',
  summarize: '產生摘要',
  render: '渲染圖卡',
};

export function stageLabel(stage: Stage | string): string {
  return STAGE_LABELS[stage as Stage] ?? stage;
}

const STATUS_LABELS: Record<PipelineStatus, string> = {
  PENDING: '待處理',
  DOWNLOADING: '下載中',
  TRANSCRIBING: '轉錄中',
  CORRECTING: '校正中',
  SUMMARIZING: '摘要中',
  REVIEW: '待人工審核',
  RENDERING: '渲染中',
  PUBLISHING: '發布中',
  PUBLISHED: '已發布',
  FAILED: '失敗',
};

export function statusLabel(status: PipelineStatus | string): string {
  return STATUS_LABELS[status as PipelineStatus] ?? status;
}

/** 可以 rerun 退回的目標狀態。PENDING 之後、REVIEW 之前的主線階段。 */
export const RERUN_TARGETS: readonly PipelineStatus[] = [
  'PENDING',
  'DOWNLOADING',
  'TRANSCRIBING',
  'CORRECTING',
  'SUMMARIZING',
];

const QUEUE_LABELS: Record<QueueState, string> = {
  idle: '閒置',
  queued: '排隊中',
  running: '執行中',
};

export function queueLabel(state: QueueState | string): string {
  return QUEUE_LABELS[state as QueueState] ?? state;
}

/**
 * 把後端錯誤攤成人看得懂的字串。
 *
 * 409（狀態不允許）與 400（解析不出 video_id）的 detail 是唯一說明原因的地方，
 * 一定要原樣顯示出來，不能吞掉只寫「操作失敗」。
 */
export function apiErrorMessage(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    const detail = err.error?.detail;
    if (typeof detail === 'string' && detail) return `${err.status}：${detail}`;
    if (detail) return `${err.status}：${JSON.stringify(detail)}`;
    if (err.status === 0) return '連不到後端 API（後端可能還沒啟動）。';
    return `${err.status} ${err.statusText || ''}`.trim();
  }
  const anyErr = err as { message?: string } | null;
  return anyErr?.message ?? String(err);
}
