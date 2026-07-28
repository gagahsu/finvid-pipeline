import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import {
  PipelineVideo,
  PipelineVideoDetail,
  QueueSnapshot,
  RegisterVideoRequest,
  RegisterVideoResponse,
  RerunRequest,
  RunRequest,
  RunResponse,
  StatusChangeResponse,
} from './models';

/**
 * Pipeline 控制 API 的薄封裝（API 合約 A 節）。
 *
 * 與 ReviewApi 分開是因為兩者的資料來源不同：ReviewApi 讀 transcripts／summaries，
 * 這支讀 videos 表，RSS 抓進來還沒轉錄的影片只有這裡看得到。
 */
@Injectable({ providedIn: 'root' })
export class PipelineApi {
  private readonly http = inject(HttpClient);

  /** videos 表全部，最近更新在前。 */
  listVideos(): Observable<PipelineVideo[]> {
    return this.http.get<PipelineVideo[]>('/api/pipeline/videos');
  }

  /** 單筆，外加 jobs 陣列。找不到回 404。 */
  getVideo(videoId: string): Observable<PipelineVideoDetail> {
    return this.http.get<PipelineVideoDetail>(
      `/api/pipeline/videos/${encodeURIComponent(videoId)}`,
    );
  }

  /** 只登記進 videos 表，不開跑。解析不出 video_id 回 400。 */
  registerVideo(body: RegisterVideoRequest): Observable<RegisterVideoResponse> {
    return this.http.post<RegisterVideoResponse>('/api/pipeline/videos', body);
  }

  /** 丟進背景佇列，立刻回應不等執行完。狀態不允許開跑回 409。 */
  run(videoId: string, body: RunRequest): Observable<RunResponse> {
    return this.http.post<RunResponse>(
      `/api/pipeline/videos/${encodeURIComponent(videoId)}/run`,
      body,
    );
  }

  /** 退回指定狀態重跑。InvalidTransition → 409。 */
  rerun(videoId: string, body: RerunRequest): Observable<StatusChangeResponse> {
    return this.http.post<StatusChangeResponse>(
      `/api/pipeline/videos/${encodeURIComponent(videoId)}/rerun`,
      body,
    );
  }

  /** 退回 REVIEW。InvalidTransition → 409。 */
  backToReview(videoId: string): Observable<StatusChangeResponse> {
    return this.http.post<StatusChangeResponse>(
      `/api/pipeline/videos/${encodeURIComponent(videoId)}/review`,
      {},
    );
  }

  /** runner 的記憶體佇列快照。跑的時候要輪詢這支。 */
  getQueue(): Observable<QueueSnapshot> {
    return this.http.get<QueueSnapshot>('/api/pipeline/queue');
  }
}
