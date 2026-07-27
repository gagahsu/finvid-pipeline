import { DecimalPipe } from '@angular/common';
import { Component, OnInit, computed, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { forkJoin } from 'rxjs';

import { ReviewApi } from '../../core/review-api';
import { Correction, ReviewAction, SegmentDiff, TranscriptDetail } from '../../core/models';

/** 逐字稿的顯示單位：一段文字加上它的標記類型。 */
type MarkKind = 'plain' | 'del' | 'ins' | 'pending';

interface Part {
  text: string;
  kind: MarkKind;
}

interface Row {
  index: number;
  time: string;
  rawParts: Part[];
  correctedParts: Part[];
  corrections: Correction[];
  changed: boolean;
  pending: boolean;
}

type Filter = 'pending' | 'changed' | 'all';

/** 把 text 依照 marks 切成帶標記的片段。長詞先標，避免短詞把長詞切壞。 */
function markup(text: string, marks: { word: string; kind: MarkKind }[]): Part[] {
  let parts: Part[] = [{ text, kind: 'plain' }];
  const sorted = [...marks].sort((a, b) => b.word.length - a.word.length);

  for (const { word, kind } of sorted) {
    if (!word) continue;
    const next: Part[] = [];
    for (const part of parts) {
      if (part.kind !== 'plain' || !part.text.includes(word)) {
        next.push(part);
        continue;
      }
      const pieces = part.text.split(word);
      pieces.forEach((piece, i) => {
        if (piece) next.push({ text: piece, kind: 'plain' });
        if (i < pieces.length - 1) next.push({ text: word, kind });
      });
    }
    parts = next;
  }
  return parts;
}

/** 依目前狀態把該套用的修正套進一句逐字稿。規則與 backend 的 _forward_apply 一致。 */
function applyCorrections(rawText: string, corrections: Correction[]): string {
  const applicable = corrections
    .filter((c) => c.applied && c.corrected)
    .sort((a, b) => b.original.length - a.original.length);

  let text = rawText;
  for (const c of applicable) {
    text = text.split(c.original).join(c.corrected!);
  }
  return text;
}

function formatTime(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return '';
  const total = Math.floor(seconds);
  const mm = String(Math.floor(total / 60)).padStart(2, '0');
  const ss = String(total % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

/** 逐字稿審核器：左右對照原始與校正後逐字稿，逐項接受／還原修正。 */
@Component({
  selector: 'app-reviewer',
  imports: [RouterLink, DecimalPipe],
  templateUrl: './reviewer.html',
  styleUrl: './reviewer.scss',
})
export class Reviewer implements OnInit {
  /** 路由參數（withComponentInputBinding） */
  readonly videoId = input.required<string>();

  private readonly api = inject(ReviewApi);

  protected readonly transcript = signal<TranscriptDetail | null>(null);
  protected readonly corrections = signal<Correction[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  protected readonly busyId = signal<number | null>(null);

  protected readonly filter = signal<Filter>('pending');
  /**
   * 進入畫面時待審的 segment。做完判定後那筆就不再是 needs_review，
   * 若直接依現況篩選，剛按下的那一列會馬上消失、想反悔也找不到，
   * 所以用載入當下的快照固定住清單。
   */
  private readonly pendingSegments = signal<ReadonlySet<number>>(new Set());
  /** LLM 已判定「非股票詞」的建議，預設收起來，需要稽核時才展開 */
  protected readonly showExcluded = signal(false);

  ngOnInit(): void {
    this.load();
  }

  private load(): void {
    this.loading.set(true);
    forkJoin({
      transcript: this.api.getTranscript(this.videoId()),
      corrections: this.api.listCorrections(this.videoId()),
    }).subscribe({
      next: ({ transcript, corrections }) => {
        this.transcript.set(transcript);
        this.corrections.set(corrections);
        this.pendingSegments.set(
          new Set(
            corrections
              .filter((c) => this.isPending(c) && c.segment_index !== null)
              .map((c) => c.segment_index as number),
          ),
        );
        this.loading.set(false);
        // 沒有待審項目時直接顯示所有有修正的句子，免得畫面一片空白
        if (!corrections.some((c) => this.isPending(c))) {
          this.filter.set('changed');
        }
      },
      error: (err) => {
        this.error.set(`載入失敗：${err.message ?? err}`);
        this.loading.set(false);
      },
    });
  }

  private isPending(c: Correction): boolean {
    return c.status === 'needs_review' && !c.human_reviewed;
  }

  /** LLM 判斷不需替換、也還沒有人覆核的建議 —— 屬於雜訊，預設不顯示 */
  private isExcluded(c: Correction): boolean {
    return c.status === 'rejected' && !c.human_reviewed;
  }

  protected readonly stats = computed(() => {
    const all = this.corrections();
    return {
      total: all.length,
      applied: all.filter((c) => c.applied).length,
      pending: all.filter((c) => this.isPending(c)).length,
      reviewed: all.filter((c) => c.human_reviewed).length,
      excluded: all.filter((c) => this.isExcluded(c)).length,
    };
  });

  /** correction 依 segment_index 分組（已濾掉不顯示的） */
  private readonly bySegment = computed(() => {
    const map = new Map<number, Correction[]>();
    const showExcluded = this.showExcluded();
    for (const c of this.corrections()) {
      if (c.segment_index === null) continue;
      if (!showExcluded && this.isExcluded(c)) continue;
      const list = map.get(c.segment_index) ?? [];
      list.push(c);
      map.set(c.segment_index, list);
    }
    return map;
  });

  protected readonly rows = computed<Row[]>(() => {
    const t = this.transcript();
    if (!t) return [];
    const bySegment = this.bySegment();
    const filter = this.filter();
    const pendingAtLoad = this.pendingSegments();

    const wanted = t.segments.filter((seg) => {
      const list = bySegment.get(seg.index) ?? [];
      if (filter === 'all') return true;
      if (filter === 'changed') return list.length > 0;
      return pendingAtLoad.has(seg.index) || list.some((c) => this.isPending(c));
    });

    return wanted.map((seg) => this.toRow(seg, bySegment.get(seg.index) ?? []));
  });

  private toRow(seg: SegmentDiff, corrections: Correction[]): Row {
    const correctedText = applyCorrections(seg.raw_text, corrections);

    const rawMarks = corrections
      .filter((c) => c.applied || this.isPending(c))
      .map((c) => ({
        word: c.original,
        kind: (c.applied ? 'del' : 'pending') as MarkKind,
      }));

    const correctedMarks = corrections
      .map((c) =>
        c.applied && c.corrected
          ? { word: c.corrected, kind: 'ins' as MarkKind }
          : this.isPending(c)
            ? { word: c.original, kind: 'pending' as MarkKind }
            : null,
      )
      .filter((m): m is { word: string; kind: MarkKind } => m !== null);

    return {
      index: seg.index,
      time: formatTime(seg.start),
      rawParts: markup(seg.raw_text, rawMarks),
      correctedParts: markup(correctedText, correctedMarks),
      corrections,
      changed: correctedText !== seg.raw_text,
      pending: corrections.some((c) => this.isPending(c)),
    };
  }

  protected review(correction: Correction, action: ReviewAction): void {
    this.busyId.set(correction.id);
    this.api.review(correction.id, action).subscribe({
      next: (updated) => {
        this.corrections.update((list) =>
          list.map((c) => (c.id === updated.id ? updated : c)),
        );
        this.busyId.set(null);
      },
      error: (err) => {
        this.error.set(`更新失敗：${err.error?.detail ?? err.message ?? err}`);
        this.busyId.set(null);
      },
    });
  }

  protected setFilter(value: Filter): void {
    this.filter.set(value);
  }

  protected toggleExcluded(): void {
    this.showExcluded.update((v) => !v);
  }

  protected confidencePercent(c: Correction): string {
    return c.confidence === null ? '—' : `${Math.round(c.confidence * 100)}%`;
  }

  protected statusLabel(c: Correction): string {
    if (c.human_reviewed) {
      return c.status === 'rejected' ? '人工已還原' : '人工已接受';
    }
    if (c.status === 'auto') return '自動套用';
    if (c.status === 'needs_review') return '待人工確認';
    return 'LLM 判定不替換';
  }

  protected dismissError(): void {
    this.error.set(null);
  }
}
