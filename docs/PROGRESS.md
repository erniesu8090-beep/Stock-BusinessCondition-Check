# 專案進度交接 (PROGRESS.md)

- **最後更新**：2026-11-25（`home`）
- **目前狀態**：已完成完工收納、離線靜態 CSS 內嵌、盤中即時股價輪詢（`/api/realtime`）與 GitHub Pages 自動排程託管；並修復 GitHub Actions 排程延遲／連續觸發造成 `git push` 衝突（`fetch first`）及 `deploy-pages` 偶發找不到 artifact 的問題，待確認推送至 GitHub 後實測驗證。

## 已完成與架構決定
- 建立專案結構：`start_dashboard.bat`、`src/fetcher.py`、`src/server.py`、`data/latest_market_data.json`、`code_artifact.html`、`AGENTS.md`、`README.md`、`requirements.txt`、`.gitignore`。
- 完成官方免費公開數據爬蟲（TWSE + TAIFEX + Yahoo Finance），支援抓取外資期貨淨口數、近 5 日現貨買賣超、小台＋微台綜合散戶多空比、融資單日增減與維持率推算、台積電與金融指數均線結構及大盤日線 KD(9,3,3)。
- 完成本機輕量 HTTP API 伺服器與單檔離線嵌入雙模式運作（每次同步皆回寫 `code_artifact.html`）。
- 完成前端升級：暖米白看盤美學、新增散戶小微台多空比與融資維持率維度、個人資金加減碼試算器、`localStorage` 歷史紀錄與趨勢長條圖（含 JSON 匯出入）。
- 實測通過 `src/server.py --sync-only`、`node --check` JS 語法檢查與本機伺服器端點（`GET /`、`/api/health`、`/api/latest`、`POST /api/sync`），並依規範移除 `SPEC.md`。

## 下一步
- 經使用者確認 `git push` 將工作流修復推送至 `origin/main` 後，觸發一次 `workflow_dispatch` 驗證「回存快照」與「部署至 GitHub Pages」全步驟通過。

## 待使用者決定
- 目前無。

## 修訂紀錄
- 2026-11-22：完成首版建置、實測驗證與 `SPEC.md` 搬移收尾。
- 2026-11-22（二版升級）：新增頂部「一言買賣決策大燈號」、整合《台股低本益比與成長潛力觀察名單.pdf》與 `5871 中租-KY` 共 12 檔個股即時報價與可買進價位自動評估、並加入右下角懸浮 `page-mascot` (`kamran`) 互動吉祥物。
- 2026-11-22（三版籌碼升級）：整合集保中心 TDCC（千張/400張大戶與 10 張以下散戶人數，存週歷史檔 `data/tdcc_weekly_history.json`）、`rsi.pttstock.tw` 近 4 週大戶變動與富邦嘉實前 15 大主力買賣超及最大買賣分點，於觀察名單與維度三即時展示大戶動向。
- 2026-11-22（啟動效能與卡頓修復）：新增集保 TDCC 本週快取命中與 45 秒逾時自動回退機制、與 12 檔個股並行抓取及終端機 `1/5`～`5/5` 即時進度輸出；伺服器改為秒開後於背景非同步同步（搭配互斥鎖與 `/api/health` 同步狀態）；前端「一鍵同步」逾時調寬至 65 秒並支援背景同步完成自動刷新畫面。
- 2026-11-22（冗餘清理、自訂圖示與完工收納）：清除 `src/fetcher.py` 未使用變數 `recent_3` 與 `code_artifact.html` 過時註解；設計多尺寸 `assets/app.ico` 並掛載至網頁 favicon（`/favicon.ico`）與根目錄捷徑 `台股籌碼儀表板.lnk`；將 `code_artifact.html` 收進 `src/`（同步調整為 `../assets/` 相對路徑與 `server.py` 的 `HTML_PATH`），將 `README.md`、`PROGRESS.md` 與觀察名單 PDF 收進 `docs/`，於根目錄產出 `台股籌碼風險判讀儀表板_操作手冊.pdf`，並將 `AGENTS.md` 改指 `docs/PROGRESS.md` 且設為 Windows 隱藏檔。
- 2026-11-23（離線／受限網路靜態 CSS 內嵌修復）：於 `controlled` 環境實測發現 `cdn.tailwindcss.com` 連線遭重置導致整頁排版失效；改以 Tailwind CSS v3 預編譯本頁（含 JS 動態狀態）全部 251 個類別為純靜態 CSS 並直接內嵌進 `src/code_artifact.html` 的 `<style>`，同步移除外部 `cdn.tailwindcss.com` 與 Google Fonts 請求依賴，補上繁中系統字型備援與 `font-size-adjust: from-font`；實測 `--sync-only`、`GET /`、`GET /api/latest`、`POST /api/sync` 及 `node --check` 全數通過。
- 2026-11-23（盤中即時股價與明細升級）：於 `src/fetcher.py` 抽離 `_compute_stock_entry_metrics()` 並新增 `fetch_realtime_quotes()`（鉅亨網 `ws.api.cnyes.com` 零延遲主抓＋證交所 MIS 自動備援，處理 MIS `z="-"` 撮合空檔），同步於完整同步時疊加零延遲報價；於 `src/server.py` 新增帶 2 秒短快取之 `GET /api/realtime` 與 `/api/health` 的 `realtimeSupported` 標記；於 `src/code_artifact.html` 加入盤中 15 秒自動輪詢（含手動刷新、暫停開關與分頁隱藏暫停）、頂部「籌碼基準日」與「報價時間」分離、加權與台積電即時連動、表格及展開區盤中開高低量明細、價位變動微閃爍與買點／本益比／殖利率即時重算（並補齊靜態內嵌 CSS）；實測 `--sync-only`、靜態 CSS 類別稽核、`node --check` 及 `GET /api/realtime` 全數通過。
- 2026-11-24（手機版 GitHub Pages 自動化託管方案 B 準備）：於 `src/code_artifact.html` 加入 PWA meta 標籤、手機小螢幕 RWD（釋放 header sticky、表格首欄固定 `.wl-sticky-col`、縮小右下吉祥物與手機預設收合氣泡）及 `isStaticSiteMode()` 雲端靜態模式判斷（跳過無效本機 API 請求並隱藏即時輪詢按鈕）；新增 `src/build_pages.py` 打包 `_site/` 與 `manifest.webmanifest`，新增 `.github/workflows/pages_sync.yml` 設定週一至五 16:30/21:30（UTC+8）自動抓數並回存快照、發佈 Pages；修正 `src/fetcher.py` 兩處補齊台北時區（`UTC+8`）以免雲端 runner UTC 偏移。
- 2026-11-25（修復 GitHub Actions 自動更新衝突與部署重試）：追查 Actions 歷史日誌發現兩項卡關主因——(1) 排程進入佇列時會凍結舊 `headSha` 且連續觸發排隊後直接 `git push` 觸發 `rejected (fetch first)` 中斷部署；(2) `upload-pages-artifact` 後立即 `deploy-pages` 偶發 `Found 0 artifact(s)`。於 `.github/workflows/pages_sync.yml` 改為簽出指定 `ref: main`（`fetch-depth: 0`）、同步前對齊最新 `origin/main`、回存快照加入最多 3 次 `fetch + rebase` 衝突重試與備援不中斷 Pages 發佈，並於上傳 artifact 後加入 8 秒緩衝與 `deploy-pages` 失敗 15 秒自動重試機制。
