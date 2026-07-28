import { Component, OnInit, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { MediaApi } from '../../core/media-api';
import {
  CardSet,
  DraftContent,
  DraftPlatform,
  MediaCard,
  Platform,
  PostPatch,
  PublishPost,
} from '../../core/publish-models';

interface PlatformMeta {
  key: Platform;
  label: string;
  /** 為什麼這個平台的最後一步一定是人工 —— 三個平台都是，原因各不相同。 */
  note: string;
  /** 有草稿檔可以複製的平台；null 代表沒有（Instagram 走圖卡不是文章）。 */
  draftKey: DraftPlatform | null;
}

/**
 * 三個平台的現實限制（見 CLAUDE.md 的「已知限制」）：
 * - Vocus 沒有官方發文 API
 * - Medium 官方 API 已停用，新申請拿不到 token
 * - Instagram Graph API 要求圖片是公開可存取的 URL，純本機沒有主機可放圖
 * 所以「發布」在這個系統裡一律只到「備妥」為止，真正貼出去是人做的。
 * 介面必須誠實反映這件事，不能做成按下去就自動發文的假象。
 */
const PLATFORMS: PlatformMeta[] = [
  {
    key: 'instagram',
    label: 'Instagram',
    note: '本機環境無法自動發文：Graph API 需要公開可存取的圖片 URL，純本機沒有主機可放圖。後端只會確認圖卡已渲染並標成 ready，實際發文要自己下載圖卡、在手機或網頁上傳，再回來填連結。',
    draftKey: null,
  },
  {
    key: 'vocus',
    label: '方格子 Vocus',
    note: '沒有官方發文 API，後端只產出 Markdown 草稿，複製後人工貼上發布。',
    draftKey: 'vocus',
  },
  {
    key: 'medium',
    label: 'Medium',
    note: '官方 API 已停用（新申請拿不到 token），同樣走草稿 + 人工發布。',
    draftKey: 'medium',
  },
];

/**
 * 圖卡預覽與發布頁。
 *
 * 圖卡是 1080x1350 的直式輪播，橫向排列是為了對應 IG 上實際的滑動順序 ——
 * 縮圖排成一列才看得出「第一張抓不抓得住人、最後一張有沒有收尾」。
 */
@Component({
  selector: 'app-publish',
  imports: [RouterLink],
  templateUrl: './publish.html',
  styleUrl: './publish.scss',
})
export class Publish implements OnInit {
  /** 路由參數（withComponentInputBinding） */
  readonly videoId = input.required<string>();

  private readonly api = inject(MediaApi);

  protected readonly platforms = PLATFORMS;

  protected readonly cardSet = signal<CardSet | null>(null);
  protected readonly cardsLoading = signal(true);
  protected readonly cardsError = signal<string | null>(null);
  /** 目前畫面上的圖卡是不是「預覽不存檔」的結果 —— 這種圖卡不會出現在發布流程裡。 */
  protected readonly isPreview = signal(false);
  protected readonly rendering = signal(false);

  protected readonly posts = signal<PublishPost[]>([]);
  protected readonly postsLoading = signal(true);
  protected readonly publishing = signal<string | null>(null);

  protected readonly drafts = signal<Record<string, DraftContent>>({});
  protected readonly draftOpen = signal<string | null>(null);

  /** post id → 編輯中的 external_url */
  protected readonly urlEdits = signal<Record<number, string>>({});

  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);

  /** 放大檢視的圖卡，null = 關閉。 */
  protected readonly zoomed = signal<MediaCard | null>(null);

  /** 使用者指定的摘要版本；null = 交給後端取最新版。 */
  protected readonly version = signal<number | null>(null);

  /** 版本輸入框的字串值（input.value 只吃 string，空字串代表「交給後端取最新版」）。 */
  protected readonly versionText = computed(() => {
    const v = this.version();
    return v === null ? '' : String(v);
  });

  protected readonly cards = computed<MediaCard[]>(() => this.cardSet()?.cards ?? []);
  protected readonly hasCards = computed(() => this.cards().length > 0);

  ngOnInit(): void {
    this.loadCards();
    this.loadPosts();
  }

  /* ---------- 圖卡 ---------- */

  private loadCards(): void {
    this.cardsLoading.set(true);
    this.cardsError.set(null);
    this.api.getCards(this.videoId(), this.version()).subscribe({
      next: (set) => {
        this.cardSet.set(set);
        this.isPreview.set(false);
        this.version.set(set.version);
        this.cardsLoading.set(false);
      },
      error: (err) => {
        const status = (err as { status?: number })?.status;
        // 404 = 這支影片還沒有摘要，那是「還沒輪到這一步」不是錯誤，講清楚下一步要做什麼
        this.cardsError.set(
          status === 404
            ? '這支影片還沒有摘要，圖卡要有摘要才渲染得出來。先去跑 SUMMARIZING 階段。'
            : `載入圖卡失敗：${this.detail(err)}`,
        );
        this.cardsLoading.set(false);
      },
    });
  }

  protected reloadCards(): void {
    this.loadCards();
  }

  protected onVersionInput(event: Event): void {
    const raw = (event.target as HTMLInputElement).value.trim();
    const n = Number(raw);
    this.version.set(raw && Number.isFinite(n) && n > 0 ? Math.floor(n) : null);
  }

  protected render(preview: boolean): void {
    this.rendering.set(true);
    this.notice.set(null);
    this.cardsError.set(null);
    this.api.render(this.videoId(), this.version(), preview).subscribe({
      next: (set) => {
        this.cardSet.set(set);
        this.isPreview.set(preview);
        this.version.set(set.version);
        this.rendering.set(false);
        this.notice.set(
          preview
            ? `已預覽 v${set.version} 的 ${set.cards.length} 張圖卡（未存檔，預覽連結約 30 分鐘後失效）。`
            : `已重新渲染 v${set.version} 的 ${set.cards.length} 張圖卡。`,
        );
        if (!preview) this.loadPosts();
      },
      error: (err) => {
        this.rendering.set(false);
        // FontNotFound / NoSummary 後端回 400 帶原訊息，那是唯一查得出缺什麼字型的線索
        this.cardsError.set(`渲染失敗：${this.detail(err)}`);
      },
    });
  }

  protected zoom(card: MediaCard): void {
    this.zoomed.set(card);
  }

  protected closeZoom(): void {
    this.zoomed.set(null);
  }

  /* ---------- 發布 ---------- */

  private loadPosts(): void {
    this.postsLoading.set(true);
    this.api.listPosts(this.videoId()).subscribe({
      next: (rows) => {
        this.posts.set(rows);
        const edits: Record<number, string> = {};
        for (const p of rows) edits[p.id] = p.external_url ?? '';
        this.urlEdits.set(edits);
        this.postsLoading.set(false);
      },
      error: (err) => {
        this.error.set(`載入發布狀態失敗：${this.detail(err)}`);
        this.postsLoading.set(false);
      },
    });
  }

  protected postOf(platform: Platform): PublishPost | null {
    return this.posts().find((p) => p.platform === platform) ?? null;
  }

  protected publish(platforms: Platform[]): void {
    if (this.isPreview()) {
      this.error.set('目前顯示的是未存檔的預覽圖卡，先按「重新渲染」存檔再發布。');
      return;
    }
    this.publishing.set(platforms.join(','));
    this.notice.set(null);
    this.api.publish(this.videoId(), platforms, this.version()).subscribe({
      next: (rows) => {
        this.posts.set(rows);
        const edits: Record<number, string> = { ...this.urlEdits() };
        for (const p of rows) edits[p.id] = p.external_url ?? edits[p.id] ?? '';
        this.urlEdits.set(edits);
        this.publishing.set(null);
        const blocked = rows.filter((p) => p.status === 'blocked');
        this.notice.set(
          blocked.length
            ? `已處理，但 ${blocked.map((p) => p.platform).join('、')} 被擋下（見下方原因）。`
            : '已備妥，接下來的貼上與上傳要人工完成。',
        );
      },
      error: (err) => {
        this.publishing.set(null);
        const status = (err as { status?: number })?.status;
        this.error.set(
          status === 409
            ? `無法發布：${this.detail(err)}（影片要先走到 RENDERING 才能推進 PUBLISHING）`
            : `發布失敗：${this.detail(err)}`,
        );
      },
    });
  }

  protected publishAll(): void {
    this.publish(['vocus', 'medium', 'instagram']);
  }

  /* ---------- 草稿 ---------- */

  protected toggleDraft(platform: DraftPlatform): void {
    if (this.draftOpen() === platform) {
      this.draftOpen.set(null);
      return;
    }
    this.draftOpen.set(platform);
    if (this.drafts()[platform]) return;

    this.api.getDraft(this.videoId(), platform).subscribe({
      next: (d) => this.drafts.update((m) => ({ ...m, [platform]: d })),
      error: (err) => this.error.set(`載入草稿失敗：${this.detail(err)}`),
    });
  }

  protected draftOf(platform: string): DraftContent | null {
    return this.drafts()[platform] ?? null;
  }

  protected copyDraft(platform: string): void {
    const d = this.drafts()[platform];
    if (!d) return;
    navigator.clipboard.writeText(d.content).then(
      () => this.notice.set(`${platform} 草稿全文已複製到剪貼簿。`),
      () => this.error.set('複製失敗，請手動選取草稿內容。'),
    );
  }

  /* ---------- 手動回填 ---------- */

  protected onUrlInput(postId: number, event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    this.urlEdits.update((m) => ({ ...m, [postId]: value }));
  }

  protected urlOf(postId: number): string {
    return this.urlEdits()[postId] ?? '';
  }

  protected saveUrl(post: PublishPost): void {
    const url = this.urlOf(post.id).trim();
    this.patch(post, { external_url: url || null }, '已儲存連結。');
  }

  /** 人工貼完文之後才按這個。三個平台都 published 時後端會自動把影片推到 PUBLISHED。 */
  protected markPublished(post: PublishPost): void {
    const url = this.urlOf(post.id).trim();
    if (!url && !confirm('沒有填外部連結就標記已發布嗎？之後不容易回頭找到那篇文。')) return;
    this.patch(
      post,
      { status: 'published', external_url: url || null, error: null },
      '已標記為已發布。',
    );
  }

  protected revertToReady(post: PublishPost): void {
    this.patch(post, { status: 'ready' }, '已退回 ready。');
  }

  private patch(post: PublishPost, body: PostPatch, okMessage: string): void {
    this.publishing.set(post.platform);
    this.api.patchPost(post.id, body).subscribe({
      next: (updated) => {
        this.posts.update((list) => list.map((p) => (p.id === updated.id ? updated : p)));
        this.urlEdits.update((m) => ({ ...m, [updated.id]: updated.external_url ?? '' }));
        this.publishing.set(null);
        this.notice.set(okMessage);
      },
      error: (err) => {
        this.publishing.set(null);
        this.error.set(`更新失敗：${this.detail(err)}`);
      },
    });
  }

  /* ---------- 顯示輔助 ---------- */

  protected statusLabel(post: PublishPost | null): string {
    if (!post) return '尚未建立';
    switch (post.status) {
      case 'draft':
        return '草稿';
      case 'ready':
        return '已備妥，等人工發布';
      case 'published':
        return '已發布';
      case 'blocked':
        return '被擋下';
      case 'failed':
        return '失敗';
      default:
        return post.status;
    }
  }

  protected busyFor(key: Platform): boolean {
    const p = this.publishing();
    return p !== null && p.includes(key);
  }

  protected dismissError(): void {
    this.error.set(null);
  }

  protected dismissNotice(): void {
    this.notice.set(null);
  }

  private detail(err: unknown): string {
    const e = err as { error?: { detail?: unknown }; message?: string };
    const d = e?.error?.detail;
    if (typeof d === 'string' && d) return d;
    if (d) return JSON.stringify(d);
    return e?.message ?? String(err);
  }
}
