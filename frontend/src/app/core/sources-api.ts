import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import {
  BackfillResult,
  PollAllResult,
  PollResult,
  Source,
  SourceCreateRequest,
  SourceCreated,
  SourcePatch,
} from './publish-models';

/**
 * 訂閱來源管理 API（合約 C 節）。路徑走相對的 /api，由 dev server proxy 轉到 FastAPI。
 *
 * 注意 poll 系列是「慢的」endpoint：rss.fetch_feed 對 YouTube feeds server
 * 預設重試 10 次，最壞要等 40 秒左右。呼叫端一定要有進行中狀態，
 * 不要以為沒回應就是掛掉。
 */
@Injectable({ providedIn: 'root' })
export class SourcesApi {
  private readonly http = inject(HttpClient);

  list(): Observable<Source[]> {
    return this.http.get<Source[]>('/api/sources');
  }

  create(body: SourceCreateRequest): Observable<SourceCreated> {
    return this.http.post<SourceCreated>('/api/sources', body);
  }

  update(id: number, body: SourcePatch): Observable<Source> {
    return this.http.patch<Source>(`/api/sources/${id}`, body);
  }

  /** 硬刪。底下還有影片時後端回 409，detail 會建議改用停用。 */
  remove(id: number): Observable<void> {
    return this.http.delete<void>(`/api/sources/${id}`);
  }

  /** 單一來源輪詢，可能要等將近一分鐘。 */
  poll(id: number, maxNew: number | null = null): Observable<PollResult> {
    return this.http.post<PollResult>(`/api/sources/${id}/poll`, { max_new: maxNew });
  }

  /** 全部 active 來源輪詢，時間是單一來源的數倍。 */
  pollAll(): Observable<PollAllResult> {
    return this.http.post<PollAllResult>('/api/sources/poll', {});
  }

  /** 用 yt-dlp 拉歷史清單（RSS 只給最新 15 支，初始化訂閱一定要跑這個）。 */
  backfill(id: number, limit: number): Observable<BackfillResult> {
    return this.http.post<BackfillResult>(`/api/sources/${id}/backfill`, { limit });
  }
}
