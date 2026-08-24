# 其他 AI 接手說明

## 正典與邊界

- 唯一可寫根：`D:\Quant_Grill_Lab\66.performance_accumulation_dashboard`
- 不得修改或依賴 `D:\WEGO_ALPHA_FACTORY`。
- 不得讀取 credentials、登入券商或實際傳送／修改／取消委託。
- 不得把單一庫存快照拿去算 CAGR、Sharpe、Alpha、Beta 或 MDD。

## 已完成契約

- `inputs/holdings_snapshot_2026-08-24.csv`：owner 貼入的 15 檔現股盤中快照。
- `inputs/snapshot_summary_2026-08-24.csv`：原畫面小計，用於 totals reconciliation。
- `scripts/build_dashboard.py`：只用 Python standard library 建立離線 HTML。
- `run_dashboard.ps1`／`run_dashboard.bat`：地端重算入口。
- `tests/test_dashboard.py`：輸入 totals、估算今日貢獻、TWR 與 HTML 狀態測試。

## 待做但尚未獲得 owner 決策

1. 實際績效使用策略歸因 sleeve 或整戶口徑。
2. benchmark 使用 0050、加權報酬指數，或兩者並列。
3. 外部現金流發生時點與股息處理。
4. GitHub repo／Pages 位置及收盤／盤中更新頻率。
5. 永豐唯讀資料 provider；任何連線都必須保留 no-login/no-order 測試。

## 接手時先執行

```powershell
..\.venv\Scripts\python.exe -m pytest -q tests\test_dashboard.py
.\run_dashboard.ps1 -NoOpen
```

> **每日訊號流水線（收圖→閘門→匯出→儀表板→發布）請一律照
> [DEEPSEEK_RUNBOOK.md](DEEPSEEK_RUNBOOK.md) 執行**。訊號正典是主 repo 的
> `..\data\strategy_signals.tsv`；`inputs/latest_strategy_signals.csv` 由
> `..\scripts\export_latest_signals.py` 自動產出，不得手改。
