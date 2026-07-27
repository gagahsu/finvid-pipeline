"""把 corrections 套回逐字稿，產生 corrected_text。

只套用可信的修正：status='auto'（信心足夠）與 human_reviewed=1（人工確認過）。
status='needs_review' 的刻意不套用 —— 那一層本來就是「還沒有人拍板」的意思，
自動套用等於繞過審核閘門。

套用範圍限定在該修正被偵測到的那個 segment，不做整份取代。同一個詞在不同
句子裡可能一個是股名、一個是一般詞彙（「新高」在「創新高」跟「欣高瓦斯」
就是兩回事），全域取代會把對的也改壞。
"""

import json
from pathlib import Path

from app.db import connect

# 只套用 status='auto'：LLM 信心足夠自動通過，或人工按下接受（審核 API 會把
# 狀態改成 auto）。
#
# 不能寫成「status='auto' OR human_reviewed=1」：人工「還原」一筆修正會留下
# status='rejected' 加 human_reviewed=1，用 human_reviewed 判斷會把人明確否決
# 掉的修正照樣套回逐字稿，等於繞過審核閘門。
APPLICABLE = "status = 'auto'"


def load_applicable(video_id: str) -> dict[int, list[tuple[str, str]]]:
    """撈出可套用的修正，依 segment 分組。

    同一句內較長的詞先套用：「新盛例呢」與「新盛例」若順序顛倒，
    短的會先把長的切壞。
    """
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT segment_index, original, corrected
                FROM corrections
                WHERE video_id = ? AND corrected IS NOT NULL AND ({APPLICABLE})""",
            (video_id,),
        ).fetchall()

    by_segment: dict[int, list[tuple[str, str]]] = {}
    for r in rows:
        by_segment.setdefault(r["segment_index"], []).append(
            (r["original"], r["corrected"])
        )
    for pairs in by_segment.values():
        pairs.sort(key=lambda p: -len(p[0]))
    return by_segment


def apply_to_transcript(video_id: str, transcript: dict) -> dict:
    """產生校正後的 segments 與全文，寫入 transcripts 表。"""
    by_segment = load_applicable(video_id)

    segments = transcript["segments"]
    corrected_segments = []
    applied = 0

    for idx, seg in enumerate(segments):
        text = seg["text"]
        for original, corrected in by_segment.get(idx, []):
            if original in text:
                text = text.replace(original, corrected)
                applied += 1
        corrected_segments.append({**seg, "text": text})

    raw_text = "".join(s["text"] for s in segments)
    corrected_text = "".join(s["text"] for s in corrected_segments)

    with connect() as conn:
        conn.execute(
            """INSERT INTO transcripts
                   (video_id, raw_text, corrected_text, segments, applied_count, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(video_id) DO UPDATE SET
                   raw_text = excluded.raw_text,
                   corrected_text = excluded.corrected_text,
                   segments = excluded.segments,
                   applied_count = excluded.applied_count,
                   updated_at = excluded.updated_at""",
            (
                video_id,
                raw_text,
                corrected_text,
                json.dumps(corrected_segments, ensure_ascii=False),
                applied,
            ),
        )

    return {
        "segments": len(segments),
        "segments_touched": len(by_segment),
        "applied": applied,
        "raw_chars": len(raw_text),
        "corrected_chars": len(corrected_text),
    }


if __name__ == "__main__":
    import sys

    path = Path(sys.argv[1])
    transcript = json.loads(path.read_text(encoding="utf-8"))
    stats = apply_to_transcript(path.stem, transcript)
    print(stats)
