# 財經影片自動化內容 Pipeline

## 專案目標

從訂閱的 YouTube 財經頻道自動擷取新影片 → 轉逐字稿 → 校正股票代號/名稱 →
生成摘要與社群文案 → 人工審核 → 發布到 Instagram（全自動）、Vocus／Medium（產出草稿，手動發布）。

## 核心原則

- **純本機、不上雲、不用付費 API 之前不花錢。** 目前沒有效益驗證前，一切在本機跑。
- **人工審核閘門不可省略。** 財經內容出錯成本高，AI 產出必須經過人看過才能發布。
- **著作權界線：不對外發布逐字稿本身。** 逐字稿只在系統內部當作處理素材，對外發布的
  只有重新整理過的摘要與觀點，並附上原影片來源連結。
- **先驗證最高風險的部分，再往下做。** 股票代號校正的準確率是整個專案能否成立的關�键，
  優先做這塊並用真實逐字稿測試，準確率不過關就要重新評估可行性（例如改成只處理有官方字幕的影片）。

## 技術選型（本機版）

| 項目 | 選擇 | 原因 |
|---|---|---|
| 語言 | Python | 核心依賴（yt-dlp, faster-whisper, rapidfuzz, Pillow, playwright）都是 Python 生態 |
| DB | SQLite | 單機、寫入量小，不需要 Postgres |
| 排程 | APScheduler 常駐程式，或系統工作排程器 | 不需要雲端排程服務 |
| 佇列 | 無，單一 script 依序跑 pipeline stage | 個人用途不需要 Celery/Redis 這種分散式架構 |
| 語音轉錄 | faster-whisper（本機，有 GPU 用 CUDA） | 免費，避免付費 API |
| 前端 | Angular（審核後台） | 熟悉的技術棧，逐字稿 diff 檢視、摘要編輯這類資料密集介面適合 |
| 後端 API | FastAPI | 跟 Python pipeline 同語言，方便 |
| YouTube 來源監控 | RSS Feed（見下方），不用 YouTube Data API | 避免配額限制與 API Key 申請 |

## YouTube 來源監控

用 YouTube 內建 RSS，不用官方 Data API（配額限制、需要 API Key）：

```
頻道：https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID
播放清單：https://www.youtube.com/feeds/videos.xml?playlist_id=PLAYLIST_ID
```

- 只會回傳最新約 15 支影片，新訂閱的頻道要先用
  `yt-dlp --flat-playlist --print id URL` 手動拉歷史清單做初始化
- 用 `video_id` 判斷是否為新影片，不要用標題（標題可能事後被改）
- 輪詢頻率抓 15-30 分鐘一次即可，不要抓太頻繁

## 資料表設計（SQLite）

```
sources          頻道/播放清單訂閱設定 (channel_id, type, name, active)
videos           video_id, title, published_at, status, source_id
transcripts      video_id, raw_text, corrected_text, segments(json)
corrections      video_id, original, corrected, confidence, candidates(json), human_reviewed
tickers          symbol, market(TW/US), name_zh, name_en, aliases(json), phonetic
summaries        video_id, version, content, ig_caption, created_at
media_assets     summary_id, file_path, type, width, height
posts            summary_id, platform, status, external_url, published_at, error
jobs             video_id, stage, status, retry_count, error_detail, updated_at
```

`corrections` 表要留完整修正紀錄（原詞、修正後、信心度、候選清單、是否人工覆核過），
方便事後稽核 AI 改了什麼，也能反過來優化字典。

## Pipeline 狀態機

```
PENDING → DOWNLOADING → TRANSCRIBING → CORRECTING
        → SUMMARIZING → REVIEW → RENDERING → PUBLISHING
        → PUBLISHED | FAILED
```

`REVIEW` 是唯一停下來等人工的狀態，可以從任何後續狀態退回重跑。

## 股票詞校正邏輯（retrieve-then-generate 模式）

不是傳統 RAG（語意檢索），字典規模小，用 fuzzy match 更準確：

1. 逐字稿分句，抓可疑片段（數字+英文字母組合、疑似公司名）
2. 用 `rapidfuzz` 對 `tickers` 表做 fuzzy match，取 top-k 候選
3. 有候選才丟給 Claude：附上該句 context + 候選清單，讓它判斷是否替換／替換成哪個，
   沒把握時明確指示不要亂改
4. 合併回逐字稿，寫入 `corrections` 表留紀錄

Whisper 對股票代號的錯誤多半是發音相近造成，例如「00981A」這類代號容易聽錯，
所以 fuzzy match 時應考慮語音相似度，不只是字面相似度。

## 開發優先順序

1. **股票詞校正模組**（最高風險，優先驗證） — 拿一段真實財經影片逐字稿測試準確率
2. 單機 CLI 跑通完整 pipeline，輸出 Markdown 到本機檔案
3. SQLite + 狀態管理
4. Angular 審核後台，重點是逐字稿審核器（左右對照、逐項接受/還原修正）
5. IG 圖卡渲染（Pillow 或 HTML→截圖）+ Instagram Graph API 發文
6. YouTube RSS 輪詢自動化

## 已知限制與待決事項

- Vocus 無官方發文 API，改為產出格式化 Markdown 草稿，人工複製貼上發布
- Medium 官方 API 已停用（新申請拿不到 token），同樣走草稿 + 人工發布
- Instagram 純文字無法發文，摘要要轉成圖卡才能用 Graph API 發布
- 財經內容需附免責聲明
