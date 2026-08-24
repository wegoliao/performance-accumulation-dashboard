# DeepSeek 每日訊號流水線 SOP（接手包）

> 給 DeepSeek（DSH）或其他 AI 接手「四策略每日卡 → Excel → 公開儀表板」的標準作業程序。
> 正典根：`D:\Quant_Grill_Lab`。治理契約見專案 `AGENTS.md` 與 `.planning/GRILL_DECISIONS.md`（G027）。
> 這是例行 SOP；儀表板架構面的精進清單見 [Claude 版精進盤點](claude/)，兩者分工：Claude 修儀表板斷點，本文件固化「訊號入庫」段。

## 1. 分工地圖（兩個 repo）

| Repo | 路徑 | 管什麼 |
|---|---|---|
| 主 repo（不公開） | `D:\Quant_Grill_Lab` | 訊號正典 `data/strategy_signals.tsv`、表頭對帳 `data/strategy_sleeve_header.tsv`、閘門與匯出腳本 `scripts/` |
| 公開 repo | `D:\Quant_Grill_Lab\66.performance_accumulation_dashboard` | 儀表板建置、公開 GitHub Pages、Excel 主檔 |

- 唯一 Python：`D:\Quant_Grill_Lab\.venv\Scripts\python.exe`（3.10，專案正典）。
- 不得使用或依賴 `D:\WEGO_ALPHA_FACTORY`；不得讀取憑證、登入券商、傳送/修改/取消委託。
- 主 repo 依 AGENTS.md 不自動 commit/push；公開 repo 需 Owner 授權後才 push。

## 2. 資料契約（餵資料的標準格式）

### 2.1 訊號正典 `data/strategy_signals.tsv`

欄位（tab 分隔）：`日期 / 策略 / 代號 / 名稱 / 產業 / 進場價 / 收盤價 / 幅度 / 方向 / 訊號`

- 策略：`投信 / YOY / 融資 / 突破` 四種。
- 方向：`+` = 畫面上紅字（獲利）、`-` = 綠字（虧損）；`幅度` 欄**不帶正負號**。
- 訊號：`進 / 抱 / 出`。進場價寫 `明開` 或 `明收` 表示隔日以該價位進場。
- 逐日 append，不覆寫歷史；缺圖的日期就缺值，**不得用前值或行情補造**。

### 2.2 表頭對帳 `data/strategy_sleeve_header.tsv`

欄位：`日期 / 策略 / 表頭報酬`。表頭是當日持有成分的等權平均，是免費 checksum。

### 2.3 儀表板輸入 `66.performance_accumulation_dashboard/inputs/latest_strategy_signals.csv`

**不要手改**。由 `python scripts/export_latest_signals.py` 從 TSV 自動產出：
- `strategy_id`：投信→`TRUST`、YOY→`YOY`、融資→`MARGIN`、突破→`BREAKOUT`。
- `effective_date`：進/出列 = 來源日的下一交易日（週一至五推算；台灣交易所休假日未建模，以來源卡為準）。
- `quality_note`：`SOURCE_CHECKSUM_MISMATCH`（該日策略在閘門 KNOWN_GAPS 待補清單）與 `PLANNED_SIGNAL_NOT_ACTUAL_FILL`（進/出計畫訊號，等待真實成交）。

### 2.4 成交正典 `66.performance_accumulation_dashboard/inputs/actual_fills.csv`

**只收真實成交**：成交日、成交價、股數、手續費、交易稅。計畫訊號在 Owner 回報成交前不得寫入。

## 3. 每日流程 A：收到新的四張策略卡（訊號日 D 收盤後）

每步都要能重跑；每批訊號在 commit message 記錄**程式時間 vs 來源 data_asof**。

1. **收圖**：Owner 貼 4 張策略卡（投信／YOY／融資／突破），逐列核對紅綠方向與表頭數字。
2. **抄錄**：append 到 `data/strategy_signals.tsv` 與 `data/strategy_sleeve_header.tsv`。
3. **閘門**（新日 FAIL 必修好才繼續；backlog 不擋但每次列印）：
   ```powershell
   D:\Quant_Grill_Lab\.venv\Scripts\python.exe D:\Quant_Grill_Lab\scripts\check_strategy_signals.py <D>
   ```
   常見 FAIL = 某一格紅綠看反、漏抄/多抄一列、幅度欄誤帶負號。
4. **匯出**（自動產出 latest CSV 並驗證逐字節一致）：
   ```powershell
   D:\Quant_Grill_Lab\.venv\Scripts\python.exe D:\Quant_Grill_Lab\scripts\export_latest_signals.py
   D:\Quant_Grill_Lab\.venv\Scripts\python.exe D:\Quant_Grill_Lab\scripts\export_latest_signals.py --check
   ```
5. **重建儀表板＋測試**：
   ```powershell
   cd D:\Quant_Grill_Lab\66.performance_accumulation_dashboard
   ..\..\.venv\Scripts\python.exe scripts\build_dashboard.py
   ..\..\.venv\Scripts\python.exe -m pytest -q tests
   ```
6. **發布**（Owner 授權後）：`git add` 只加本次實際變更的檔案 → commit → push，然後用 Node fetch 驗證公開頁關鍵字（個股代號、`最新四策略卡 · <D> 收盤`、`<D+1> 計畫進出`）：
   ```powershell
   node -e "fetch('https://wegoliao.github.io/performance-accumulation-dashboard/').then(r=>r.text()).then(t=>console.log(t.includes('2637'),t.includes('最新四策略卡')))"
   ```
7. **成交回填（D+1 收盤後）**：Owner 回報 2637／2646 等實際成交 → append `actual_fills.csv` → 重跑 4–6。計畫訊號列在回填後自然退出 latest CSV。

## 4. 每日流程 B：只有官方收盤、無新卡

GitHub Actions `daily-close.yml` 自動抓 TWSE／TPEx 官方收盤並延長曲線。若 bot commit 未觸發 Pages 部署（已知斷點，見 Claude 稽核 #02），手動 `workflow_dispatch` 或人工 push。

## 5. 硬邊界（每次都生效）

- 不修改、不依賴 `D:\WEGO_ALPHA_FACTORY`。
- 不讀憑證、不登入券商、不傳送/修改/取消任何委託；AI 任何輸出都不構成下單。
- 不把庫存快照倒推成歷史績效；樣本 < 20 筆日報酬就不硬算 Sharpe 等風險統計。
- 畫面表頭與成分股平均對不上時（例如 8/24 YOY +3.7% vs +4.33%），兩邊原樣保留並標 `SOURCE_CHECKSUM_MISMATCH`，**不修改任何一格**。
- 缺來源圖的日期保持缺值（目前 8/21 缺卡）。

## 6. 已知待補（收圖優先序）

閘門 `KNOWN_GAPS`（`scripts/check_strategy_signals.py`）目前 6 筆：
8/10 YOY、8/10 融資、8/14 投信、8/17 投信、8/18 投信（皆缺原圖成分股）、**8/24 YOY（來源表頭 +3.7% vs 可見平均 +4.33%，等來源端澄清）**。
Owner 補圖或來源端更正後：重抄 → 閘門轉 OK → 從 `KNOWN_GAPS` 刪掉那一行。

## 7. 疑難排解

| 現象 | 原因 | 動作 |
|---|---|---|
| 閘門新日 FAIL | 紅綠看反／漏抄列 | 回到原圖逐列重核，勿改表頭 |
| `export --check` FAIL | TSV 與 latest CSV 漂移 | 直接跑 export（無 --check）以 TSV 覆寫 CSV |
| build 失敗 `takes 4 positional arguments but 5 were given` | 另一 AI 正在同檔編輯 | 等對方 commit 靜止後再重建 |
| 公開頁沒更新 | bot push 不觸發 Pages（#02） | 人工 push 或 workflow_dispatch |
| 8/25 訊號還在「等待實際成交」 | 沒有 Owner 成交回報 | 正確行為；不得自行補成交 |

## 8. 未收編的自動化缺口

- Excel 主檔 `inputs/four_strategy_daily_signals.xlsx` 的產製工具尚未定位/標準化（歷史註記），目前不在每日自動鏈。
- `build_dashboard.py` 的 snapshots 路徑（`holdings_snapshot_2026-08-24.csv`）與 2886 未歸屬判斷仍寫死檔名，見 Claude 稽核 #06。
