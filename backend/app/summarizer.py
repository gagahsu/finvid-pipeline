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

# 輸出 token 上限。正文一千二到兩千字加上 IG 文案與 JSON 結構，中文一字約
# 一到兩個 token，抓 8000 才不會在 stocks 陣列中途被切斷 —— JSON 被截斷等於
# 整個階段白跑，寧可給寬一點。
MAX_OUTPUT_TOKENS = 8000

DISCLAIMER = (
    "本文為 YouTube 影片內容的重點整理，不代表本人立場，"
    "亦不構成任何投資建議。投資有風險，請自行判斷並承擔決策結果。"
)

SYSTEM_PROMPT = """你是財經內容編輯。你會收到一支台股財經節目的逐字稿，
要把它整理成一篇資訊密度高、可以獨立閱讀的重點文章與社群貼文。

最重要的規則：**不可以照抄逐字稿**。

逐字稿是口語，充滿贅字、重複與跳躍。你的工作是理解它在講什麼，
然後用自己的話重新寫一遍。任何超過二十個字的連續片段都不可以跟原文相同。
這不是風格建議，是著作權界線。

## 這種節目在講什麼

台股財經節目通常照這個順序走：先講當天盤勢，再帶到影響盤勢的總經與國際
消息，然後分析產業族群，最後由來賓逐檔講個股看法與操作方式。
你的整理要把這四塊都抓出來，而不是只寫幾句籠統的心得。

**節目講到的具體內容才是價值所在**：指數點位、成交量、外資買賣超金額、
法人籌碼、殖利率、油價匯率數字、個股價位與均線位置、來賓說的進出場條件。
這些數字與條件務必保留下來，這是讀者真正想看的東西。
反過來說，「市場情緒不佳」「要留意風險」這種空話沒有資訊量，不要寫。

## 文章結構（article）

用二級標題分節，只寫節目真的有談到的部分，沒談到的整節略過，不要硬湊：

- `## 盤勢` — 指數走勢與點位、成交量能、技術面位置（均線、支撐壓力、缺口）、
  三大法人買賣超與籌碼變化、內外資或散戶動向。
- `## 總經與國際情勢` — 利率與央行動向、油價、匯率、通膨數據、地緣政治、
  美股與其他主要市場的表現，以及節目認為它們如何影響台股。
- `## 產業與族群` — 節目談到的族群輪動、產業供需、法說會與訂單能見度。
- `## 個股觀察` — **逐檔獨立成段**，每檔至少三到五句，格式如下：
  `### 台積電（2330）` 然後寫節目對這檔的看法、理由、以及提到的
  觀察價位或操作方式（例如站上哪條均線、跌破什麼價位要減碼、
  哪個區間是箱型整理）。有幾檔就寫幾檔，不要合併成一段流水帳。
- `## 操作策略與風險` — 節目給的部位配置、進出場紀律、以及它自己點出的風險。

篇幅：正文一千二到兩千字。節目內容豐富就寫足，內容真的少就照實少寫，
但不可以為了湊字數把同一件事換句話說講兩遍。

## 其他要求

- 保留說話者的判斷與理由，但要標明那是節目觀點而非既成事實
  （「來賓認為⋯⋯」「節目提到⋯⋯」）。
- 不要寫成投資建議的語氣（「快買」「必漲」）。轉述操作方法時也一樣，
  寫「來賓的操作條件是跌破月線先減碼」，不要寫「你應該跌破月線減碼」。
- 逐字稿是語音辨識產生的，可能還有錯字。看不懂的段落直接略過，
  不要猜測後當成事實寫出來。個股代號若與名稱明顯不符，以名稱為準。
- 不要自己加文章標題列、免責聲明或來源連結，那些由程式補上。

## IG 文案（ig_caption）

四百到八百字。開頭一句話要能讓人停下來滑，接著用條列帶出盤勢、總經、
個股三塊的具體重點（一樣要有數字與價位，不要只寫結論）。
語氣口語但不浮誇。結尾不要加 hashtag，由程式補上。

## 輸出格式

只輸出 JSON 物件，不要有任何其他文字或 markdown 標記。
{"title": "文章標題", "points": ["重點一", "重點二"], "tickers": [{"symbol": "2330", "name": "台積電"}], "stocks": [{"symbol": "2330", "name": "台積電", "view": "節目對這檔的看法與理由", "action": "提到的觀察價位或操作方式，沒提到就給空字串"}], "article": "Markdown 正文", "ig_caption": "IG 文案"}

title 二十字以內。points 六到十則、每則二十五字以內，是給 IG 圖卡用的短句，
要挑最具體的（含數字或價位那種），不要放空泛結論。
tickers 與 stocks 只放節目明確談到的個股，沒有就給空陣列。"""

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


# 文中的「公司名（代號）」寫法。代號含台股四碼、上櫃五碼與 ETF 的英數尾碼。
_SYMBOL_RE = re.compile(r"([一-鿿][一-鿿A-Za-z0-9\-]{1,9})[（(]\s*(\d{4,6}[A-Za-z]?)\s*[）)]")


def load_ticker_maps() -> tuple[dict[str, str], dict[str, str]]:
    """回傳 (名稱→代號, 代號→名稱)。別名也算名稱。"""
    by_name: dict[str, str] = {}
    by_symbol: dict[str, str] = {}
    with connect() as conn:
        rows = conn.execute("SELECT symbol, name_zh, aliases FROM tickers").fetchall()
    for r in rows:
        by_symbol[r["symbol"]] = r["name_zh"]
        by_name.setdefault(r["name_zh"], r["symbol"])
        try:
            for alias in json.loads(r["aliases"] or "[]"):
                by_name.setdefault(alias, r["symbol"])
        except (json.JSONDecodeError, TypeError):
            continue
    return by_name, by_symbol


# 名稱與代號正式名稱的相似度門檻。「台積」對「台積電」是 100（部分比對），
# 「長農航」對「光寶科」是 0 —— 中間地帶很少，門檻取多少都不敏感。
NAME_MATCH_THRESHOLD = 70


def _name_matches_symbol(name: str, symbol: str, by_symbol: dict[str, str]) -> bool:
    """文中名稱是否像該代號的正式名稱。用來放行未收錄的簡稱。"""
    official = by_symbol.get(symbol)
    if not official:
        return False
    from rapidfuzz import fuzz

    return fuzz.partial_ratio(name, official) >= NAME_MATCH_THRESHOLD


def verify_symbols(text: str) -> tuple[str, list[dict]]:
    """校對文中的股票代號，回傳 (修正後文字, 問題清單)。

    實測 model 會自己編代號：把長榮航寫成「長農航（2301）」（2301 是光寶科）、
    雷虎寫成「雷虎（2331）」（2331 是精英）。這在財經內容是最貴的一種錯 ——
    讀者可能照著代號去下單。

    prompt 裡已經寫了「代號與名稱不符時以名稱為準」，但 model 照樣編。
    跟 demote_headings 同樣的道理：格式與事實問題用程式修是確定的，
    靠 prompt 是機率的，而這一項錯了的代價遠高於排版。

    三種處置：
    - 名稱查得到 → 用表裡的代號覆蓋（名稱是人聽得懂的，代號才是會被照抄的）
    - 名稱查不到，但代號查得到、且該代號的正式名稱與文中名稱夠像 → 代號留著。
      這是「台積」對「台積電」這種未收錄的簡稱，不是錯。
    - 其餘一律把括號拿掉，只留名稱。少一個代號不會害到人，錯一個會。
    """
    by_name, by_symbol = load_ticker_maps()
    issues: list[dict] = []

    def _sub(m: re.Match) -> str:
        name, symbol = m.group(1), m.group(2)
        correct = by_name.get(name)
        if correct:
            if correct != symbol:
                issues.append(
                    {"name": name, "given": symbol, "action": "corrected", "symbol": correct}
                )
            return f"{name}（{correct}）"
        if _name_matches_symbol(name, symbol, by_symbol):
            issues.append(
                {"name": name, "given": symbol, "action": "name_unknown", "symbol": symbol}
            )
            return m.group(0)
        issues.append({"name": name, "given": symbol, "action": "dropped", "symbol": None})
        return name

    return _SYMBOL_RE.sub(_sub, text), issues


# hashtag 只吃到 15 個非空白字元為止。中文句子沒有空白，用 \S+ 會讓一個 #
# 把後面整段文案吃光 —— 實測就發生過整篇 IG 文案被清成空字串。
# 真正的標籤不會超過十幾個字，超過的那是句子不是標籤。
_HASHTAG_RE = re.compile(r"#[^\s#]{1,15}")


def strip_hashtags(caption: str) -> str:
    """去掉 model 自己加的 hashtag。

    prompt 交代結尾不要加，實測照樣加，程式再補一份就變成兩串重複的標籤。
    """
    return _HASHTAG_RE.sub("", caption).strip()


def _line(text: object) -> str:
    """把值壓成單行：條列項目跨行會被解析成新的段落。"""
    return " ".join(str(text or "").split())


def _stock_table(data: dict) -> list[str]:
    """個股速覽：逐檔列出節目看法與提到的價位／操作條件。

    正文的「個股觀察」已經逐檔寫過一遍，這段不是重複而是索引 —— 讀者想快速掃
    「哪幾檔、條件是什麼」時不必回頭讀整篇。

    刻意用條列而不是 Markdown 表格：IG 圖卡渲染器與審核台的 Markdown 解析器
    都只認標題／段落／清單，表格會變成一行行管線符號。Vocus/Medium 吃得下表格，
    但為了那兩個平台犧牲另外兩處的可讀性不划算。

    stocks 是後來才加的欄位，舊版摘要沒有；退回只列出 tickers 的名稱，
    不要讓舊資料重跑時整段消失。
    """
    stocks = data.get("stocks") or []
    if stocks:
        rows = []
        for s in stocks:
            name = _line(s.get("name"))
            symbol = _line(s.get("symbol"))
            label = f"{name}（{symbol}）" if symbol else name
            detail = _line(s.get("view"))
            action = _line(s.get("action"))
            if action:
                detail = f"{detail} 操作條件：{action}" if detail else f"操作條件：{action}"
            rows.append(f"- **{label}**：{detail}")
        return ["## 個股速覽", "", *rows, ""]

    tickers = data.get("tickers") or []
    if tickers:
        names = "、".join(f"{t.get('name', '')}（{t.get('symbol', '')}）" for t in tickers)
        return [f"**影片提到的個股**：{names}", ""]
    return []


def build_markdown(data: dict, video_id: str, title: str) -> str:
    """組出完整的 Markdown 草稿：標題 + 正文 + 來源 + 免責聲明。

    來源連結與免責聲明由程式補，不交給 model：這兩項少一個就是合規問題，
    而 model 漏掉任何一段指示都是家常便飯。
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    parts = [f"# {title}", "", demote_headings(data.get("article", "").strip()), ""]

    parts += _stock_table(data)

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
    caption = strip_hashtags(data.get("ig_caption", "").strip())
    return f"{caption}\n\n{DISCLAIMER}\n\n{HASHTAGS}"


def verify_stock_list(data: dict) -> list[dict]:
    """校對 stocks / tickers 兩個陣列裡的代號，沿用 verify_symbols 的規則。

    正文走 regex、結構化欄位走這裡，兩條路都要擋 —— 個股速覽是最容易被直接
    複製去下單的一段。
    """
    by_name, by_symbol = load_ticker_maps()
    issues: list[dict] = []
    for key in ("stocks", "tickers"):
        for item in data.get(key) or []:
            name, symbol = (item.get("name") or "").strip(), (item.get("symbol") or "").strip()
            correct = by_name.get(name)
            if correct:
                if correct != symbol:
                    issues.append(
                        {"name": name, "given": symbol, "action": "corrected", "symbol": correct}
                    )
                item["symbol"] = correct
            elif _name_matches_symbol(name, symbol, by_symbol):
                issues.append(
                    {"name": name, "given": symbol, "action": "name_unknown", "symbol": symbol}
                )
            else:
                issues.append({"name": name, "given": symbol, "action": "dropped", "symbol": None})
                item["symbol"] = ""
    return issues


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
    data = llm.complete_json(SYSTEM_PROMPT, transcript, max_tokens=MAX_OUTPUT_TOKENS)

    title = (data.get("title") or "").strip() or f"影片重點整理 {video_id}"
    article = data.get("article") or ""
    if not article.strip():
        raise RuntimeError("model returned an empty article")
    # IG 文案空白同樣是壞產出。實測 model 會回空字串，而下游只是把免責聲明與
    # hashtag 接上去，看起來像一則「正常但沒內容」的貼文，一路混到發布才被發現。
    if not strip_hashtags((data.get("ig_caption") or "").strip()):
        raise RuntimeError("model returned an empty ig_caption")

    # 代號校對放在逐字重疊檢查之前：改動的是括號裡的數字，不影響重疊判定，
    # 但要確保寫進 DB 的已經是校對過的版本。
    article, issues = verify_symbols(article)
    data["article"] = article
    caption_text, caption_issues = verify_symbols(data.get("ig_caption") or "")
    data["ig_caption"] = caption_text
    issues += caption_issues + verify_stock_list(data)

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
        "stocks": len(data.get("stocks") or []),
        # 代號校對結果留在回傳裡：人工審核時要知道 model 編了哪些代號，
        # 也能反過來看出這個 model 值不值得繼續用。
        "symbol_issues": issues,
        "content_chars": len(content),
        "ig_caption_chars": len(ig_caption),
        "draft": str(draft_path),
    }


if __name__ == "__main__":
    import sys

    stats = summarize(sys.argv[1])
    # Windows console 是 cp950，標題含中文會炸，只印 ASCII 安全的欄位
    print({k: v for k, v in stats.items() if k != "title"})
