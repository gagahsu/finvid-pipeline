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

逐筆判斷該可疑詞是否應替換成某個候選：

- 只有在你有把握該詞確實是在講那支股票時才替換。沒把握就不要改。
- 語境要合理：前後文明顯在談該公司或該產業，才有理由替換。
- 可疑詞若本身就是正常的一般詞彙（例如「大家」「新高」「經濟」「壓力」），不要替換。
- 候選清單只是模糊比對結果，可能全部都是錯的。

輸出格式：只輸出 JSON 物件，不要有任何其他文字或 markdown 標記。
{"results": [{"index": 項目編號, "replace": true/false, "symbol": "代號或null", "name": "股票名稱或null", "confidence": 0.0到1.0, "reason": "簡短理由"}]}

reason 必須簡短（20 字內）且不可包含雙引號或換行。
每個項目都要有對應的結果，index 要跟輸入的編號一致。"""


class LLMError(RuntimeError):
    pass


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


def _post(messages: list[dict]) -> str:
    if not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY not set in backend/.env")

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
    if response.status_code != 200:
        raise LLMError(f"OpenRouter {response.status_code}: {response.text[:300]}")
    return response.json()["choices"][0]["message"]["content"]


def judge_batch(items: list[dict]) -> dict[int, dict]:
    """批次判斷。

    items 每筆需有 sentence / suspect / candidates。
    回傳 {輸入索引: 判斷結果}；model 漏掉的項目不會出現在結果中。
    """
    blocks = []
    for i, item in enumerate(items):
        cands = "、".join(
            f"{c['symbol']} {c['name_zh']}({c['score']:.0f})" for c in item["candidates"]
        )
        blocks.append(
            f"[{i}] 句子：{item['sentence']}\n"
            f"    可疑詞：{item['suspect']}\n"
            f"    候選：{cands}"
        )

    content = _post(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
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
            out[int(r["index"])] = r
        except (KeyError, TypeError, ValueError):
            continue
    return out
