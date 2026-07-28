import { Component, OnDestroy, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { SourcesApi } from '../../core/sources-api';
import { PollResult, Source, SourceType } from '../../core/publish-models';

/** backfill 預設拉幾支歷史影片。RSS 只給最新約 15 支，初始化訂閱時要靠 yt-dlp 補。 */
const DEFAULT_BACKFILL_LIMIT = 30;

/**
 * 訂閱來源管理。
 *
 * 這個頁面最大的設計難題是「輪詢很慢」：YouTube 的 feeds server 會間歇回 404/500，
 * 後端 rss.fetch_feed 預設重試 10 次，最壞要等 40 秒才回來，全部輪詢更是數倍。
 * 這是實測過的環境問題不是 bug，所以 UI 一律：
 *   1. 動作前就先寫明「可能要等將近一分鐘」
 *   2. 進行中顯示逐秒累加的計時，讓人看得出它還活著
 *   3. 結果逐來源列出「新增幾支」或錯誤訊息，不要只給一句成功／失敗
 */
@Component({
  selector: 'app-sources',
  imports: [RouterLink],
  templateUrl: './sources.html',
  styleUrl: './sources.scss',
})
export class Sources implements OnDestroy {
  private readonly api = inject(SourcesApi);

  protected readonly sources = signal<Source[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);

  /** source id → 正在進行的動作說明；同時當作該列的 disabled 依據。 */
  protected readonly busy = signal<Record<number, string>>({});
  protected readonly pollingAll = signal(false);

  /** channel_id → 最近一次輪詢結果，逐來源顯示在該列底下。 */
  protected readonly pollResults = signal<Record<string, PollResult>>({});

  /** 新增表單 */
  protected readonly newTarget = signal('');
  protected readonly newType = signal<SourceType>('channel');
  protected readonly newName = signal('');
  protected readonly creating = signal(false);

  protected readonly backfillLimit = signal(DEFAULT_BACKFILL_LIMIT);

  /** input.value 只吃 string，所以綁字串版本。 */
  protected readonly backfillLimitText = computed(() => String(this.backfillLimit()));

  /** 輪詢進行中的秒數。只有計時顯示用，不影響任何邏輯。 */
  protected readonly elapsed = signal(0);
  private ticker: ReturnType<typeof setInterval> | null = null;

  /** 任何輪詢在跑時，其他輪詢鈕一律鎖住 —— 同時打好幾個慢請求只會更慢更難懂。 */
  protected readonly anyPolling = computed(
    () => this.pollingAll() || Object.values(this.busy()).some((label) => label === '輪詢中'),
  );

  protected readonly activeCount = computed(
    () => this.sources().filter((s) => s.active).length,
  );

  constructor() {
    this.load();
  }

  ngOnDestroy(): void {
    this.stopTimer();
  }

  private load(): void {
    this.loading.set(true);
    this.api.list().subscribe({
      next: (rows) => {
        this.sources.set(rows);
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(`載入來源失敗：${this.detail(err)}`);
        this.loading.set(false);
      },
    });
  }

  protected reload(): void {
    this.load();
  }

  /* ---------- 新增 ---------- */

  protected onTargetInput(event: Event): void {
    this.newTarget.set((event.target as HTMLInputElement).value);
  }

  protected onNameInput(event: Event): void {
    this.newName.set((event.target as HTMLInputElement).value);
  }

  protected onTypeChange(event: Event): void {
    this.newType.set((event.target as HTMLSelectElement).value as SourceType);
  }

  protected onLimitInput(event: Event): void {
    const n = Number((event.target as HTMLInputElement).value);
    this.backfillLimit.set(Number.isFinite(n) && n > 0 ? Math.floor(n) : DEFAULT_BACKFILL_LIMIT);
  }

  protected create(): void {
    const target = this.newTarget().trim();
    if (!target) {
      this.error.set('請先貼上 channel_id、頻道網址或 @handle。');
      return;
    }
    this.creating.set(true);
    this.notice.set(null);
    this.api
      .create({ target, type: this.newType(), name: this.newName().trim() || null })
      .subscribe({
        next: (created) => {
          this.creating.set(false);
          this.newTarget.set('');
          this.newName.set('');
          this.notice.set(`已新增來源 ${created.name || created.channel_id}。`);
          // 回應少了 video_count 這些統計欄位，直接重抓清單比手工拼一列可靠
          this.load();
        },
        error: (err) => {
          this.creating.set(false);
          this.error.set(`新增失敗：${this.detail(err)}`);
        },
      });
  }

  /* ---------- 改名 / 啟用停用 / 刪除 ---------- */

  protected rename(source: Source): void {
    const next = prompt('新的顯示名稱', source.name ?? '');
    if (next === null) return;
    const name = next.trim();
    if (!name || name === (source.name ?? '')) return;

    this.setBusy(source.id, '更新中');
    this.api.update(source.id, { name }).subscribe({
      next: (updated) => {
        this.replace(updated);
        this.clearBusy(source.id);
        this.notice.set(`已改名為「${name}」。`);
      },
      error: (err) => {
        this.clearBusy(source.id);
        this.error.set(`改名失敗：${this.detail(err)}`);
      },
    });
  }

  protected toggleActive(source: Source): void {
    this.setBusy(source.id, '更新中');
    this.api.update(source.id, { active: !source.active }).subscribe({
      next: (updated) => {
        this.replace(updated);
        this.clearBusy(source.id);
        this.notice.set(
          `${updated.name || updated.channel_id} 已${updated.active ? '啟用' : '停用'}。`,
        );
      },
      error: (err) => {
        this.clearBusy(source.id);
        this.error.set(`更新失敗：${this.detail(err)}`);
      },
    });
  }

  protected remove(source: Source): void {
    const label = source.name || source.channel_id;
    if (!confirm(`確定要刪除來源「${label}」嗎？這是硬刪，不會連帶刪影片。`)) return;

    this.setBusy(source.id, '刪除中');
    this.api.remove(source.id).subscribe({
      next: () => {
        this.sources.update((list) => list.filter((s) => s.id !== source.id));
        this.clearBusy(source.id);
        this.notice.set(`已刪除來源「${label}」。`);
      },
      error: (err) => {
        this.clearBusy(source.id);
        // 底下還掛著影片時後端回 409，detail 會說明；不要吞掉，也順手提示替代做法
        const status = (err as { status?: number })?.status;
        const detail = this.detail(err);
        this.error.set(
          status === 409
            ? `無法刪除：${detail} 建議改按「停用」，影片紀錄才不會變成孤兒指標。`
            : `刪除失敗：${detail}`,
        );
      },
    });
  }

  /* ---------- 輪詢 / backfill ---------- */

  protected poll(source: Source): void {
    this.setBusy(source.id, '輪詢中');
    this.notice.set(null);
    this.startTimer();
    this.api.poll(source.id).subscribe({
      next: (result) => {
        this.pollResults.update((m) => ({ ...m, [result.channel_id]: result }));
        this.clearBusy(source.id);
        this.stopTimer();
        if (result.error) {
          this.error.set(`${result.name || result.channel_id} 輪詢失敗：${result.error}`);
        } else {
          this.notice.set(
            `${result.name || result.channel_id}：讀到 ${result.total} 筆，` +
              `新增 ${result.new_ids.length} 支。`,
          );
          if (result.new_ids.length) this.load();
        }
      },
      error: (err) => {
        this.clearBusy(source.id);
        this.stopTimer();
        this.error.set(`輪詢失敗：${this.detail(err)}`);
      },
    });
  }

  protected pollAll(): void {
    this.pollingAll.set(true);
    this.notice.set(null);
    this.startTimer();
    this.api.pollAll().subscribe({
      next: (res) => {
        const map: Record<string, PollResult> = {};
        for (const r of res.results) map[r.channel_id] = r;
        this.pollResults.set(map);
        this.pollingAll.set(false);
        this.stopTimer();

        const failed = res.results.filter((r) => r.error).length;
        this.notice.set(
          `輪詢完成：${res.results.length} 個來源，共新增 ${res.new_ids.length} 支影片` +
            (failed ? `，其中 ${failed} 個來源失敗（見各列訊息）。` : '。'),
        );
        if (res.new_ids.length) this.load();
      },
      error: (err) => {
        this.pollingAll.set(false);
        this.stopTimer();
        this.error.set(`輪詢全部失敗：${this.detail(err)}`);
      },
    });
  }

  protected backfill(source: Source): void {
    const limit = this.backfillLimit();
    if (!confirm(`要用 yt-dlp 拉「${source.name || source.channel_id}」最近 ${limit} 支歷史影片嗎？`))
      return;

    this.setBusy(source.id, '拉取歷史中');
    this.notice.set(null);
    this.startTimer();
    this.api.backfill(source.id, limit).subscribe({
      next: (res) => {
        this.clearBusy(source.id);
        this.stopTimer();
        this.notice.set(`已補入 ${res.count} 支歷史影片。`);
        if (res.count) this.load();
      },
      error: (err) => {
        this.clearBusy(source.id);
        this.stopTimer();
        // yt-dlp 不在 PATH 之類的環境問題後端會回 400 帶原訊息，要原樣顯示才查得出來
        this.error.set(`拉取歷史失敗：${this.detail(err)}`);
      },
    });
  }

  /* ---------- 顯示輔助 ---------- */

  protected busyLabel(id: number): string | null {
    return this.busy()[id] ?? null;
  }

  protected resultOf(source: Source): PollResult | null {
    return this.pollResults()[source.channel_id] ?? null;
  }

  protected typeLabel(type: SourceType): string {
    return type === 'playlist' ? '播放清單' : '頻道';
  }

  protected dismissError(): void {
    this.error.set(null);
  }

  protected dismissNotice(): void {
    this.notice.set(null);
  }

  private replace(updated: Source): void {
    this.sources.update((list) => list.map((s) => (s.id === updated.id ? updated : s)));
  }

  private setBusy(id: number, label: string): void {
    this.busy.update((m) => ({ ...m, [id]: label }));
  }

  private clearBusy(id: number): void {
    this.busy.update((m) => {
      const next = { ...m };
      delete next[id];
      return next;
    });
  }

  private startTimer(): void {
    this.stopTimer();
    this.elapsed.set(0);
    this.ticker = setInterval(() => this.elapsed.update((v) => v + 1), 1000);
  }

  private stopTimer(): void {
    if (this.ticker !== null) {
      clearInterval(this.ticker);
      this.ticker = null;
    }
  }

  /** 後端錯誤一律優先顯示 FastAPI 的 detail（409/400 的說明都在那裡），不要吞掉。 */
  private detail(err: unknown): string {
    const e = err as { error?: { detail?: unknown }; message?: string };
    const d = e?.error?.detail;
    if (typeof d === 'string' && d) return d;
    if (d) return JSON.stringify(d);
    return e?.message ?? String(err);
  }
}
