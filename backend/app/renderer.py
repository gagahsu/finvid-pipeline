"""IG 圖卡渲染（RENDERING 階段）。

吃 summaries.content（Markdown 草稿），輸出一組 1080x1350 的 PNG：

    01 封面      標題
    02..n 內容   正文分頁
    n+1 免責     免責聲明 + 影片來源

每張圖以「相對於專案根目錄」的路徑寫進 media_assets，圖檔本身放 data/
底下不進版控（.gitignore 已整個忽略 data/）。

為什麼用 Pillow 而不是 HTML→截圖：CLAUDE.md 的技術選型把 Pillow 排在前面，
而且圖卡版型固定、不需要 CSS 排版能力，多裝一個 Chromium 只為了畫幾個文字方塊
不划算。代價是斷行要自己算 —— 見 wrap_text()。

中文字型是硬需求，不是選配：找不到就直接拋 FontNotFound。Pillow 的
load_default() 沒有 CJK glyph，靜默降級的結果是整張圖都是方框，
而那種圖看起來「有產出」，會一路混到人工審核才被發現。
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.db import connect

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CARDS_DIR = PROJECT_ROOT / "data" / "cards"

# IG 貼文的最大直式尺寸（4:5）。再高會被裁切。
CARD_W, CARD_H = 1080, 1350

MARGIN_X = 90
MARGIN_TOP = 140
MARGIN_BOTTOM = 150

BG = (16, 24, 40)
FG = (238, 242, 248)
MUTED = (150, 163, 184)
ACCENT = (245, 183, 66)

TITLE_SIZE = 66
HEADING_SIZE = 46
BODY_SIZE = 38
SMALL_SIZE = 28

LINE_RATIO = 1.55  # 行高倍率；中文字密，行距比英文需要更鬆
BLOCK_GAP = 28

DISCLAIMER = (
    "本內容為 YouTube 影片的重點整理，不代表本帳號立場，"
    "亦不構成任何投資建議。投資有風險，請自行判斷並承擔決策結果。"
)

# 依序尋找的中文字型（正常體, 粗體）。都是 Windows 內建，不必額外安裝。
# 微軟正黑體優先：它是繁中 UI 預設字型，字面比細明體現代，縮到小字仍清楚。
FONT_CANDIDATES = [
    ("msjh.ttc", "msjhbd.ttc"),
    ("msyh.ttc", "msyhbd.ttc"),
    ("NotoSansTC-VF.ttf", "NotoSansTC-VF.ttf"),
    ("NotoSansHK-VF.ttf", "NotoSansHK-VF.ttf"),
    ("mingliu.ttc", "mingliub.ttc"),
    ("simsun.ttc", "simsun.ttc"),
]

FONT_DIRS = [
    Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
    Path("/usr/share/fonts/truetype"),
    Path("/System/Library/Fonts"),
]


class FontNotFound(RuntimeError):
    """找不到可用的中文字型。"""


class NoSummary(RuntimeError):
    """summaries 表裡沒有這支影片（或指定版本）的摘要。"""


# --- 字型 ---------------------------------------------------------------


def _find_font_file(name: str) -> Path | None:
    for d in FONT_DIRS:
        if not d or not d.is_dir():
            continue
        p = d / name
        if p.is_file():
            return p
    return None


def resolve_fonts() -> tuple[Path, Path]:
    """回傳 (regular, bold) 字型檔路徑；一組都找不到就拋 FontNotFound。

    是「整組一起找」而不是正常體與粗體各自獨立找：兩者混用不同字族時
    標題與內文的字面寬度會對不上，版面看起來像出錯。
    """
    for regular, bold in FONT_CANDIDATES:
        r = _find_font_file(regular)
        if r is None:
            continue
        return r, (_find_font_file(bold) or r)
    tried = ", ".join(r for r, _ in FONT_CANDIDATES)
    raise FontNotFound(
        "找不到可用的中文字型，圖卡會整片變成方框。已嘗試："
        f"{tried}（搜尋路徑：{', '.join(str(d) for d in FONT_DIRS if d)}）"
    )


@dataclass
class Fonts:
    title: ImageFont.FreeTypeFont
    heading: ImageFont.FreeTypeFont
    body: ImageFont.FreeTypeFont
    small: ImageFont.FreeTypeFont


def load_fonts() -> Fonts:
    regular, bold = resolve_fonts()
    return Fonts(
        title=ImageFont.truetype(str(bold), TITLE_SIZE),
        heading=ImageFont.truetype(str(bold), HEADING_SIZE),
        body=ImageFont.truetype(str(regular), BODY_SIZE),
        small=ImageFont.truetype(str(regular), SMALL_SIZE),
    )


# --- 斷行 ---------------------------------------------------------------

# 不能出現在行首的標點（中文避頭點）。斷在這些字之前會讓標點孤零零掉到下一行。
NO_LINE_START = "，。、；：！？）」』】〉》%,.;:!?)]}"

_ASCII_WORD = re.compile(r"[A-Za-z0-9@#$&_./%\-']+")


def tokenize(text: str) -> list[str]:
    """把一行切成斷行單位：中文逐字，英數連續片段當一個單位。

    中文沒有空白可以當斷點，用英文的 split() 斷詞會整段擠成一個超長 token，
    最後不是溢出畫面就是被硬切在奇怪的位置。反過來英數不能逐字切 ——
    股票代號「00981A」被拆成兩行就失去意義了。
    """
    tokens: list[str] = []
    i = 0
    while i < len(text):
        m = _ASCII_WORD.match(text, i)
        if m:
            tokens.append(m.group())
            i = m.end()
        else:
            tokens.append(text[i])
            i += 1
    return tokens


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """逐字量測換行。回傳每行的字串。"""
    lines: list[str] = []
    current = ""
    for token in tokenize(text):
        if token == " " and not current:
            continue  # 行首的空白不留
        # 單一 token 就比整行還寬（超長網址、無空白的英數串）只能硬切，
        # 否則它會整段畫出畫面外
        while font.getlength(token) > max_width and len(token) > 1:
            cut = len(token)
            while cut > 1 and font.getlength(token[:cut]) > max_width:
                cut -= 1
            if current:
                lines.append(current)
                current = ""
            lines.append(token[:cut])
            token = token[cut:]
        candidate = current + token
        if current and font.getlength(candidate) > max_width:
            # 換行後行首是標點時，把標點留在上一行（寧可稍微超寬也不讓標點掉行首）
            if token in NO_LINE_START:
                lines.append(candidate)
                current = ""
                continue
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def line_height(font: ImageFont.FreeTypeFont) -> int:
    return int(font.size * LINE_RATIO)


# --- Markdown → 版面區塊 ------------------------------------------------


@dataclass
class Block:
    kind: str  # heading / para / bullet
    text: str


_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _clean(line: str) -> str:
    line = _LINK.sub(r"\1", line)
    line = _BOLD.sub(r"\1", line)
    return line.replace("`", "").strip()


def parse_blocks(markdown: str) -> list[Block]:
    """把摘要 Markdown 拆成圖卡要排的區塊。

    只取正文：`---` 之後是 summarizer 補的來源連結與免責聲明，那兩項在
    最後一張卡有自己的版面，混進內容頁會重複。

    段落用「空行才算結束」而不是「一行一段」：Markdown 允許把一個段落
    軟換行成多行，照行拆的話同一句話會被切成兩個段落，圖卡上看起來像
    中間漏字。
    """
    blocks: list[Block] = []
    para: list[str] = []

    def flush_para() -> None:
        if not para:
            return
        # 中文接中文不補空白（補了會多一個字寬的洞），英數交界才補
        text = para[0]
        for nxt in para[1:]:
            sep = " " if (text[-1].isascii() and nxt[0].isascii()) else ""
            text += sep + nxt
        blocks.append(Block("para", text))
        para.clear()

    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("---"):
            break
        if not line:
            flush_para()
            continue
        if line.startswith("#"):
            flush_para()
            text = _clean(line.lstrip("#"))
            # 第一個 H1 是文章標題，封面卡已經印過了
            if text and not (raw.startswith("# ") and not blocks):
                blocks.append(Block("heading", text))
            continue
        if re.match(r"^([-*+]|\d+\.)\s+", line):
            flush_para()
            blocks.append(Block("bullet", _clean(re.sub(r"^([-*+]|\d+\.)\s+", "", line))))
            continue
        text = _clean(line.lstrip(">"))
        if text:
            para.append(text)
    flush_para()
    return blocks


@dataclass
class Line:
    text: str
    font: ImageFont.FreeTypeFont
    color: tuple
    gap_before: int
    indent: int = 0
    bullet: bool = False  # 只有條列的第一行畫項目符號，續行靠 indent 對齊


def layout(blocks: list[Block], fonts: Fonts, max_width: int, max_height: int) -> list[list[Line]]:
    """把區塊流排進固定高度的頁面，回傳每頁的行清單。

    分頁只在區塊之間換頁，不會把標題留在頁尾、內文推到下一頁 ——
    但單一區塊本身超過一頁時仍允許中途切開，否則超長段落會無限重排。
    """
    pages: list[list[Line]] = []
    page: list[Line] = []
    used = 0

    def flush() -> None:
        nonlocal page, used
        if page:
            pages.append(page)
        page, used = [], 0

    bullet_indent = int(fonts.body.getlength("・"))
    for index, block in enumerate(blocks):
        if block.kind == "heading":
            font, color = fonts.heading, ACCENT
        else:
            font, color = fonts.body, FG
        width = max_width - bullet_indent if block.kind == "bullet" else max_width
        wrapped = wrap_text(block.text, font, width)
        lh = line_height(font)
        gap = BLOCK_GAP if page else 0

        # 一般區塊至少要放得下前三行才留在本頁；標題另外加算後面內文的前兩行 ——
        # 標題自己塞得進頁尾但內文擠到下一頁，看起來就是一個沒有內容的標題。
        need = lh * min(len(wrapped), 3) + gap
        if block.kind == "heading" and index + 1 < len(blocks):
            need += BLOCK_GAP + line_height(fonts.body) * 2
        if used and used + need > max_height:
            flush()
            gap = 0

        for i, text in enumerate(wrapped):
            if used + lh > max_height:
                flush()
                gap = 0
            page.append(
                Line(
                    text=text,
                    font=font,
                    color=color,
                    gap_before=gap if i == 0 else 0,
                    indent=bullet_indent if block.kind == "bullet" else 0,
                    bullet=(block.kind == "bullet" and i == 0),
                )
            )
            used += lh + (gap if i == 0 else 0)
    flush()
    return pages or [[]]


# --- 繪圖 ---------------------------------------------------------------


def _new_card() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (CARD_W, CARD_H), BG)
    draw = ImageDraw.Draw(img)
    # 左側細長色條：整組圖卡共用的視覺識別，比畫 logo 便宜且不會擋到文字
    draw.rectangle([0, 0, 12, CARD_H], fill=ACCENT)
    return img, draw


def _footer(draw: ImageDraw.ImageDraw, fonts: Fonts, left: str, right: str = "") -> None:
    y = CARD_H - MARGIN_BOTTOM + 60
    draw.line([MARGIN_X, y - 30, CARD_W - MARGIN_X, y - 30], fill=(45, 56, 76), width=2)
    draw.text((MARGIN_X, y), left, font=fonts.small, fill=MUTED)
    if right:
        w = fonts.small.getlength(right)
        draw.text((CARD_W - MARGIN_X - w, y), right, font=fonts.small, fill=MUTED)


def render_cover(title: str, fonts: Fonts, eyebrow: str = "影片重點整理") -> Image.Image:
    img, draw = _new_card()
    max_w = CARD_W - MARGIN_X * 2
    lines = wrap_text(title, fonts.title, max_w)
    lh = line_height(fonts.title)

    draw.text((MARGIN_X, MARGIN_TOP), eyebrow, font=fonts.small, fill=ACCENT)

    # 標題垂直置中，行數多寡都不會偏上或偏下
    total = lh * len(lines)
    y = (CARD_H - total) // 2 - 40
    for line in lines:
        draw.text((MARGIN_X, y), line, font=fonts.title, fill=FG)
        y += lh
    draw.rectangle([MARGIN_X, y + 30, MARGIN_X + 120, y + 38], fill=ACCENT)
    _footer(draw, fonts, "往右滑看重點 →")
    return img


def render_body(lines: list[Line], fonts: Fonts, page_no: int, total: int) -> Image.Image:
    img, draw = _new_card()
    y = MARGIN_TOP
    for line in lines:
        y += line.gap_before
        if line.bullet:
            draw.text((MARGIN_X, y), "・", font=line.font, fill=ACCENT)
        draw.text(
            (MARGIN_X + line.indent, y), line.text, font=line.font, fill=line.color
        )
        y += line_height(line.font)
    _footer(draw, fonts, "投資有風險，內容非投資建議", f"{page_no} / {total}")
    return img


def render_disclaimer(fonts: Fonts, video_id: str) -> Image.Image:
    img, draw = _new_card()
    max_w = CARD_W - MARGIN_X * 2
    y = MARGIN_TOP
    draw.text((MARGIN_X, y), "免責聲明", font=fonts.heading, fill=ACCENT)
    y += line_height(fonts.heading) + BLOCK_GAP
    for line in wrap_text(DISCLAIMER, fonts.body, max_w):
        draw.text((MARGIN_X, y), line, font=fonts.body, fill=FG)
        y += line_height(fonts.body)

    # 來源連結是著作權界線的一部分（CLAUDE.md），每組圖卡都必須帶
    y += BLOCK_GAP * 2
    draw.text((MARGIN_X, y), "影片來源", font=fonts.heading, fill=ACCENT)
    y += line_height(fonts.heading) + BLOCK_GAP
    url = f"https://www.youtube.com/watch?v={video_id}"
    for line in wrap_text(url, fonts.body, max_w):
        draw.text((MARGIN_X, y), line, font=fonts.body, fill=MUTED)
        y += line_height(fonts.body)
    _footer(draw, fonts, "本圖卡為影片重點整理，非逐字稿轉載")
    return img


# --- DB -----------------------------------------------------------------


def load_summary(video_id: str, version: int | None = None) -> dict:
    """取指定版本的摘要，沒給 version 就取最新一版。"""
    sql = "SELECT * FROM summaries WHERE video_id = ?"
    params: list = [video_id]
    if version is not None:
        sql += " AND version = ?"
        params.append(version)
    sql += " ORDER BY version DESC LIMIT 1"
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        raise NoSummary(f"{video_id} v{version}" if version else video_id)
    return dict(row)


def store_assets(summary_id: int, items: list[tuple[Path, str]]) -> None:
    """把圖卡寫進 media_assets；同一份摘要重跑時先清掉舊紀錄。

    重跑會覆寫同名檔案，不清舊列的話 media_assets 會累積指向同一批檔案的
    重複紀錄，發布階段就不知道該送哪幾張。
    """
    with connect() as conn:
        conn.execute("DELETE FROM media_assets WHERE summary_id = ?", (summary_id,))
        for path, type_ in items:
            rel = path.resolve().relative_to(PROJECT_ROOT).as_posix()
            with Image.open(path) as im:
                w, h = im.size
            conn.execute(
                """INSERT INTO media_assets (summary_id, file_path, type, width, height)
                   VALUES (?, ?, ?, ?, ?)""",
                (summary_id, rel, type_, w, h),
            )


# --- 入口 ---------------------------------------------------------------


def render(
    video_id: str,
    version: int | None = None,
    out_dir: Path | None = None,
    store: bool = True,
) -> list[Path]:
    """把某支影片的摘要渲染成 IG 圖卡，回傳產出的檔案清單（依序）。

    store=False 是給預覽用的：只出圖不動 DB。
    """
    summary = load_summary(video_id, version)
    fonts = load_fonts()

    out_dir = Path(out_dir) if out_dir else CARDS_DIR / f"{video_id}_v{summary['version']}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 舊版本的圖比新版本多頁時，殘留的尾頁會被誤當成這次的產出
    for stale in out_dir.glob("*.png"):
        stale.unlink()

    title = (summary.get("title") or "").strip() or f"影片重點整理 {video_id}"
    blocks = parse_blocks(summary["content"])
    body_height = CARD_H - MARGIN_TOP - MARGIN_BOTTOM
    pages = layout(blocks, fonts, CARD_W - MARGIN_X * 2, body_height)

    # IG 輪播上限 10 張，扣掉封面與免責聲明頁；超出的內容留在 Markdown 草稿裡。
    # 寧可截斷也不要產出發不出去的張數 —— 發布階段才失敗更難處理。
    pages = pages[:8]

    total = len(pages)
    images: list[tuple[Image.Image, str]] = [(render_cover(title, fonts), "ig_cover")]
    for i, page in enumerate(pages, start=1):
        images.append((render_body(page, fonts, i, total), "ig_body"))
    images.append((render_disclaimer(fonts, video_id), "ig_disclaimer"))

    items: list[tuple[Path, str]] = []
    for idx, (img, type_) in enumerate(images, start=1):
        path = out_dir / f"{idx:02d}_{type_}.png"
        img.save(path, "PNG", optimize=True)
        items.append((path, type_))

    if store:
        store_assets(summary["id"], items)
    return [p for p, _ in items]


if __name__ == "__main__":
    import sys

    paths = render(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None)
    # Windows console 是 cp950，路徑含中文會炸；這裡路徑都是 ASCII，安全
    for p in paths:
        print(p)
