/** 後端 /api 回傳的資料型別，欄位名稱與 backend/app/api/schemas.py 對應。 */

export type CorrectionStatus = 'auto' | 'needs_review' | 'rejected';

export type ReviewAction = 'accept' | 'reject' | 'reset';

export interface VideoSummary {
  video_id: string;
  title: string | null;
  updated_at: string | null;
  segment_count: number;
  applied_count: number;
  total_corrections: number;
  auto_count: number;
  needs_review_count: number;
  rejected_count: number;
  human_reviewed_count: number;
  /** 還沒被人看過、且不是 rejected 的數量 */
  pending_count: number;
}

export interface Candidate {
  symbol: string | null;
  name_zh: string | null;
  score: number | null;
}

export interface Correction {
  id: number;
  video_id: string;
  segment_index: number | null;
  context: string | null;
  original: string;
  corrected: string | null;
  confidence: number | null;
  candidates: Candidate[];
  reason: string | null;
  status: CorrectionStatus;
  human_reviewed: boolean;
  created_at: string | null;
  /** 依目前狀態，這筆是否會被套進逐字稿 */
  applied: boolean;
}

export interface SegmentDiff {
  index: number;
  start: number | null;
  end: number | null;
  raw_text: string;
  corrected_text: string;
  changed: boolean;
  correction_ids: number[];
  has_needs_review: boolean;
}

export interface TranscriptDetail {
  video_id: string;
  title: string | null;
  updated_at: string | null;
  applied_count: number;
  raw_text: string;
  corrected_text: string;
  /** 還原出來的原始 segments 是否與 raw_text 完全吻合 */
  raw_exact: boolean;
  segments: SegmentDiff[];
}

/** 摘要版本清單的一列，不含正文（清單頁不需要，內容很長）。 */
export interface SummaryVersion {
  id: number;
  video_id: string;
  version: number;
  title: string | null;
  model: string | null;
  created_at: string | null;
  content_chars: number;
  ig_caption_chars: number;
  /** 這個版本是不是人工在審核台編輯後存下來的 */
  human_edited: boolean;
}

export interface SummaryDetail extends SummaryVersion {
  content: string;
  ig_caption: string | null;
  /** 與逐字稿的逐字重疊片段，有值代表這一版有著作權疑慮 */
  verbatim_overlap: string | null;
}

/** 人工編輯後送出的內容。後端會存成新的一個 version，不覆寫舊版。 */
export interface SummaryEditRequest {
  title: string | null;
  content: string;
  ig_caption: string | null;
}

// ---------------------------------------------------------------------------
// Pipeline 控制（GET/POST /api/pipeline/*）
// 型別依 API 合約 v1 寫死，欄位名稱不得擅改。
// ---------------------------------------------------------------------------

/** videos.status 的狀態機。順序即 pipeline 主線推進順序。 */
export type PipelineStatus =
  | 'PENDING'
  | 'DOWNLOADING'
  | 'TRANSCRIBING'
  | 'CORRECTING'
  | 'SUMMARIZING'
  | 'REVIEW'
  | 'RENDERING'
  | 'PUBLISHING'
  | 'PUBLISHED'
  | 'FAILED';

/** jobs.stage —— 比 status 細，一個 status 可能對應多個 stage。 */
export type Stage = 'download' | 'transcribe' | 'correct' | 'apply' | 'summarize' | 'render';

/** runner 記憶體佇列裡的位置。 */
export type QueueState = 'idle' | 'queued' | 'running';

export interface PipelineJob {
  stage: Stage;
  status: string;
  retry_count: number;
  /** 失敗原因的完整內容，前端不得截斷（這是唯一看得到失敗細節的地方）。 */
  error_detail: string | null;
  started_at: string | null;
  updated_at: string | null;
}

/** 清單頁只回最近一次 job，沒有 started_at。 */
export type LatestJob = Omit<PipelineJob, 'started_at'>;

export interface PipelineVideo {
  video_id: string;
  title: string | null;
  url: string | null;
  published_at: string | null;
  status: PipelineStatus;
  source_id: number | null;
  source_name: string | null;
  updated_at: string | null;
  has_transcript: boolean;
  has_summary: boolean;
  card_count: number;
  queue_state: QueueState;
  latest_job: LatestJob | null;
}

export interface PipelineVideoDetail extends PipelineVideo {
  jobs: PipelineJob[];
}

/** POST /api/pipeline/videos —— 只登記，不開跑。 */
export interface RegisterVideoRequest {
  target: string;
  title: string | null;
}

export interface RegisterVideoResponse {
  video_id: string;
  status: PipelineStatus;
  /** 這次是否為新登記；已存在則 false，且後端不動它的 status。 */
  created: boolean;
}

export interface RunRequest {
  /** null = 從目前狀態接續 */
  from_stage: Stage | null;
  skip_judge: boolean;
  restart: boolean;
}

export interface RunResponse {
  video_id: string;
  /** 已在佇列中則 false */
  queued: boolean;
  status: PipelineStatus;
  position: number;
}

export interface RerunRequest {
  status: PipelineStatus;
}

/** rerun / review 共用的回應。 */
export interface StatusChangeResponse {
  video_id: string;
  status: PipelineStatus;
}

export interface QueueRunning {
  video_id: string;
  stage: Stage;
  started_at: string | null;
}

export interface QueueRecent {
  video_id: string;
  stage: Stage;
  status: string;
  error_detail: string | null;
  finished_at: string | null;
}

export interface QueueSnapshot {
  running: QueueRunning | null;
  /** 等待中的 video_id，依序 */
  queued: string[];
  /** 最近 20 筆，記憶體保留 */
  recent: QueueRecent[];
}
