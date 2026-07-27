"""從累積的 corrections 紀錄產生 Whisper hotwords。

Whisper 的 prompt 有 223 token 上限（max_length 448 的一半減一），中文股名平均
4 tokens，實際只塞得下約 55 檔，遠少於字典的 1983 檔。所以 hotwords 不能取代
校正模組，只能當作「從源頭少錯一點」的輔助。

選股策略是累積式的：從過去校正紀錄看這個頻道實際常提到哪些股票。跑過的影片
越多，這份清單越貼近該頻道的選股偏好。

faster-whisper 會自行把過長的 hotwords 截斷，所以這裡只負責「照重要性排序」，
不需要自己算 token — 截斷自然會砍掉最不重要的尾巴。
"""

from app.db import connect

# 給得比 223 token 塞得下的量多一些，讓 faster-whisper 自己截。
MAX_NAMES = 100


def build(channel_videos: list[str] | None = None) -> str:
    """產生 hotwords 字串。

    只採信心足夠自動套用的（status='auto'）與人工覆核過的紀錄；
    待確認的 needs_review 誤判率偏高，不拿來餵回轉錄，免得把錯誤放大。

    排序依據：人工覆核過的優先，其次出現次數，最後是平均信心度。
    channel_videos 可指定只看某些影片的紀錄（同頻道），None 表示全部。
    """
    sql = """
        SELECT corrected AS name,
               MAX(human_reviewed) AS reviewed,
               COUNT(*) AS hits,
               AVG(confidence) AS conf
        FROM corrections
        WHERE corrected IS NOT NULL
          AND (status = 'auto' OR human_reviewed = 1)
    """
    params: list = []
    if channel_videos:
        placeholders = ",".join("?" * len(channel_videos))
        sql += f" AND video_id IN ({placeholders})"
        params.extend(channel_videos)
    sql += """
        GROUP BY corrected
        ORDER BY reviewed DESC, hits DESC, conf DESC
        LIMIT ?
    """
    params.append(MAX_NAMES)

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return "、".join(r["name"] for r in rows)


if __name__ == "__main__":
    hw = build()
    print(f"names: {len(hw.split('、')) if hw else 0}")
    print(f"chars: {len(hw)}")
