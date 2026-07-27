import { Component, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ReviewApi } from '../../core/review-api';
import { VideoSummary } from '../../core/models';

/** 影片清單：列出所有已有逐字稿的影片與審核進度。 */
@Component({
  selector: 'app-video-list',
  imports: [RouterLink],
  templateUrl: './video-list.html',
  styleUrl: './video-list.scss',
})
export class VideoList {
  private readonly api = inject(ReviewApi);

  protected readonly videos = signal<VideoSummary[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);

  constructor() {
    this.api.listVideos().subscribe({
      next: (videos) => {
        this.videos.set(videos);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(`載入失敗：${err.message ?? err}`);
        this.loading.set(false);
      },
    });
  }
}
