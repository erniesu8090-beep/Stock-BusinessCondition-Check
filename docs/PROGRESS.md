# 專案進度交接 (PROGRESS.md)

- **最後更新**：2026-11-22（`home`）
- **目前狀態**：已完成完工收納、自訂 `.ico` 圖示與操作手冊 PDF，全項實測通過。

## 已完成與架構決定
- 建立專案結構：`start_dashboard.bat`、`src/fetcher.py`、`src/server.py`、`data/latest_market_data.json`、`code_artifact.html`、`AGENTS.md`、`README.md`、`requirements.txt`、`.gitignore`。
- 完成官方免費公開數據爬蟲（TWSE + TAIFEX + Yahoo Finance），支援抓取外資期貨淨口數、近 5 日現貨買賣超、小台＋微台綜合散戶多空比、融資單日增減與維持率推算、台積電與金融指數均線結構及大盤日線 KD(9,3,3)。
- 完成本機輕量 HTTP API 伺服器與單檔離線嵌入雙模式運作（每次同步皆回寫 `code_artifact.html`）。
- 完成前端升級：暖米白看盤美學、新增散戶小微台多空比與融資維持率維度、個人資金加減碼試算器、`localStorage` 歷史紀錄與趨勢長條圖（含 JSON 匯出入）。
- 實測通過 `src/server.py --sync-only`、`node --check` JS 語法檢查與本機伺服器端點（`GET /`、`/api/health`、`/api/latest`、`POST /api/sync`），並依規範移除 `SPEC.md`。

## 下一步
- 目前無待辦；日後可依實際看盤習慣微調權重或擴充個股觀察清單。

## 待使用者決定
- 目前無。

## 修訂紀錄
- 2026-11-22：完成首版建置、實測驗證與 `SPEC.md` 搬移收尾。
- 2026-11-22（二版升級）：新增頂部「一言買賣決策大燈號」、整合《台股低本益比與成長潛力觀察名單.pdf》與 `5871 中租-KY` 共 12 檔個股即時報價與可買進價位自動評估、並加入右下角懸浮 `page-mascot` (`kamran`) 互動吉祥物。
- 2026-11-22（三版籌碼升級）：整合集保中心 TDCC（千張/400張大戶與 10 張以下散戶人數，存週歷史檔 `data/tdcc_weekly_history.json`）、`rsi.pttstock.tw` 近 4 週大戶變動與富邦嘉實前 15 大主力買賣超及最大買賣分點，於觀察名單與維度三即時展示大戶動向。
- 2026-11-22（啟動效能與卡頓修復）：新增集保 TDCC 本週快取命中與 45 秒逾時自動回退機制、與 12 檔個股並行抓取及終端機 `1/5`～`5/5` 即時進度輸出；伺服器改為秒開後於背景非同步同步（搭配互斥鎖與 `/api/health` 同步狀態）；前端「一鍵同步」逾時調寬至 65 秒並支援背景同步完成自動刷新畫面。
- 2026-11-22（冗餘清理、自訂圖示與完工收納）：清除 `src/fetcher.py` 未使用變數 `recent_3` 與 `code_artifact.html` 過時註解；設計多尺寸 `assets/app.ico` 並掛載至網頁 favicon（`/favicon.ico`）與根目錄捷徑 `台股籌碼儀表板.lnk`；將 `code_artifact.html` 收進 `src/`（同步調整為 `../assets/` 相對路徑與 `server.py` 的 `HTML_PATH`），將 `README.md`、`PROGRESS.md` 與觀察名單 PDF 收進 `docs/`，於根目錄產出 `台股籌碼風險判讀儀表板_操作手冊.pdf`，並將 `AGENTS.md` 改指 `docs/PROGRESS.md` 且設為 Windows 隱藏檔。
