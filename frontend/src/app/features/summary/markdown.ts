/** 極簡 Markdown 解析器：只認得 summarizer.py 會產出的那幾種語法。
 *
 * 不裝 marked/markdown-it 這類套件，也不用 innerHTML。理由有兩個：
 *
 * 1. 這裡要顯示的 Markdown 全是自己人產的（summarizer.build_markdown 的固定樣板
 *    加上 model 寫的正文），語法範圍很窄 —— 標題、段落、清單、引言、分隔線、
 *    粗體與連結，其他都用不到。為了這點需求拉一個依賴不划算。
 * 2. 內容裡混著 LLM 生成的文字，走 innerHTML 等於把 XSS 的門開著。解析成結構
 *    再交給 Angular 樣板渲染，文字一律是 text node，注入不了東西。
 *
 * 解析不出來的行一律當普通段落，寧可少渲染一點格式也不要吃掉內容。
 */

export type InlineKind = 'text' | 'strong' | 'link';

export interface Inline {
  kind: InlineKind;
  text: string;
  href?: string;
}

export type BlockKind = 'h1' | 'h2' | 'h3' | 'p' | 'li' | 'quote' | 'hr';

export interface Block {
  kind: BlockKind;
  parts: Inline[];
}

/** 拆出粗體 `**x**` 與連結 `[text](url)`，其餘當純文字。 */
function parseInline(text: string): Inline[] {
  const parts: Inline[] = [];
  // 一次掃過，兩種語法共用同一個正則，避免先處理粗體時把連結文字切壞
  const pattern = /\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)]+)\)/g;
  let last = 0;
  let m: RegExpExecArray | null;

  while ((m = pattern.exec(text)) !== null) {
    if (m.index > last) parts.push({ kind: 'text', text: text.slice(last, m.index) });
    if (m[1] !== undefined) {
      parts.push({ kind: 'strong', text: m[1] });
    } else {
      parts.push({ kind: 'link', text: m[2], href: m[3] });
    }
    last = pattern.lastIndex;
  }
  if (last < text.length) parts.push({ kind: 'text', text: text.slice(last) });
  return parts.length ? parts : [{ kind: 'text', text }];
}

export function parseMarkdown(source: string): Block[] {
  const blocks: Block[] = [];
  // 段落不做跨行合併：逐字稿改寫出來的正文本來就是一行一段，
  // 硬合併反而會把 model 刻意分開的短句黏在一起。
  for (const rawLine of (source ?? '').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;

    if (/^---+$/.test(line)) {
      blocks.push({ kind: 'hr', parts: [] });
      continue;
    }

    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      const kind = (['h1', 'h2', 'h3'] as const)[heading[1].length - 1];
      blocks.push({ kind, parts: parseInline(heading[2]) });
      continue;
    }

    const quote = /^>\s?(.*)$/.exec(line);
    if (quote) {
      blocks.push({ kind: 'quote', parts: parseInline(quote[1]) });
      continue;
    }

    const item = /^[-*]\s+(.*)$/.exec(line);
    if (item) {
      blocks.push({ kind: 'li', parts: parseInline(item[1]) });
      continue;
    }

    blocks.push({ kind: 'p', parts: parseInline(line) });
  }
  return blocks;
}
