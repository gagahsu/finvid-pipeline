"""股票代號/名稱校正。

流程（retrieve-then-generate，非語意檢索）：
1. 逐字稿分句，抓可疑片段（中文 n-gram、數字代號）
2. rapidfuzz 對 tickers 做 fuzzy match，字面 + 語音（拼音）雙軌取分
3. 有候選才丟 LLM 判斷，沒把握不改
4. 寫入 corrections 表留紀錄

Whisper 對股票名稱的錯誤多半是發音相近造成（緯創→為創），
所以拼音相似度往往比字面相似度更能抓到。
"""

import json
import re
from dataclasses import dataclass, field

from pypinyin import Style, lazy_pinyin
from rapidfuzz import fuzz

from app.db import connect

# 股票代號：4 位數字，或 5-6 碼含英文尾碼的 ETF（00981A）
CODE_PATTERN = re.compile(r"\b\d{4,6}[A-Za-z]?\b")
CJK = re.compile(r"[一-鿿]+")

# n-gram 長度：台股簡稱多為 2-4 字
NGRAM_SIZES = (2, 3, 4)

# 分數門檻：低於此值視為無關，高於 EXACT 視為本來就正確、不需校正
#
# 90 是實測調出來的。70 會產生 12k+ 候選（絕大多數是常用詞誤撞股名），
# 90 以上收斂到約 350 個 span、190 個不重複詞，且真陽性（環球晶、長榮航、
# 台達電、鴻海）都還在裡面。剩下的誤判交給 LLM 用上下文濾掉。
SCORE_FLOOR = 90.0
EXACT_SCORE = 100.0

# 信心分級門檻。實測第一支影片的 25 個替換建議：conf >= 0.9 的 11 個全對，
# 0.80-0.85 那段 8 個裡有 5 個是誤判（多半是 n-gram 切壞詞界，例如
# 「就是電池」被切出「是電」）。但那段也有真陽性（國巨、光聖），
# 所以不能直接丟掉，改成標記待人工確認。
AUTO_APPLY_CONFIDENCE = 0.90


def to_pinyin(text: str) -> str:
    """帶聲調的拼音。

    聲調是關鍵：無聲調時常用詞會大量誤撞股名（大家/大甲、話說/華碩、壓力/亞力
    在無聲調下完全相同），加上聲調即可區分，而真正的同音錯字（齊鴻/奇鋐、
    連亞/聯亞）聲調本來就一致，仍然match得到。
    """
    return "".join(lazy_pinyin(text, style=Style.TONE3))


@dataclass
class Ticker:
    symbol: str
    name_zh: str
    pinyin: str


@dataclass
class Suspect:
    text: str
    segment_index: int
    context: str
    candidates: list[dict] = field(default_factory=list)


def load_tickers() -> list[Ticker]:
    with connect() as conn:
        rows = conn.execute("SELECT symbol, name_zh FROM tickers").fetchall()
    return [Ticker(r["symbol"], r["name_zh"], to_pinyin(r["name_zh"])) for r in rows]


def _ngrams(text: str) -> set[str]:
    """抽出中文 n-gram 與數字代號。"""
    out: set[str] = set()
    for run in CJK.findall(text):
        for n in NGRAM_SIZES:
            for i in range(len(run) - n + 1):
                out.add(run[i : i + n])
    out.update(CODE_PATTERN.findall(text))
    return out


def score_candidates(term: str, tickers: list[Ticker], top_k: int = 5) -> list[dict]:
    """字面 + 拼音雙軌比對，回傳 top-k 候選。"""
    term_py = to_pinyin(term)
    scored: list[tuple[float, Ticker]] = []
    for t in tickers:
        literal = fuzz.ratio(term, t.name_zh)
        phonetic = fuzz.ratio(term_py, t.pinyin)
        score = max(literal, phonetic)
        if score >= SCORE_FLOOR:
            scored.append((score, t))
    scored.sort(key=lambda x: -x[0])
    return [
        {"symbol": t.symbol, "name_zh": t.name_zh, "score": s}
        for s, t in scored[:top_k]
    ]


def detect(segments: list[dict], tickers: list[Ticker]) -> list[Suspect]:
    """找出所有有候選、且非完全正確的可疑片段。"""
    suspects: list[Suspect] = []
    for idx, seg in enumerate(segments):
        text = seg["text"].strip()
        for term in _ngrams(text):
            candidates = score_candidates(term, tickers)
            if not candidates:
                continue
            # 已經完全命中某支股票 → 本來就對，不需校正
            if candidates[0]["score"] >= EXACT_SCORE and term == candidates[0]["name_zh"]:
                continue
            suspects.append(
                Suspect(text=term, segment_index=idx, context=text, candidates=candidates)
            )
    return suspects


def judge_and_store(video_id: str, suspects: list[Suspect], sleep: float = 1.0) -> dict:
    """批次判斷所有不重複的可疑詞，結果寫入 corrections 表。

    同一個詞在整份逐字稿可能出現多次，只判斷一次以節省 request 並保持一致性。
    """
    import time

    from app import llm

    by_term: dict[str, list[Suspect]] = {}
    for s in suspects:
        by_term.setdefault(s.text, []).append(s)

    # 每個詞挑最長的 context，資訊量最足
    terms = list(by_term)
    items = [
        {
            "sentence": max(by_term[t], key=lambda s: len(s.context)).context,
            "suspect": t,
            "candidates": max(by_term[t], key=lambda s: len(s.context)).candidates,
        }
        for t in terms
    ]

    stats = {
        "judged": 0,
        "replaced": 0,
        "kept": 0,
        "auto": 0,
        "needs_review": 0,
        "missing": 0,
        "errors": 0,
    }
    with connect() as conn:
        for start in range(0, len(items), llm.BATCH_SIZE):
            batch = items[start : start + llm.BATCH_SIZE]
            batch_terms = terms[start : start + llm.BATCH_SIZE]
            n = start // llm.BATCH_SIZE + 1
            total = (len(items) + llm.BATCH_SIZE - 1) // llm.BATCH_SIZE

            try:
                verdicts = llm.judge_batch(batch)
            except llm.LLMError as exc:
                print(f"  batch {n}/{total}: ERROR {exc}", flush=True)
                stats["errors"] += len(batch)
                continue

            for i, term in enumerate(batch_terms):
                verdict = verdicts.get(i)
                if verdict is None:
                    stats["missing"] += 1
                    continue

                stats["judged"] += 1
                replaced = bool(verdict.get("replace"))
                stats["replaced" if replaced else "kept"] += 1

                try:
                    confidence = float(verdict.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0

                if not replaced:
                    status = "rejected"
                elif confidence >= AUTO_APPLY_CONFIDENCE:
                    status = "auto"
                    stats["auto"] += 1
                else:
                    status = "needs_review"
                    stats["needs_review"] += 1

                for s in by_term[term]:
                    conn.execute(
                        """INSERT INTO corrections
                           (video_id, segment_index, context, original, corrected,
                            confidence, candidates, reason, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            video_id,
                            s.segment_index,
                            s.context,
                            term,
                            verdict.get("name") if replaced else None,
                            confidence,
                            json.dumps(s.candidates, ensure_ascii=False),
                            verdict.get("reason"),
                            status,
                        ),
                    )
            conn.commit()
            print(
                f"  batch {n}/{total}: judged={stats['judged']} "
                f"replaced={stats['replaced']}",
                flush=True,
            )
            time.sleep(sleep)
    return stats


if __name__ == "__main__":
    import sys
    from pathlib import Path

    path = Path(sys.argv[1])
    video_id = path.stem
    transcript = json.loads(path.read_text(encoding="utf-8"))
    tickers = load_tickers()
    suspects = detect(transcript["segments"], tickers)

    unique_terms = {s.text for s in suspects}
    print(f"segments:      {len(transcript['segments'])}")
    print(f"suspect spans: {len(suspects)}")
    print(f"unique terms:  {len(unique_terms)}")

    out = path.with_name("suspects.json")
    out.write_text(
        json.dumps(
            [
                {
                    "term": s.text,
                    "segment_index": s.segment_index,
                    "context": s.context,
                    "candidates": s.candidates,
                }
                for s in suspects
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Saved: {out}")

    if "--judge" in sys.argv:
        print("\nJudging with LLM...")
        stats = judge_and_store(video_id, suspects)
        print(f"\n{stats}")
