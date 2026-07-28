"""OpenRouter client。

把「句子 context + fuzzy match 候選清單」丟給 model，判斷該不該替換、替換成哪個候選。

採批次判斷：一次送多個可疑詞，大幅降低 request 數（187 個詞 → 約 16 個 request），
免費層的 rate limit 也比較不會踩到。
"""

import json
import re

import httpx

from app.config import settings

API_URL = "https://openrouter.ai/api/v1/chat/completions"

BATCH_SIZE = 12

SYSTEM_PROMPT = """你是台股財經逐字稿的校對助理。

逐字稿由語音辨識產生，股票名稱常因發音相近而聽錯（例如「鴻海」被聽成「紅海」）。
你會收到多筆待判斷項目，每筆包含一個句子、句中的可疑詞，以及模糊比對出的候選股票。

可疑詞是用滑動視窗機械切出來的，很多根本不是完整詞，只是橫跨兩個詞的殘片。
每筆會附上該句的斷詞結果與「是否對齊詞界」，這是判斷的重要依據。

逐筆判斷該可疑詞是否應替換成某個候選：

- 只有在你有把握該詞確實是在講那支股票時才替換。沒把握就不要改。
- 對齊詞界=否，通常代表這個可疑詞是切壞的殘片，例如「今天是開始大買」切出
  「是開」、「說實話是繼續」切出「是繼」、「今天連亞衝上去」切出「天連」。
  這種一律不要替換。
- 但對齊詞界=否也可能只是斷詞器不認得誤聽的字（例如「台大電」被切成
  「台大/電影」）。若該殘片本身就是完整的公司名稱誤寫、且語境明顯在談個股，
  仍然可以替換。判斷依據是「這個詞在句中是不是一個獨立的指稱對象」。
- 語境要合理：前後文明顯在談該公司或該產業，才有理由替換。
- 可疑詞若本身就是正常的一般詞彙（例如「大家」「新高」「經濟」「壓力」），
  或是人名、地名，不要替換。
- 候選清單只是模糊比對結果，可能全部都是錯的。

輸出格式：只輸出 JSON 物件，不要有任何其他文字或 markdown 標記。
{"results": [{"index": 項目編號, "term": "原可疑詞", "replace": true/false, "symbol": "代號或null", "name": "股票名稱或null", "confidence": 0.0到1.0, "reason": "簡短理由"}]}

reason 必須簡短（20 字內）且不可包含雙引號或換行。
每個項目都要有對應的結果，index 要跟輸入的編號一致，term 要原封不動照抄輸入的可疑詞。
即使某項你無法判斷，也要輸出該項並填 replace=false，不可跳過不編號。"""


class LLMError(RuntimeError):
    pass


class RateLimitError(LLMError):
    """配額用盡。

    OpenRouter 免費層是每日 50 個 request，重置在 UTC 00:00。
    這跟連線失敗那種暫時性錯誤不同，重試只會更快燒完額度，
    所以要單獨分類：不重試，並且讓整個 run 直接中止。
    """


def _extract_json(content: str) -> dict:
    """從 model 回應抽出 JSON。

    免費 model 常在 JSON 外面包 markdown，或在 reason 欄位塞未跳脫的引號。
    先照正常方式解析，失敗再退回抓最外層大括號。
    """
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise LLMError(f"model did not return parseable JSON: {content[:200]}")


def _post_once(messages: list[dict]) -> str:
    if not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY not set in backend/.env")

    try:
        response = httpx.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.openrouter_model,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"request failed: {exc}") from exc

    if response.status_code == 429:
        raise RateLimitError(f"OpenRouter 429: {response.text[:300]}")
    if response.status_code != 200:
        raise LLMError(f"OpenRouter {response.status_code}: {response.text[:300]}")

    # 免費層偶爾在 200 之下回非 JSON 的 body（錯誤頁、SSE 片段）。
    # 這裡一律轉成 LLMError，讓呼叫端能跳過該批而不是整個 run 掛掉。
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMError(f"non-JSON response body: {response.text[:300]}") from exc

    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected response shape: {str(payload)[:300]}") from exc


def _post(messages: list[dict], attempts: int = 3) -> str:
    """帶退避重試。免費層的暫時性失敗很常見，重試一次通常就過。"""
    import time

    last: LLMError | None = None
    for i in range(attempts):
        try:
            return _post_once(messages)
        except RateLimitError:
            raise  # 配額問題，重試只會燒更快
        except LLMError as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise last  # type: ignore[misc]


def judge_batch(items: list[dict], system_prompt: str | None = None) -> dict[int, dict]:
    """批次判斷。

    items 每筆需有 sentence / suspect / candidates。
    回傳 {輸入索引: 判斷結果}；model 漏掉的項目不會出現在結果中。

    system_prompt 可覆寫，供 eval_prompt.py 做 A/B 比較用。
    """
    blocks = []
    for i, item in enumerate(items):
        cands = "、".join(
            f"{c['symbol']} {c['name_zh']}({c['score']:.0f})" for c in item["candidates"]
        )
        aligned = item.get("aligned")
        aligned_text = {True: "是", False: "否", None: "無法判定"}[aligned]
        blocks.append(
            f"[{i}] 句子：{item['sentence']}\n"
            f"    斷詞：{item.get('segmented', '')}\n"
            f"    可疑詞：{item['suspect']}（對齊詞界：{aligned_text}）\n"
            f"    候選：{cands}"
        )

    content = _post(
        [
            {"role": "system", "content": system_prompt or SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(blocks)},
        ]
    )
    payload = _extract_json(content)
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise LLMError(f"unexpected results shape: {str(results)[:200]}")

    out: dict[int, dict] = {}
    for r in results:
        try:
            idx = int(r["index"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= idx < len(items):
            continue
        # 核對回傳的 term 與該 index 的可疑詞是否相符。
        #
        # 實測 model 會漏掉一筆之後把後續項目重新編號，造成整批位移一格：
        # 「電量」拿到「飛鴻」的判斷、「驚訝」拿到「高就」的判斷。這種錯誤
        # 不會拋例外、結果看起來完全正常，但套回逐字稿就是把字改到別的位置。
        # 只信 index 無法察覺，所以要求 model 回傳原詞當校驗碼。
        term = r.get("term")
        if term is not None and term != items[idx]["suspect"]:
            continue
        out[idx] = r
    return out
