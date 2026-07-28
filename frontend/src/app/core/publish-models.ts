/**
 * 圖卡／發布／訂閱來源的資料型別，對應 api-contract.md 的 B、C 兩節。
 *
 * 這些欄位名稱是前後端的契約，後端由另一支 agent 平行實作，
 * 任何欄位改名都要先改合約，不要在這裡「順手修正」。
 */

/** 只有這三個平台。instagram 在純本機環境無法自動發文，見 PublishPost.status 註解。 */
export type Platform = 'instagram' | 'vocus' | 'medium';

/** 草稿是人工貼上的平台，只有 vocus / medium 有草稿檔。 */
export type DraftPlatform = 'vocus' | 'medium';

/**
 * - `draft`：還沒跑發布動作
 * - `ready`：東西已經備妥（草稿檔已寫出／圖卡已渲染），等人工完成最後一步
 * - `published`：人工回填 external_url 後標記完成
 * - `blocked`：缺前置條件（例如 IG 圖卡還沒渲染），error 會寫原因
 * - `failed`：發布過程出錯
 */
export type PostStatus = 'draft' | 'ready' | 'published' | 'blocked' | 'failed';

export interface MediaCard {
  /** preview 模式渲染不存檔，這時 id 是 null */
  id: number | null;
  type: string;
  file_path: string;
  width: number;
  height: number;
  /** `/api/media/{id}` 或 preview 的 `/api/media/preview/{token}`，直接餵給 img src */
  url: string;
}

export interface CardSet {
  video_id: string;
  summary_id: number;
  version: number;
  /** 有摘要但還沒渲染時是空陣列（不是 404） */
  cards: MediaCard[];
}

export interface RenderRequest {
  version: number | null;
  /** true = 渲染到暫存目錄不寫 media_assets，url 指向 30 分鐘後過期的 preview token */
  preview: boolean;
}

export interface PublishPost {
  id: number;
  summary_id: number;
  platform: Platform;
  status: PostStatus;
  external_url: string | null;
  published_at: string | null;
  error: string | null;
  updated_at: string | null;
}

export interface PublishRequest {
  platforms: Platform[];
  version: number | null;
}

/** PATCH /api/posts/{id}：只送要改的欄位，沒給的欄位後端不動。 */
export interface PostPatch {
  status?: PostStatus | null;
  external_url?: string | null;
  error?: string | null;
}

export interface DraftContent {
  platform: string;
  content: string;
  file_path: string | null;
}

/* ---------- C. 訂閱來源 ---------- */

export type SourceType = 'channel' | 'playlist';

export interface Source {
  id: number;
  channel_id: string;
  type: SourceType;
  name: string | null;
  active: boolean;
  created_at: string | null;
  video_count: number;
  latest_video_published_at: string | null;
  feed_url: string;
}

/** POST /api/sources 的回應比 GET 少了統計欄位，刻意分開型別避免誤用。 */
export interface SourceCreated {
  id: number;
  channel_id: string;
  type: SourceType;
  name: string | null;
  active: boolean;
}

export interface SourceCreateRequest {
  /** `UC…` / `PL…`、頻道網址或 `@handle`，後端解析不出來回 400 */
  target: string;
  type: SourceType;
  name: string | null;
}

export interface SourcePatch {
  active?: boolean;
  name?: string;
}

/**
 * 輪詢單一來源的結果。抓失敗時後端仍回 200，把訊息塞進 `error`，
 * 因為 YouTube feeds server 會間歇 404/500，那不是我們的 bug，
 * 不該讓整批輪詢因為一個來源掛掉而全滅。
 */
export interface PollResult {
  channel_id: string;
  name: string | null;
  total: number;
  new_ids: string[];
  error: string | null;
}

export interface PollAllResult {
  results: PollResult[];
  new_ids: string[];
}

export interface BackfillResult {
  added: string[];
  count: number;
}
