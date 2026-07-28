# finvid-pipeline

財經影片自動化內容 Pipeline：從 YouTube 財經頻道擷取影片 → 轉逐字稿 → 校正股票代號 →
生成摘要與社群文案 → 人工審核 → 發布。

專案目標、原則與架構決策見 [CLAUDE.md](CLAUDE.md)。本文件只講「怎麼把環境跑起來」。

## 環境需求

| 項目 | 版本 | 備註 |
|---|---|---|
| Python | 3.14（3.11+ 應該都可以） | backend |
| Node.js | 22（20+ 即可） | frontend |
| ffmpeg | 任意近期版本 | faster-whisper 解碼音訊用，需在 PATH |

GPU 非必要。目前是 CPU 模式（`compute_type="int8"`），49 分鐘音檔約需 15-20 分鐘轉錄。
有 NVIDIA GPU 的話可以把 `app/transcriber.py` 的 `device` 改成 `"cuda"` 加速。

## 安裝

```bash
git clone https://github.com/gagahsu/finvid-pipeline.git
cd finvid-pipeline
```

### Backend

```bash
cd backend
python -m venv .venv

# Windows (Git Bash)
source .venv/Scripts/activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

設定環境變數：

```bash
cp .env.example .env
```

編輯 `.env` 填入 OpenRouter API key（到 https://openrouter.ai 申請，免費層即可）：

```
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=openrouter/free
DATABASE_URL=sqlite:///./finvid.db
```

`openrouter/free` 是 OpenRouter 的自動路由設定，會挑當下可用的免費 model，
不用自己指定特定 model 名稱。

> `.env` 已列入 `.gitignore`，不會進版控。每台設備都要各自建立。

啟動 API：

```bash
uvicorn app.main:app --reload
```

健康檢查：http://127.0.0.1:8000/health

### Frontend

```bash
cd frontend
npm install
npm start
```

開發伺服器：http://localhost:4200

審核後台的資料來自 backend，`/api` 由 `proxy.conf.json` 轉到 http://127.0.0.1:8000，
所以要先把上面的 uvicorn 跑起來，畫面才有東西。

## 初始化資料

### 建立股票代號字典

從證交所（TWSE）與櫃買中心（TPEx）的公開 API 抓上市櫃公司清單，寫入 `tickers` 表：

```bash
cd backend
mkdir -p ../data

curl -s "https://openapi.twse.com.tw/v1/opendata/t187ap03_L" \
  -o ../data/twse_listed.json
curl -s "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O" \
  -o ../data/tpex_otc_basic.json

python build_tickers.py
```

預期輸出約 1,983 檔（上市 1,092 + 上櫃 891）。

> 這兩份 API 只涵蓋個股，不含 ETF（例如 0050、00981A）。ETF 代號要另外補進 `tickers` 表。

## 使用

### 下載影片音訊

```bash
cd backend
python -m app.downloader "https://www.youtube.com/watch?v=VIDEO_ID" ../data/audio
```

存成 `../data/audio/VIDEO_ID.m4a`。

### 轉錄逐字稿

```bash
python -m app.transcriber ../data/audio/VIDEO_ID.m4a
```

輸出同目錄的 `VIDEO_ID.json`，包含完整逐字稿與帶時間軸的 segments。
進度寫在 stderr，可即時觀察。

### 跑完整條 pipeline

```bash
python -m app.cli run "https://www.youtube.com/watch?v=VIDEO_ID"
python -m app.cli status VIDEO_ID
```

依序跑 download → transcribe → correct → apply → summarize，最後停在 `REVIEW`。
`REVIEW` 之後的 RENDERING / PUBLISHING 一律要人在審核台按下去才會走，CLI 不會自己往下跑。

會花 OpenRouter 配額的是 `correct`（約 18 個 request）與 `summarize`（1 個）。
`--skip-judge` 只跳過 correct 那段。

### 產出摘要草稿

```bash
python -m app.summarizer VIDEO_ID
```

寫入 `summaries` 表（同一支影片可有多版，version 自動遞增），
並輸出 `data/drafts/VIDEO_ID_v<n>.md` 供人工複製貼上到 Vocus / Medium。

草稿的來源連結與免責聲明由程式補上，不交給 model —— 這兩項少一個就是合規問題，
而 model 漏掉指示是常態。

**逐字稿不會被對外發布**：CLAUDE.md 規定只能發布重新整理過的摘要。
prompt 有交代不可照抄，但 prompt 是請求不是保證，所以產出後另外用
`check_verbatim()` 掃描 —— 與逐字稿有連續 20 字以上重疊就讓這階段失敗，
不會靜靜寫進 DB。比對前會先去掉標點，否則加個逗號就能繞過。

## 目錄結構

```
backend/
  app/
    main.py          FastAPI 進入點
    config.py        環境變數設定（pydantic-settings）
    db.py            SQLite schema 與遷移
    pipeline.py      狀態機與 job 記錄
    cli.py           pipeline CLI 入口
    downloader.py    YouTube 音訊下載
    transcriber.py   faster-whisper 語音轉錄
    corrector.py     股票代號偵測與校正判斷
    llm.py           OpenRouter client
    applier.py       把 corrections 套回逐字稿
    hotwords.py      從累積修正產生 Whisper hotwords
    api/             審核後台的 REST 端點
  build_tickers.py   建立股票代號字典
  requirements.txt
  finvid.db          SQLite（不進版控）
frontend/            Angular 審核後台
data/                音訊、逐字稿、來源資料（不進版控）
```

## 已知問題與技術決策

### 為什麼用 pytubefix 而不是 yt-dlp

YouTube 已對 yt-dlp 強制 SABR streaming：多數 client 回傳的 format 不含直接下載 URL，
少數能拿到 URL 的（`android_vr`）實際抓取時回 HTTP 403。
PO Token provider（bgutil）與瀏覽器 cookies 都無法解決，因為問題出在 format 解析層而非驗證層。

`pytubefix` 目前仍能解到 non-SABR 的 itag 140 音訊流，實測可完整下載。
若日後失效，替代方向是自架 [cobalt](https://github.com/imputnet/cobalt)，
但需部署在本機以外的環境（與專案「純本機」原則相衝突，屆時要重新評估）。

### Windows console 編碼

Windows 中文環境的 console 預設 cp950，直接 `print` 逐字稿內容遇到簡體字會拋
`UnicodeEncodeError`。所有逐字稿內容一律寫檔（`encoding="utf-8"`），
console 只輸出進度數字。

## 開發進度

- [x] 環境骨架（FastAPI + Angular）
- [x] YouTube 音訊下載
- [x] faster-whisper 本機轉錄
- [x] 股票代號字典（台股上市櫃）
- [x] 股票代號校正模組（rapidfuzz + OpenRouter）
- [x] 校正結果套回逐字稿（corrected_text）
- [x] CLI 跑通 pipeline（下載 → 轉錄 → 校正 → 套用）
- [x] SQLite 狀態管理（狀態機 + jobs）
- [x] Angular 審核後台
- [x] 摘要與社群文案生成（SUMMARIZING）+ Markdown 草稿輸出
- [ ] 審核後台顯示／編輯摘要（目前只有逐字稿校正的介面）
- [ ] IG 圖卡渲染（media_assets 表已備，還沒有產生圖的程式）
- [ ] Instagram Graph API 發文（posts 表已備，狀態停在 draft）
- [ ] YouTube RSS 輪詢

### 校正準確率

CLAUDE.md 把「股票代號校正準確率」列為專案能否成立的關鍵。

現在有固定的評測基準：`data/eval/` 底下放著凍結的可疑詞集（207 個詞，
從 ef_V8R3Ld8Q 的逐字稿機械產生）與人工標註的答案集（16 個真陽性、
12 個判不出來的排除計分、其餘為真陰性）。

```bash
python eval_prompt.py v3 --prompt prompts/v3.txt   # 跑一輪並評分（18 個 request）
python eval_prompt.py --score baseline v2 v3       # 只比對已存檔結果，不打 API
```

評測結果不寫 corrections 表，各自存成 `data/eval/run_<名稱>.json`，
所以任兩輪隨時可以重新比較而不必再花配額。

2026-07-28 的實測（單支影片，樣本量小，只能當方向參考）：

| prompt | 提報數 | Precision | Recall |
|---|---|---|---|
| baseline | 11 | 50.0% | 43.8% |
| v2 | 11 | 63.6% | 43.8% |
| v3 | 28 | 53.6% | 93.8% |
| v2 ∩ v3 | 8 | 87.5% | 43.8% |

（baseline 的 11 筆裡有 2 筆受下述 index 錯位污染，實際 precision 約 58%。
v3 只跑難題子集 `subset_hard.txt`，其 precision 是上界。）

三件事從資料裡看出來：

**1. 「這個詞不是公司名」是循環論證。** baseline 與 v2 漏掉的真陽性，
理由幾乎都是「非公司名」——但可疑詞本來就是聽錯寫出來的字，長得像公司名就
不需要校正了。v3 把這句話明講成前提而非證據，recall 從 43.8% 跳到 93.8%。

**2. LLM 自報的 confidence 不能當安全閘。** v3 的誤判裡，
「非紅供應鏈」被改成飛宏是 confidence 1.0，中東紅海被改成鴻海是 0.95，
矽晶圓被改成精元是 0.93。`AUTO_APPLY_CONFIDENCE = 0.90` 擋不住這些。

**3. 兩個 prompt 的共識比 confidence 可靠。** 取交集 precision 87.5%，
唯一的誤判是「擊太→基泰」。代價是每支影片要跑兩輪（36 個 request）。

**4. 誤判會一路流進對外草稿。** 第一份摘要草稿裡出現「長佳概念股」——
影片講的是「漲價概念股」，長佳（4550）是精神科醫材公司，影片從沒提到它。
來源就是那筆 confidence 0.9、`status='auto'`、沒經人看過的修正：
它被自動套進 `corrected_text`，摘要階段照單全收，於是一家真實上市公司
被寫進了準備對外發布的內容裡。

人工閘門仍然攔得住（狀態停在 REVIEW），但這條鏈已經完整跑過一次，
說明 `AUTO_APPLY_CONFIDENCE` 不該繼續當作自動套用的依據。

仍待處理：

- **自動套用的閘門要換掉**：改用雙 prompt 共識，或乾脆先關掉自動套用、
  全部進人工審核。目前 `AUTO_APPLY_CONFIDENCE = 0.90` 擋不住任何東西。
- 只測過一支影片。要下結論得再標 2-3 支，尤其是不同主持人的口音。
- ETF 不在字典裡，「00981A」這類代號目前完全抓不到（recall 沒涵蓋這塊）。
- 真陰性的 precision 只在難題子集上量過，整份逐字稿的實際誤判率會更低
  （159 個平凡的真陰性沒被 v3 判斷過）。
