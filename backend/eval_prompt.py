"""校正準確率評測。

拿凍結的可疑詞集（data/eval/frozen.json）跑指定的 prompt 變體，
對照人工標註答案（data/eval/ground_truth.json）算 precision / recall。

刻意不寫 corrections 表：評測是實驗，寫進去會污染真實的審核資料，
而且同一支影片重跑多次會累積成一堆重複列。結果各自存成
data/eval/run_<name>.json，之後要比較任兩次不必重新花 API 配額。

    python eval_prompt.py baseline           # 用 llm.SYSTEM_PROMPT 跑一輪
    python eval_prompt.py v2 --prompt prompts/v2.txt
    python eval_prompt.py --score baseline v2   # 只比對已存檔的結果，不打 API

OpenRouter 免費層每日 50 個 request，一輪要 18 個，一天最多跑兩輪。
"""

import argparse
import json
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent / "data" / "eval"
FROZEN = EVAL_DIR / "frozen.json"
TRUTH = EVAL_DIR / "ground_truth.json"


def load_truth() -> tuple[dict[str, str], set[str]]:
    d = json.loads(TRUTH.read_text(encoding="utf-8"))
    return d["positives"], set(d["uncertain"])


def run(name: str, prompt: str | None, limit: int | None, terms: Path | None) -> Path:
    from app import llm

    items = json.loads(FROZEN.read_text(encoding="utf-8"))
    if terms:
        # 子集模式：只跑指定的詞。用來在配額不夠跑整輪時針對 recall 做探測。
        # 注意這樣算出的 precision 沒有意義 —— 子集刻意塞滿難題，
        # 真陰性的比例跟實際逐字稿完全不同。
        wanted = {
            line.strip()
            for line in terms.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        items = [x for x in items if x["term"] in wanted]
        missing = wanted - {x["term"] for x in items}
        if missing:
            print(f"warning: {len(missing)} terms not in frozen set", flush=True)
    if limit:
        items = items[:limit]

    verdicts: dict[str, dict] = {}
    total = (len(items) + llm.BATCH_SIZE - 1) // llm.BATCH_SIZE
    for start in range(0, len(items), llm.BATCH_SIZE):
        batch = [{**x, "suspect": x["term"]} for x in items[start : start + llm.BATCH_SIZE]]
        n = start // llm.BATCH_SIZE + 1
        try:
            got = llm.judge_batch(batch, system_prompt=prompt)
        except llm.RateLimitError as exc:
            print(f"batch {n}/{total}: quota exhausted, stopping. {exc}", flush=True)
            break
        except llm.LLMError as exc:
            print(f"batch {n}/{total}: ERROR {exc}", flush=True)
            continue
        for i, item in enumerate(batch):
            if i in got:
                verdicts[item["term"]] = got[i]
        print(f"batch {n}/{total}: {len(verdicts)} judged", flush=True)
        time.sleep(1.0)

    out = EVAL_DIR / f"run_{name}.json"
    out.write_text(
        json.dumps({"name": name, "verdicts": verdicts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def score(name: str, subset: set[str] | None = None) -> dict:
    """算 precision / recall。

    判定用「有沒有替換」加「換對名稱沒有」兩層：replace=true 但換成錯的股票
    仍然算誤判，因為套回逐字稿後就是一個錯字。
    """
    positives, uncertain = load_truth()
    data = json.loads((EVAL_DIR / f"run_{name}.json").read_text(encoding="utf-8"))
    verdicts = data["verdicts"]
    if subset:
        # 把整輪的結果限縮到子集，才能跟子集跑出來的結果比。
        verdicts = {k: v for k, v in verdicts.items() if k in subset}
        positives = {k: v for k, v in positives.items() if k in subset}

    tp, fp, fn, wrong_name = [], [], [], []
    for term, v in verdicts.items():
        if term in uncertain:
            continue
        replaced = bool(v.get("replace"))
        expected = positives.get(term)
        if replaced and expected and v.get("name") == expected:
            tp.append(term)
        elif replaced and expected:
            wrong_name.append((term, v.get("name"), expected))
        elif replaced:
            fp.append((term, v.get("name"), v.get("confidence"), v.get("reason")))
    for term, expected in positives.items():
        if term in verdicts and not bool(verdicts[term].get("replace")):
            fn.append((term, expected, verdicts[term].get("reason")))
        elif term not in verdicts:
            fn.append((term, expected, "(未判斷)"))

    hit = len(tp)
    flagged = hit + len(fp) + len(wrong_name)
    truth_n = len([t for t in positives if t not in uncertain])
    return {
        "name": name,
        "judged": len(verdicts),
        "precision": hit / flagged if flagged else 0.0,
        "recall": hit / truth_n if truth_n else 0.0,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "wrong_name": wrong_name,
    }


def report(names: list[str], subset: set[str] | None = None) -> None:
    lines = []
    for name in names:
        s = score(name, subset)
        lines.append(
            f"=== {s['name']}  judged={s['judged']}  "
            f"P={s['precision']:.1%}  R={s['recall']:.1%}  "
            f"TP={len(s['tp'])} FP={len(s['fp'])} FN={len(s['fn'])} "
            f"WRONG={len(s['wrong_name'])}"
        )
        lines.append(f"  TP: {'、'.join(s['tp'])}")
        lines.append("  FP:")
        for term, name_, conf, reason in s["fp"]:
            lines.append(f"    {term} -> {name_} ({conf}) {reason}")
        lines.append("  FN:")
        for term, expected, reason in s["fn"]:
            lines.append(f"    {term} 應為 {expected}｜{reason}")
        if s["wrong_name"]:
            lines.append("  換錯名稱:")
            for term, got, expected in s["wrong_name"]:
                lines.append(f"    {term} -> {got}（應為 {expected}）")
        lines.append("")
    text = "\n".join(lines)
    # Windows console 是 cp950，逐字稿內容直接 print 會炸；一律寫檔。
    (EVAL_DIR / "report.txt").write_text(text, encoding="utf-8")
    print(f"report written: {EVAL_DIR / 'report.txt'}")
    for line in lines:
        if line.startswith("==="):
            print(line.encode("ascii", "replace").decode())


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("names", nargs="+")
    p.add_argument("--prompt", help="prompt 檔路徑；省略則用 llm.SYSTEM_PROMPT")
    p.add_argument("--score", action="store_true", help="只評分已存檔的結果")
    p.add_argument("--limit", type=int, help="只跑前 N 個詞（省配額用）")
    p.add_argument("--terms", type=Path, help="只跑檔案裡列出的詞（一行一個）")
    args = p.parse_args()

    subset = None
    if args.terms:
        subset = {
            line.strip()
            for line in args.terms.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }

    if args.score:
        report(args.names, subset)
        sys.exit()

    if len(args.names) != 1:
        sys.exit("跑評測一次只能指定一個名稱")
    prompt = Path(args.prompt).read_text(encoding="utf-8") if args.prompt else None
    out = run(args.names[0], prompt, args.limit, args.terms)
    print(f"saved: {out}")
    report(args.names, subset)
