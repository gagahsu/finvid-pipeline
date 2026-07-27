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
