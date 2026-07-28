import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import {
  CardSet,
  DraftContent,
  DraftPlatform,
  Platform,
  PostPatch,
  PublishPost,
} from './publish-models';

/** 圖卡與發布 API（合約 B 節）。 */
@Injectable({ providedIn: 'root' })
export class MediaApi {
  private readonly http = inject(HttpClient);

  /** version 省略取最新版摘要。沒摘要 → 404；有摘要沒渲染 → cards 是空陣列。 */
  getCards(videoId: string, version: number | null = null): Observable<CardSet> {
    const suffix = version === null ? '' : `?version=${version}`;
    return this.http.get<CardSet>(
      `/api/videos/${encodeURIComponent(videoId)}/cards${suffix}`,
    );
  }

  /**
   * preview=false 會寫進 media_assets；preview=true 只渲染到暫存目錄，
   * 回傳的 card.id 是 null、url 指向 30 分鐘後過期的 preview token。
   */
  render(
    videoId: string,
    version: number | null = null,
    preview = false,
  ): Observable<CardSet> {
    return this.http.post<CardSet>(
      `/api/videos/${encodeURIComponent(videoId)}/cards/render`,
      { version, preview },
    );
  }

  listPosts(videoId: string): Observable<PublishPost[]> {
    return this.http.get<PublishPost[]>(
      `/api/videos/${encodeURIComponent(videoId)}/posts`,
    );
  }

  publish(
    videoId: string,
    platforms: Platform[],
    version: number | null = null,
  ): Observable<PublishPost[]> {
    return this.http.post<PublishPost[]>(
      `/api/videos/${encodeURIComponent(videoId)}/publish`,
      { platforms, version },
    );
  }

  /** 手動回填 external_url / 標記 published。三個平台都 published 時後端自動推進影片狀態。 */
  patchPost(postId: number, body: PostPatch): Observable<PublishPost> {
    return this.http.patch<PublishPost>(`/api/posts/${postId}`, body);
  }

  /** 只有 vocus / medium 有草稿，其他平台後端回 400。 */
  getDraft(videoId: string, platform: DraftPlatform): Observable<DraftContent> {
    return this.http.get<DraftContent>(
      `/api/videos/${encodeURIComponent(videoId)}/drafts/${platform}`,
    );
  }
}
