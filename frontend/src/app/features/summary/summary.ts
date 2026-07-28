import { Component, OnInit, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ReviewApi } from '../../core/review-api';
import { SummaryDetail, SummaryVersion } from '../../core/models';
import { Block, parseMarkdown } from './markdown';

/** IG 貼文文案的字數上限（Graph API 的硬限制），超過就發不出去，要在審核台先擋。 */
const IG_CAPTION_LIMIT = 2200;

/**
 * 摘要審核器：看每個版本的 Markdown 草稿與 IG 文案，就地編輯後存成新版本。
 *
 * 「存成新版本而非覆寫」是 summaries 表的設計（見 backend/app/db.py 的註解），
 * 所以介面上刻意把版本清單擺在最顯眼的地方 —— 人要看得出自己剛才存的是第幾版、
 * 上一版還在。改壞了不必復原，切回舊版就好。
 */
@Component({
  selector: 'app-summary',
  imports: [RouterLink],
  templateUrl: './summary.html',
  styleUrl: './summary.scss',
})
export class Summary implements OnInit {
  /** 路由參數（withComponentInputBinding） */
  readonly videoId = input.required<string>();

  private readonly api = inject(ReviewApi);

  protected readonly versions = signal<SummaryVersion[]>([]);
  protected readonly current = signal<SummaryDetail | null>(null);
  protected readonly loading = signal(true);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);

  /** 編輯中的內容。載入版本時從 current 覆寫回去，等於「捨棄未存的修改」。 */
  protected readonly draftTitle = signal('');
  protected readonly draftContent = signal('');
  protected readonly draftIgCaption = signal('');

  protected readonly editing = signal(false);

  protected readonly igLimit = IG_CAPTION_LIMIT;

  ngOnInit(): void {
    this.load();
  }

  private load(): void {
    this.loading.set(true);
    this.api.listSummaries(this.videoId()).subscribe({
      next: (versions) => {
        this.versions.set(versions);
        if (versions.length === 0) {
          this.loading.set(false);
          return;
        }
        // 清單已依 version DESC 排序，第一筆就是最新版
        this.select(versions[0].id);
      },
      error: (err) => {
        this.error.set(`載入失敗：${err.message ?? err}`);
        this.loading.set(false);
      },
    });
  }

  protected select(summaryId: number): void {
    if (this.dirty() && !confirm('尚未儲存的修改會被捨棄，確定要切換版本嗎？')) return;

    this.loading.set(true);
    this.api.getSummary(summaryId).subscribe({
      next: (detail) => {
        this.current.set(detail);
        this.resetDraft(detail);
        this.editing.set(false);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(`載入版本失敗：${err.message ?? err}`);
        this.loading.set(false);
      },
    });
  }

  private resetDraft(detail: SummaryDetail): void {
    this.draftTitle.set(detail.title ?? '');
    this.draftContent.set(detail.content);
    this.draftIgCaption.set(detail.ig_caption ?? '');
  }

  /** 編輯內容與目前版本是否有差異 —— 決定存檔鈕能不能按、切版本要不要攔一下。 */
  protected readonly dirty = computed(() => {
    const c = this.current();
    if (!c) return false;
    return (
      this.draftTitle() !== (c.title ?? '') ||
      this.draftContent() !== c.content ||
      this.draftIgCaption() !== (c.ig_caption ?? '')
    );
  });

  /** 預覽用的 Markdown 區塊。編輯中就即時預覽草稿，否則顯示已存的版本。 */
  protected readonly blocks = computed<Block[]>(() =>
    parseMarkdown(this.editing() ? this.draftContent() : (this.current()?.content ?? '')),
  );

  protected readonly igText = computed(() =>
    this.editing() ? this.draftIgCaption() : (this.current()?.ig_caption ?? ''),
  );

  protected readonly igOverLimit = computed(() => this.igText().length > IG_CAPTION_LIMIT);

  protected toggleEditing(): void {
    if (this.editing() && this.dirty()) {
      if (!confirm('尚未儲存的修改會被捨棄，確定要離開編輯嗎？')) return;
      const c = this.current();
      if (c) this.resetDraft(c);
    }
    this.editing.update((v) => !v);
  }

  protected onTitleInput(event: Event): void {
    this.draftTitle.set((event.target as HTMLInputElement).value);
  }

  protected onContentInput(event: Event): void {
    this.draftContent.set((event.target as HTMLTextAreaElement).value);
  }

  protected onIgInput(event: Event): void {
    this.draftIgCaption.set((event.target as HTMLTextAreaElement).value);
  }

  protected save(): void {
    if (!this.draftContent().trim()) {
      this.error.set('正文不能是空的。');
      return;
    }
    this.saving.set(true);
    this.notice.set(null);
    this.api
      .saveSummary(this.videoId(), {
        title: this.draftTitle().trim() || null,
        content: this.draftContent(),
        ig_caption: this.draftIgCaption() || null,
      })
      .subscribe({
        next: (created) => {
          // 後端回的是剛建立的版本，直接插到清單最前面，不必重抓一次清單
          this.versions.update((list) => [this.toVersionRow(created), ...list]);
          this.current.set(created);
          this.resetDraft(created);
          this.editing.set(false);
          this.saving.set(false);
          this.notice.set(`已存成第 ${created.version} 版。`);
        },
        error: (err) => {
          this.error.set(`儲存失敗：${err.error?.detail ?? err.message ?? err}`);
          this.saving.set(false);
        },
      });
  }

  private toVersionRow(detail: SummaryDetail): SummaryVersion {
    const { content, ig_caption, verbatim_overlap, ...rest } = detail;
    return rest;
  }

  protected copy(text: string, label: string): void {
    // Vocus / Medium 沒有發文 API，草稿註定要人工貼上，複製鈕是這條路上必經的一步
    navigator.clipboard.writeText(text).then(
      () => this.notice.set(`${label}已複製到剪貼簿。`),
      () => this.error.set('複製失敗，請手動選取。'),
    );
  }

  protected modelLabel(v: SummaryVersion): string {
    if (v.human_edited) return '人工編輯';
    return v.model || '未知來源';
  }

  protected dismissError(): void {
    this.error.set(null);
  }

  protected dismissNotice(): void {
    this.notice.set(null);
  }
}
