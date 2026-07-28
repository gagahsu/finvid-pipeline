"""摘要與社群文案生成（SUMMARIZING 階段）。

吃 transcripts.corrected_text，產出：

- content     給 Vocus / Medium 的 Markdown 草稿（人工複製貼上發布）
- ig_caption  IG 貼文文案（圖卡由後續 RENDERING 階段生成）

兩份都寫進 summaries 表，並在 posts 表為每個平台開一列 draft。

著作權界線（CLAUDE.md）：對外發布的只能是重新整理過的摘要與觀點，
不能是逐字稿本身。這件事不只寫在 prompt 裡 —— prompt 是請求，不是保證，
model 想照抄還是會照抄。所以產出後另外用 check_verbatim() 做程式檢查，
逐字重疊超過門檻就讓這個階段失敗，不會靜靜寫進 DB。
"""

import json
import re
from pathlib import Path

from app.db import connect

# 逐字重疊容許長度（字元）。
#
# 中文 20 字大約是一個完整句子。短於此的重疊避不掉也不該避：談同一支股票必然
# 會重複出現「台積電法說會」這種詞組，那是事實陳述不是抄寫。超過就代表整句搬運。
MAX_VERBATIM = 20

DISCLAIMER = (
    "本文為 YouTube 影片內容的重點整理，不代表本人立場，"
    "亦不構成任何投資建議。投資有風險，請自行判斷並承擔決策結果。"
)

SYSTEM_PROMPT = """你是財經內容編輯。你會收到一支台股財經影片的逐字稿，
要把它整理成可以獨立閱讀的重點文章與社群貼文。

最重要的規則：**不可以照抄逐字稿**。

逐字稿是口語，充滿贅字、重複與跳躍。你的工作是理解它在講什麼，
然後用自己的話重新寫一遍。任何超過二十個字的連續片段都不可以跟原文相同。
這不是風格建議，是著作權界線。

內容要求：

- 抓出影片真正談到的重點，通常是三到六個。不要為了湊數硬編。
- 有提到具體個股就寫出公司名與代號；沒提到就不要自己補。
- 保留說話者的判斷與理由，但要標明那是影片觀點，不是既成事實。
- 逐字稿是語音辨識產生的，可能還有錯字。看不懂的段落直接略過，
  不要猜測後當成事實寫出來。
- 不要寫成投資建議的語氣（「快買」「必漲」），改成描述立場
  （「影片認為⋯⋯」「主持人看法是⋯⋯」）。

兩份產出的差別：

- article：Markdown 格式的文章正文，用二級標題分段，每段兩到四句話。
  不要自己加標題列、免責聲明或來源連結，那些由程式補上。
- ig_caption：IG 貼文文案，兩百到四百字，開頭一句話要能讓人停下來滑，
  接著條列重點，語氣口語但不浮誇。結尾不要加 hashtag，由程式補上。

輸出格式：只輸出 JSON 物件，不要有任何其他文字或 markdown 標記。
{"title": "文章標題", "points": ["重點一", "重點二"], "tickers": [{"symbol": "2330", "name": "台積電"}], "article": "Markdown 正文", "ig_caption": "IG 文案"}

title 二十字以內。points 每則二十五字以內，是給圖卡用的短句。
tickers 只放影片明確談到的個股，沒有就給空陣列。"""

HASHTAGS = "#台股 #財經 #投資理財 #股市 #盤勢"


def _normalize(text: str) -> str:
    """比對逐字重疊前先去掉標點與空白。

    逐字稿沒有標點，摘要有；不正規化的話「台積電法說會」與「台積電，法說會」
    會被當成不同內容，檢查等於失效。
    """
    return re.sub(r"[^\w]", "", text)


def check_verbatim(generated: str, transcript: str, limit: int = MAX_VERBATIM) -> str | None:
    """回傳第一個超過 limit 字的逐字重疊片段，沒有就回 None。

    用滑動視窗掃描：只要生成內容裡任何 limit 長度的片段出現在逐字稿裡，
    就代表至少有這麼長的照抄。找到即回報，不需要找出最長的那段。
    """
    gen, src = _normalize(generated), _normalize(transcript)
    for i in range(len(gen) - limit + 1):
        chunk = gen[i : i + limit]
        if chunk in src:
            return chunk
    return None


class VerbatimError(RuntimeError):
    """生成內容與逐字稿重疊過多，不可對外發布。"""


def load_transcript(video_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT corrected_text, raw_text FROM transcripts WHERE video_id = ?",
            (video_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"no transcript for {video_id}")
    # corrected_text 是校正後的版本，還沒跑 apply 的影片只有 raw_text
    return row["corrected_text"] or row["raw_text"]


def demote_headings(article: str) -> str:
    """把正文裡的一級標題降成二級。

    prompt 已經交代「不要自己加標題列」，實測 model 照樣加，結果草稿開頭
    連著兩個 H1。與其再多寫一句指示求它遵守，不如產出後直接改 —— 這種
    格式問題用程式修是確定的，靠 prompt 是機率的。
    """
    return re.sub(r"^# (?=\S)", "## ", article, flags=re.MULTILINE)


def build_markdown(data: dict, video_id: str, title: str) -> str:
    """組出完整的 Markdown 草稿：標題 + 正文 + 來源 + 免責聲明。

    來源連結與免責聲明由程式補，不交給 model：這兩項少一個就是合規問題，
    而 model 漏掉任何一段指示都是家常便飯。
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    parts = [f"# {title}", "", demote_headings(data.get("article", "").strip()), ""]

    tickers = data.get("tickers") or []
    if tickers:
        names = "、".join(
            f"{t.get('name', '')}（{t.get('symbol', '')}）" for t in tickers
        )
        parts += [f"**影片提到的個股**：{names}", ""]

    parts += [
        "---",
        "",
        f"**影片來源**：[{url}]({url})",
        "",
        f"> {DISCLAIMER}",
        "",
    ]
    return "\n".join(parts)


def build_ig_caption(data: dict) -> str:
    caption = data.get("ig_caption", "").strip()
    return f"{caption}\n\n{DISCLAIMER}\n\n{HASHTAGS}"


def next_version(video_id: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM summaries WHERE video_id = ?",
            (video_id,),
        ).fetchone()
    return row["v"]


def store(video_id: str, title: str, content: str, ig_caption: str, model: str) -> int:
    version = next_version(video_id)
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO summaries (video_id, version, title, content, ig_caption, model)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (video_id, version, title, content, ig_caption, model),
        )
        summary_id = cur.lastrowid
        # 三個平台各開一列 draft。Instagram 之後由 RENDERING/PUBLISHING 接手，
        # Vocus 與 Medium 沒有可用的發文 API，會一直停在 draft 等人工貼上。
        for platform in ("instagram", "vocus", "medium"):
            conn.execute(
                """INSERT INTO posts (summary_id, platform, status)
                   VALUES (?, ?, 'draft')
                   ON CONFLICT(summary_id, platform) DO NOTHING""",
                (summary_id, platform),
            )
    return summary_id


def summarize(video_id: str, drafts_dir: Path | None = None) -> dict:
    """跑完整個摘要階段，回傳統計。"""
    from app import llm
    from app.config import settings

    transcript = load_transcript(video_id)
    raw = llm.complete(SYSTEM_PROMPT, transcript)
    data = llm._extract_json(raw)

    title = (data.get("title") or "").strip() or f"影片重點整理 {video_id}"
    article = data.get("article") or ""
    if not article.strip():
        raise RuntimeError("model returned an empty article")

    # 檢查兩份產出：IG 文案同樣會對外發布，漏檢等於留一個後門
    for label, text in (("article", article), ("ig_caption", data.get("ig_caption") or "")):
        overlap = check_verbatim(text, transcript)
        if overlap:
            raise VerbatimError(
                f"{label} 與逐字稿有 {len(overlap)} 字以上的逐字重疊：{overlap}"
            )

    content = build_markdown(data, video_id, title)
    ig_caption = build_ig_caption(data)
    summary_id = store(video_id, title, content, ig_caption, settings.openrouter_model)

    drafts_dir = drafts_dir or Path(__file__).resolve().parent.parent.parent / "data" / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    version = next_version(video_id) - 1
    draft_path = drafts_dir / f"{video_id}_v{version}.md"
    draft_path.write_text(content, encoding="utf-8")

    return {
        "summary_id": summary_id,
        "version": version,
        "title": title,
        "points": len(data.get("points") or []),
        "tickers": len(data.get("tickers") or []),
        "content_chars": len(content),
        "ig_caption_chars": len(ig_caption),
        "draft": str(draft_path),
    }


if __name__ == "__main__":
    import sys

    stats = summarize(sys.argv[1])
    # Windows console 是 cp950，標題含中文會炸，只印 ASCII 安全的欄位
    print({k: v for k, v in stats.items() if k != "title"})
