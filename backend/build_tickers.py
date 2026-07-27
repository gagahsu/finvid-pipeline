import json
import re
import sqlite3
from pathlib import Path

# 證交所/櫃買的簡稱帶交易狀態註記：-KY（外國註冊）、-創（創新板）、*（特殊交易）。
# 這些不會被唸出來，留在 name_zh 只會拉低比對分數，所以剝掉當主要比對名，
# 原始官方簡稱保留進 aliases。
# -KY創 要整體優先匹配：拆開會在剝掉 -KY 後留下沒有連字號的「創」，
# 而裸的「創」不能剝，會誤傷正常股名。
ANNOTATION = re.compile(r"(-KY創|-KY|-創|-DR|\*)+$")


def clean_name(name: str) -> str:
    return ANNOTATION.sub("", name).strip()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = Path(__file__).resolve().parent / "finvid.db"

listed = json.loads((DATA_DIR / "twse_listed.json").read_text(encoding="utf-8"))
otc = json.loads((DATA_DIR / "tpex_otc_basic.json").read_text(encoding="utf-8"))

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS tickers (
    symbol TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    name_zh TEXT NOT NULL,
    name_en TEXT,
    aliases TEXT,
    phonetic TEXT
)
""")
cur.execute("DELETE FROM tickers")

rows = []
for row in listed:
    symbol = row.get("公司代號", "").strip()
    name_full = row.get("公司名稱", "").strip()
    name_short = row.get("公司簡稱", "").strip()
    name_en = row.get("英文簡稱", "").strip()
    if not symbol:
        continue
    official = name_short or name_full
    match_name = clean_name(official)
    aliases = json.dumps(
        [a for a in {name_full, official} if a and a != match_name], ensure_ascii=False
    )
    rows.append((symbol, "TW", match_name, name_en, aliases, None))

for row in otc:
    symbol = row.get("SecuritiesCompanyCode", "").strip()
    name_full = row.get("CompanyName", "").strip()
    name_short = row.get("CompanyAbbreviation", "").strip()
    symbol_en = row.get("Symbol", "").strip()
    if not symbol:
        continue
    official = name_short or name_full
    match_name = clean_name(official)
    aliases = json.dumps(
        [a for a in {name_full, official} if a and a != match_name], ensure_ascii=False
    )
    rows.append((symbol, "TW", match_name, symbol_en or None, aliases, None))

cur.executemany(
    "INSERT OR REPLACE INTO tickers (symbol, market, name_zh, name_en, aliases, phonetic) VALUES (?, ?, ?, ?, ?, ?)",
    rows,
)
conn.commit()

count = cur.execute("SELECT COUNT(*) FROM tickers").fetchone()[0]
print(f"Inserted {count} tickers into {DB_PATH}")
for r in cur.execute("SELECT * FROM tickers LIMIT 5"):
    print(r)

conn.close()
