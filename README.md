# 66 · 績效累積圖

這是一條自包含、唯讀、無券商登入／下單能力的績效觀察支線。

![績效累積圖預覽](output/dashboard_full.png)

公開網站：<https://wegoliao.github.io/performance-accumulation-dashboard/>。任何人都能直接觀看，不需要 GitHub 帳號；使用方式見 [`TEAM_ACCESS.md`](TEAM_ACCESS.md)。

## 目前能看到什麼

- 2026-08-24 使用者貼入的 15 檔現股盤中庫存快照。
- 投信、YOY、融資、突破四策略的逐筆實際成交歸屬、實際累積曲線與可變現損益。
- 共同截止日 2026-08-20 的「實際 vs 理論卡」差異；理論來源未更新到 8/24，所以不假造同日差異。
- 現值、付出成本、累積未實現損益及報酬率。
- 個股累積損益、當日價格變動估算貢獻、配置與集中度。
- 日／週／月／季／年／YTD／累計報酬卡，以及週、月、季、年歷史績效。
- 累積曲線、回撤、月度熱圖、風險報酬散點、貢獻瀑布、報酬分布與 Beta／相關性。
- 每個數字的來源日期、計算狀態與缺資料原因。
- 每日 NAV／外部現金流／benchmark 到齊後，自動計算 CAGR、波動度、Sharpe、Sortino、MDD、Calmar、Alpha、Beta、Information Ratio 與 Tracking Error。

目前實際曲線已用 `20260820庫存表.xlsx` 的成交日、成交價、股數、手續費與交易稅重建。每個策略 sleeve 以 NT$50 萬起始，每日用現金加可變現價值計算；不再把 2026-08-24 持股倒推至一年前冒充實際績效。目前只有 10 筆實際日報酬，MDD 可顯示，Sharpe、Sortino、Alpha、Beta、Information Ratio 與 Tracking Error 在滿 20 筆前保留 `N/A`。

GitHub 每日收盤會以最新官方收盤價延長四策略曲線；最後一筆成交後的股數會明確以 `CARRY_FORWARD_POSITIONS` 估值。若 Owner 有新買賣，必須先更新 `inputs/actual_fills.csv`，否則頁面不會自行推測部位變化。

## 一鍵重算

在檔案總管雙擊：

```text
run_dashboard.bat
```

或在 PowerShell 執行：

```powershell
.\run_dashboard.ps1
```

重算成功後會開啟：

```text
index.html
```

只重算、不開瀏覽器：

```powershell
.\run_dashboard.ps1 -NoOpen
```

## 輸入檔案

| 檔案 | 用途 | 目前狀態 |
|---|---|---|
| `inputs/holdings_snapshot_2026-08-24.csv` | 今日庫存快照 | 已匯入 |
| `inputs/snapshot_summary_2026-08-24.csv` | 券商畫面小計，用於交叉驗證 | 已匯入 |
| `inputs/account_nav.csv` | 每日整戶／策略 sleeve NAV 與外部現金流 | 等待資料 |
| `inputs/actual_fills.csv` | 四策略逐筆實際成交與費稅 | 已匯入 22 筆 |
| `inputs/strategy_card_returns.csv` | owner 策略卡等權顯示報酬 | 已匯入至 2026-08-20 |
| `inputs/strategy_position_marks.csv` | 已出場股在持有期的補充收盤 mark | 已匯入 3702 |
| `inputs/strategy_nav.csv` | 可投資理論 equity curve | 尚未提供；不用策略卡百分比冒充 |
| `inputs/benchmark_nav.csv` | 0050／加權指數 benchmark | 官方資料逐日累積 |
| `inputs/price_history.csv` | 個股與 benchmark 官方收盤歷史 | 官方資料逐日累積 |
| `inputs/positions_ledger.csv` | 舊版個股風險特徵輔助檔 | 不再用於實際績效 |

`account_nav.csv` 的 `external_cash_flow_twd` 採「存入為正、提出為負」，TWR 日報酬使用：

```text
(本日 portfolio_value_twd - 本日 external_cash_flow_twd) / 前日 portfolio_value_twd - 1
```

這是假設外部現金流在本日估值前發生。正式版仍須由 owner 確認現金流時點與績效歸因口徑。

## 數字口徑

- 年化基礎：252 個交易日。
- Sharpe／Alpha：risk-free rate 暫定 0，畫面會顯示此口徑。
- CAGR：實際日曆日／365.25。
- Alpha：日報酬 OLS intercept × 252。
- Beta：日報酬相對 benchmark 的 OLS slope。
- MDD：每日 equity curve 相對歷史高點的最大跌幅。
- 至少 20 筆日報酬才顯示 Sharpe、Sortino、Alpha、Beta、IR 與 Tracking Error；較短序列不冒充穩定風險統計。
- 「今日價格變動估算貢獻」為 `股數 × 畫面漲跌`，未納入今天可能產生的費稅、盤中買賣與現金部位，不能當成正式單日帳戶報酬。

## 第二版邊界

後續可以加入：

1. GitHub Actions 每日收盤重建、更新公開 GitHub Pages，並保留可下載的 HTML artifact。
2. 公開行情或永豐 Shioaji 唯讀行情／Kbars provider。
3. 依 `strategy_id` 將策略理論、實際成交與 benchmark 對齊。
4. 股息、入出金、部分成交、費稅與已實現損益。

第二版仍不得保存憑證、登入 REAL、送單、改單或刪單。行情更新與交易授權是兩件不同的事。

## 官方公開行情來源

- TWSE OpenAPI `STOCK_DAY_ALL`：上市個股日成交／收盤資訊。
- TPEx OpenAPI `tpex_mainboard_daily_close_quotes`：上櫃股票行情。
- 每日更新預定台北時間 16:30 執行；若官方資料尚未發布、休市或日期不一致，應保持 `NO_NEW_CLOSE`／`MARKET_DATE_MISMATCH`，不得拿舊價冒充今天收盤。

本 repo 與 GitHub Pages 已依 Owner 決策公開；股票、股數、成本、現值與損益都可被任何人讀取。完整視覺直接開啟 <https://wegoliao.github.io/performance-accumulation-dashboard/>。
