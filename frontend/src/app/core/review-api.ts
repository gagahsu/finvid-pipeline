import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

import { Correction, ReviewAction, TranscriptDetail, VideoSummary } from './models';

/** 審核後台 API 的薄封裝。路徑走相對的 /api，由 dev server proxy 轉到 FastAPI。 */
@Injectable({ providedIn: 'root' })
export class ReviewApi {
  private readonly http = inject(HttpClient);

  listVideos(): Observable<VideoSummary[]> {
    return this.http.get<VideoSummary[]>('/api/videos');
  }

  getTranscript(videoId: string): Observable<TranscriptDetail> {
    return this.http.get<TranscriptDetail>(
      `/api/videos/${encodeURIComponent(videoId)}/transcript`,
    );
  }

  listCorrections(videoId: string): Observable<Correction[]> {
    return this.http.get<Correction[]>(
      `/api/videos/${encodeURIComponent(videoId)}/corrections`,
    );
  }

  review(correctionId: number, action: ReviewAction): Observable<Correction> {
    return this.http.patch<Correction>(`/api/corrections/${correctionId}`, { action });
  }
}
