# 公開 GitHub：Owner、團隊與朋友觀看方式

## Owner

`wegoliao` 是 repo owner。公開網站與 repo 不需登入即可觀看；修改檔案與執行管理仍需要 GitHub 權限。

## 團隊成員需要什麼

只觀看不需要 GitHub 帳號。只有需要修改資料或程式的人，才需 GitHub 帳號並由 Owner 邀請為 collaborator。

## 怎麼看

### 最快：直接看公開網站

<https://wegoliao.github.io/performance-accumulation-dashboard/>

### GitHub 頁面看摘要

開啟 `DASHBOARD_SUMMARY.md`，可直接看到最新資產、日／週／月／季／年／YTD／累計狀態。

### 下載完整 HTML

1. 進入 `Actions`。
2. 打開最新成功的 `Daily official close and dashboard`。
3. 在 `Artifacts` 下載 `performance-dashboard-*`。
4. 解壓縮並雙擊 `index.html`。

也可以使用 `Code → Download ZIP`，解壓縮後開啟根目錄的 `index.html`。

### Git clone

```powershell
git clone https://github.com/wegoliao/performance-accumulation-dashboard.git
cd performance-accumulation-dashboard
.\run_dashboard.ps1
```

## 隱私提醒

- Repo 與 Pages 已公開，任何人都可查看或下載。
- 頁面含真實股票、股數、成本、現值及損益。
- `SIMULATED_MTM` 是固定目前股數的歷史回看，不是實際帳戶歷史績效。
