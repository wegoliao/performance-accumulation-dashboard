# 私人 GitHub：Owner 與團隊觀看方式

## Owner

`wegoliao` 是 repo owner，登入 GitHub 後可直接查看全部檔案、每日 workflow、摘要與 artifacts。

## 團隊成員需要什麼

每位成員只需要：

1. 一個 GitHub 帳號。
2. 把 GitHub username 提供給 owner。
3. 接受 private repository collaborator 邀請。

Owner 邀請路徑：

```text
Repository → Settings → Collaborators → Add people
```

建議權限先給 `Read`；只有需要修改輸入資料的人才給 `Write`。

## 怎麼看

### 最快：GitHub 頁面直接看摘要

開啟 `DASHBOARD_SUMMARY.md`，可直接看到最新資產、日／週／月／季／年／YTD／累計狀態。

### 完整視覺 HTML

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

- Repo 為 private 不代表可以把下載後的 HTML 公開轉傳。
- 頁面含真實股票、股數、成本及損益。
- 團隊成員離開時，Owner 應立即從 Collaborators 移除。
- 未另行確認前，不啟用 public GitHub Pages。
