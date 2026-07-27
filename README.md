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

## 目錄結構

```
backend/
  app/
    main.py          FastAPI 進入點
    config.py        環境變數設定（pydantic-settings）
    downloader.py    YouTube 音訊下載
    transcriber.py   faster-whisper 語音轉錄
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
- [ ] **股票代號校正模組**（rapidfuzz + OpenRouter）← 最高風險，優先驗證
- [ ] CLI 跑通完整 pipeline
- [ ] SQLite 狀態管理
- [ ] Angular 審核後台
- [ ] IG 圖卡渲染與發文
- [ ] YouTube RSS 輪詢
