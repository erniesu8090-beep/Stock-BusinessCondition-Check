# 專案 AI 協作規則 (AGENTS.md)

> **接手工作前先讀 `docs/PROGRESS.md`**

## 1. 專案基本資訊
- **專案名稱**：台股大戶撤退與籌碼風險判讀儀表板
- **目標執行環境**：住家開發機（`home`，Windows 11，個人專案使用獨立 `.venv` Python 3.12）
- **核心原則**：
  - 已完成商業軟體式完工收納：根目錄僅放 `台股籌碼儀表板.lnk`、`start_dashboard.bat`、`台股籌碼風險判讀儀表板_操作手冊.pdf`、隱藏的 `AGENTS.md`（Windows `attrib +h`）與設定檔（`.gitignore`、`requirements.txt`、`.venv/`）；`README.md`、`PROGRESS.md` 與報告 PDF 已收進 `docs/`。
  - 所有 Python 程式與單檔前端 `code_artifact.html` 收在 `src/`，市場快取與中繼數據收在 `data/`，靜態圖檔與圖示素材收在 `assets/`（如 `assets/app.ico`、`assets/mascots/`）。
  - 嚴禁使用 `pip install --user` 或全域安裝套件；一律使用 `.venv\Scripts\python.exe`。
  - 一律以繁體中文（台灣用語）回覆與撰寫註解文件。

## 2. 技術選擇與資料來源
- **後端爬蟲與伺服器**：Python 3.12（`.venv`）+ `requests>=2.31.0` + 內建 `http.server`（`ThreadingHTTPServer`），免除 `pandas`/`lxml` 編譯依賴。
- **官方免費數據源**：
  - Yahoo Finance v8 Chart API：`^TWII`、`2330.TW`、`^TFNI`（備援 `0055.TW`）及觀察名單 12 檔精選個股（`2376/2357/2881/3231/2382/2385/2882/6669/3034/2317/2330/5871`），計算日線 KD(9,3,3) 與 5/10/20/60MA 及動態買點評估。
  - 台灣證交所 TWSE：`FMTQIK`（成交金額與均量比）、`BFI82U`（三大法人現貨 5 日買賣超）、`MI_MARGN`（大盤融資餘額變化與近 40 日成本推算整戶維持率）。
  - 台灣期交所 TAIFEX：`futContractsDateDown`（三大法人台指期／小台／微台未平倉淨口數）與 `openapi.taifex.com.tw/v1/DailyMarketReportFut`（全市場小微台總未平倉量，微台以 1/5 權重合併計算散戶多空比）。
  - 集保中心 TDCC & 籌碼擴充：`smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5`（每週千張大戶與散戶人數，存歷史快照於 `data/tdcc_weekly_history.json`）、`rsi.pttstock.tw`（近 20 週 400 張大戶趨勢補齊 4 週變化）與富邦嘉實 `fubon-ebrokerdj.fbs.com.tw`（前 15 大主力買賣超淨張數與最大買賣分點）。
- **已知的坑**：
  - 集保中心 TDCC OpenData 下載（`getOD.ashx?id=1-5`）請求必須帶瀏覽器 `User-Agent`，否則會觸發 `TooManyRedirects`；CSV 證券代號欄位含尾隨空白須 `.strip()`。
  - 集保 TDCC OpenData CSV (2.4MB) 未壓縮常耗時 20～35 秒，應使用週快取並提供失敗回退，前端同步逾時須 ≥60 秒；Windows 本機埠號探測須用 `socket.bind` 而非 `connect_ex`（未開啟埠會阻塞約 2 秒）。
  - 管制／受限網路會重置 `cdn.tailwindcss.com` 連線，且單檔需支援完全離線開啟；`src/code_artifact.html` 嚴禁依賴外部 CSS CDN，新增未用過的 Tailwind class 須同步預編譯靜態 CSS 內嵌於 `<style>`。
  - GitHub Actions 排程會鎖定入隊時的舊 `headSha` 且 `upload-pages-artifact` 後偶發 Artifact API 索引延遲，自動推檔工作流須明確 `ref: main` + `fetch/rebase` 重試並為 `deploy-pages` 保留緩衝與重試步驟。
- **前端介面**：單一 HTML 檔案（`src/code_artifact.html`，預編譯靜態 Tailwind CSS 內嵌 + 原生 JS + `localStorage` + 原生 `page-mascot` (`kamran`) 互動元件），數據透過 `<script id="embedded-market-data" type="application/json">` 區塊注入更新。

## 3. 不可更動與安全規範
- Python 內嵌多行 HTML/JS 範本時，若於 Python 檔案內使用字串必須使用原始字串 `r"""..."""`，或將 HTML 範本獨立維持正確 UTF-8 編碼；每次更新後必須驗證抽出 `<script>` 區塊以 `node --check` 通過語法檢查。
- `.bat` 啟動器必須使用純 ASCII 字元與 CRLF 換行，開頭必須有 `cd /d "%~dp0"`。
- 爬蟲嚴禁寫死硬編碼磁碟代號（如 `C:\`），一律使用 `pathlib.Path(__file__).resolve().parent.parent` 定位專案根目錄。
- Gemini API Key 僅能由使用者在瀏覽器前端選填並存於使用者本機 `localStorage`，絕不可硬編碼任何金鑰於程式碼中。

## 4. 驗證方式
1. 官方數據同步單次執行測試：
   ```powershell
   .\.venv\Scripts\python.exe src\server.py --sync-only
   ```
   檢查 `data/latest_market_data.json` 與 `src/code_artifact.html` 是否成功寫入最新有效交易日數值。
2. 前端 JS 語法檢查：抽出 `src/code_artifact.html` 內所有內嵌 `<script>` 並以 `node --check` 驗證無語法錯誤。
3. 本機 API 伺服器測試：啟動 `src\server.py` 測試 `GET /`、`GET /api/latest` 與 `POST /api/sync` 回傳 HTTP 200 且結構正確。
4. 靜態 Pages 建置測試：執行 `.\.venv\Scripts\python.exe src\build_pages.py --output _site` 確認産出 `_site/index.html`、`manifest.webmanifest` 與 `assets/`，並驗 `_site/index.html` 內嵌 `<script>` 通過 `node --check`。
