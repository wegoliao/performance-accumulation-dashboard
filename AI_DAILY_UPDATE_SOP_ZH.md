# 四策略截圖、成交回報與 GitHub Pages 更新 SOP

2026-10-01 Codex 實作並驗證。此文件取代舊 `DEEPSEEK_RUNBOOK.md` 的正典、匯出和缺卡描述；舊檔留作歷史。唯一根目錄 `D:/Quant_Grill_Lab`。先遵守根 AGENTS.md 與 Knowl 查詢流程。

## 正典與口徑

| 資料 | 寫入位置 | 注意事項 |
|---|---|---|
| 8/10–8/24 歷史卡 | 主專案 `data/strategy_signals.tsv`、`data/strategy_sleeve_header.tsv` | 不以舊 TSV 覆寫 8/25 後的最新卡 |
| 8/25 起卡片逐列 | 本 repo `inputs/signal_history.csv` | key = asof_date + strategy_id + stock_code |
| 卡片表頭 | `inputs/strategy_card_returns.csv` | 原圖表頭原樣保存；不是帳戶 NAV，也不是日報酬 |
| 最新卡 | `inputs/latest_strategy_signals.csv` | history 最新一天子集；恰好一個日期 |
| 使用者尚缺費稅的成交 | `inputs/owner_reported_fills_YYYY-MM-DD.csv` | 回報已發生成交；不是委託指令；未知值留空 |
| 已完成費稅／淨收付的四策略成交 | `inputs/actual_fills.csv` | 只能 BUY/SELL 與四策略 ID；unique trade_id；不得塞入融券或未知淨收付 |
| 券商已實現損益 | `inputs/broker_realized.csv` | 需券商成本、淨收款、實現損益；不能用毛價差冒充 |
| Quant_Card 可讀匯整 | 主專案 `Quant_Card/cards.csv`、`card_headers.csv`、`latest.csv` | 由 `build_cards_csv.py` 產生，不手改 |

`Quant_Card/headers_manual.csv` 是先前因日期耦合而暫存的表頭。2026-10-01 已同步進 dashboard 正典，不再為了避免 KeyError 把新表頭藏在外面。遇到新欄位先讀實際 CSV header；目前表頭 CSV 有 source_basis、source、strategy_name、daily_return_pct，後兩欄本次未填。

## 收圖流程

1. 根目錄 `Quant_Card` 是收件匣，`processed` 是已驗證圖片。逐張看圖，讀圖中日期，不能用截圖檔名當資料日。一張圖可能有不同日期與多策略。
2. 記錄 SHA-256、日期、策略、每列代碼、名稱、產業、進場、收盤、紅綠幅度、訊號及表頭。產業截斷原樣保留並加註，不自補。辨識不清保持待確認。
3. 紅字報酬為正、綠字為負、灰字零；綠色「出」是訊號顏色，不能據此判報酬正負。保留 `(抱)`、`(進*)` 等括號與星號。突破括號列為來源反向口徑，不能按 entry/close 改成多頭報酬。原圖 -0.1 即使 entry=close 仍保留。
4. 依 key 去重；重複圖片不重複入庫。若原始卡修正既有轉錄，留 before/after 證據。來源卡表頭與可見平均不一致，兩者保留且標 mismatch，不能改數字湊綠燈。
5. 更新 history、表頭，再從最新日期生成 latest。純「進／出」填下一交易日；注意既有程式只有週末處理，遇休市先核實交易日，不盲目採用 weekday 推算。
6. 跑主專案 `.venv/Scripts/python.exe Quant_Card/build_cards_csv.py`。缺來源日期不得前填。2026-09-25/28 未提供卡片；缺平日提示不等於漏卡，需另外核對休市日。
7. dashboard build 與 tests 通過、逐圖核對完成後，更新 `screenshot_index.csv`，把原圖移到 `processed`，確認 SHA 未變。

本次批次可重播入口（固定 2026-10-01，不能拿來匯入未來圖片）：主專案 `Quant_Card/import_20261001.py`、`reconcile_fills_20261001.py`、`finalize_20261001.py`。證據在 `Quant_Card/evidence_20261001/`；不要公開原始帳號或憑證。轉錄腳本重跑會保留 key 去重，但會更新本次 computer_time 收據。finalize 是本次 migration，會寫入固定日期頁面文字，禁止作每日通用入口。

## 成交對帳

用成交日、委託單號、股票、買賣別、股數、價核對。既有 trade_id 可含日期或策略後綴；不能只用精確 ID 字串找重複。同委託碎筆必須保留每筆，或在確認後有可追溯的聚合表，避免最低手續費被重複估算。

歷史回報的「成交量」按整股張（1 張＝1,000 股）；本次 10/1 碎筆量按股。歷史表時間是委託時間，不能寫成 fill_time 或覆寫更晚的既有成交時間。原文「券賣」＝開空、「券買」＝回補，不是現股 SELL/BUY。3055 應另列，不塞進四策略多頭帳。

10/1 結果：24 筆回報；15 筆歷史已記錄，7 筆新現賣碎筆，2 筆融券。2354 788 股 @66.3，3231 501 股 @190.5，毛成交總額 147,684.90。缺費稅／淨收款，原始未知欄保持空白，未寫入 actual_fills；但**已確認成交必須立即接回股數**。`scripts/owner_account.py` 以 Decimal、FIFO 重播原成交簿，再聚合同委託碎筆與已回報平倉，2354、3231 股數歸零；3055 券賣及券買獨立對沖歸零，價差 -70,000。原站展示費用模型每委託取整：手續費 0.1425%、賣出稅 0.3%，本批短單費用估算 959，費後試算 -70,959（另加未知借券費）；不能宣稱為券商實收淨損益。原成交簿已實現 51,474.50，加兩筆現賣原實付成本差額、短單價差、減本批費用估算，帳戶累積已實現試算 -14,322.60。未入帳股息、成本調整或借券費不補造。精確費用補齊後再 reconcile，避免 raw fill/report 重複。

## 卡片與績效日期分離

目前最新卡 9/30，合併遠端官方行情後帳戶估值 9/30，Owner 庫存快照仍 9/17，10/1 新成交的股數已接回帳戶，目前追蹤多頭 9 檔；精確淨收付仍待費稅核對。`shared_comparison_date` 對四策略的 actual/card 日期取交集最大值；無共同日期明確 InputError，禁止 forward fill、補假值。比較表的日期用 COMPARISON_ASOF；理論卡用 THEORY_ASOF。implementation bridge 只讀共同日或以前的卡片，不能用未來表頭對舊帳算 gap。最新卡片收盤是截圖來源資料，不自動寫進官方 price_history。

首頁與已實現頁都有 `owner-account` 當前帳戶區塊，顯示三檔平倉、價差／費後試算與當前股數；舊快照 KPI 收合並明標 9/17，不能與 9/30 曲線或 10/1 帳戶混算。`build_prep.latest_holdings` 在原始快照上套用已確認平倉，備戰／持股體檢／跟盤表同步移除已售部位。歷史券商現值含融券權益，直接核對來源 current_value，不使用多頭 shares*price 公式；舊 liquidation_summary 曾把 3055 高估 28,272，已修復。歷史成交對帳 PASS 僅適用已收錄成交；9/30 估值使用該成交簿與官方價格，不能說 10/1 淨績效已完成。未收到完整庫存與行情，不改頁面估值日。舊 Excel 主檔尚未更新，本次正典更新是 CSV。

## 建置、驗證、發布

在主根執行；須通過完整 tests，再以公開 HTTP 回讀驗證：

```powershell
.venv/Scripts/python.exe Quant_Card/build_cards_csv.py
.venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/build_dashboard.py
.venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/build_prep.py
.venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/build_positions.py
.venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/build_watch.py
.venv/Scripts/python.exe -m pytest -q 66.performance_accumulation_dashboard/tests
git -C 66.performance_accumulation_dashboard diff --check
git -C 66.performance_accumulation_dashboard status --short
```

檢查最新卡日期、括號列／顏色、表頭、9/30 2354 出、2316 明開進且 effective 10/1；檢查 fee/tax/net 仍空、已有成交未重複計入、預期圖片數與 hash。build_receipt 記錄電腦生成時間，來源各自有 asof。不要跑舊 export_latest_signals 覆寫 dashboard latest。

只有 Owner 已授權公開網站更新才在此獨立 repo commit/push。使用明確檔案清單，禁止 `git add .`；不得提交其他 AI 的 notebook、憑證、主實驗室檔案。dirty files 先辨識來源；本次起始 price_history 的 9/18 本地快取及 9/18–23 history 是既有已記錄同一儀表板資料；合併遠端 main 的9次官方行情更新，價格按 date/code 去重，相同 key 以遠端官方源為準（50列來源重疊），其餘 notebook 排除。主 repo 不提交。

Pages 工作流必須複製所有頁面子目錄；本次補 intake、positions、history、watch（後三項原本有頁面卻未複製）。push 後看 GitHub Actions 的 Pages 結果，再實際 HTTP GET 首頁及 intake，驗證最新來源卡、147,684.90、蔚華科價差 -70,000／費後試算 -70,959、三檔歸零及關鍵連結；只看到 push 成功還不算上線成功。

Python 與 Git 的網路讀取只用公開行情／GitHub。任何 AI 永不下單；不登入券商、不讀憑證、不發布帳號。

發布環境注意：本機 repo 未設定 Git author，命令使用 `git -c user.name=Codex -c user.email=codex@users.noreply.github.com ...` 為 AI 變更署名，不改全域 Git 身分。

## 修正驗收：2026-10-01

`tests/test_owner_account.py` 驗證碎筆聚合、原始買入實付、短單方向、未知費稅不阻止歸零、超賣拒絕、共用持股頁移除三檔。`tests/test_dashboard.py` 再驗證公開主頁內容與融券快照現值不被當多頭。費後模型是假設試算，不能以測試 PASS 宣称券商費用對帳完成。原 Claude `actual_fills.csv`、`broker_realized.csv`、dated holdings/summary 與舊 frozen history 保留不變；新產物 `output/owner_account_receipt.json` 記生成時間、回報 asof、已結算成交簿截止與每檔行情日。每日官方收盤工作流已同步重建主頁、已實現、備戰、持股體檢及跟盤表。

融券部位也不得放入四策略多頭的成本差額表：缺少多頭 BUY 並不代表融券帳面成本差 -147,000。`cost_basis_gap_rows` 僅比較 in_strategy_scope 多頭快照。
