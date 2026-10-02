# 四策略卡、成交對帳與 GitHub 更新 SOP

目前版本：2026-10-02。唯一根目錄 D:/Quant_Grill_Lab；遵守根 AGENTS.md、Knowl 查詢、文件路由。獨立 Git repo 是 66.performance_accumulation_dashboard。Owner 已授權本次公開網站更新；不提交主專案、不加入其他 AI notebook，不登入券商或下單。

## 正典與日期

| 資料 | 位置 | 規則 |
|---|---|---|
| 8/10–8/24 歷史卡 | 主專案 data/strategy_signals.tsv、strategy_sleeve_header.tsv | 不覆寫8/25後 |
| 8/25後逐列卡 | inputs/signal_history.csv | 日期／策略／股票去重 |
| 最新卡 | inputs/latest_strategy_signals.csv | 最新來源日所有列 |
| 原卡表頭 | inputs/strategy_card_returns.csv | 不是帳戶報酬 |
| 完整券商成交 | inputs/broker_statement_fills.csv | 59筆物理交易、實際費稅收付、已去識別 |
| 原檔檢核 | inputs/broker_statement_receipt.json | SHA、來源合計、來源日期及電腦時間 |
| 券商績效正典 | inputs/broker_pnl_snapshot.json 及其指向的兩份報表 CSV | Owner 10/2 指定券商已實現／未實現画面，優先作主指標 |
| 四策略成交 | inputs/actual_fills.csv | 58列、原Claude策略分配、實際淨收付；不含融券 |
| Owner舊貼文 | inputs/owner_reported_fills_*.csv | 歷史證據，與statement同單不能疊加 |
| Owner本次選擇 | inputs/owner_preferences.csv | 特定卡日偏好，不是成交或永久交易規則 |
| 官方行情 | inputs/price_history.csv | TWSE／TPEx；不得以卡片取代 |
| 舊券商損益、庫存 | broker_realized.csv、dated snapshot | 歷史不同成本口徑；不能當目前帳戶 |

來源日期與生成時間分開。卡可以更新較快，actual/card比較用所有四策略日期交集最大日；無交集拒絕，不能前填。9/17快照及frozen history保持原始日期。

## 收圖

1. Quant_Card根目錄是收件匣；讀圖中日期而非檔名。一圖可能多策略／多日期。記SHA及逐列代碼、名稱、產業、進場、收盤、印出報酬、訊號、表頭。
2. 紅字正、綠字負、灰字零；訊號顏色不是報酬方向。括號反向口徑不按多頭價格公式改寫；括號星號及截斷產業保留。印出平均與表頭不一致標來源差，不改數字湊一致。
3. 去重及修改留before/after；進／出填下一真實交易日。weekday helper不含休市，需核實交易日。更新history／header，再產生latest。
4. 主.venv執行 Quant_Card/build_cards_csv.py 產生cards.csv、card_headers.csv、latest.csv；不要跑舊export覆寫正典。
5. build、tests及逐圖核對後移processed，SHA必須不變。10/2批次：2圖、4卡31列，來源日10/1。

Quant_Card/update_20261002.py 是固定本批重播；舊 import_20261001/finalize_20261001會寫固定日期文字，不可每日通用。證據在本批evidence_20261002，不能重跑改旧收據。

## 對帳

scripts/statement_account.py用標準庫讀cp950 HTML，白名單移除帳號與姓名。原HTML只留Owner本機，不能放公開repo。匯入先核每筆現金等式及來源合計，拒絕重複、未知方向、負數及未支援融資。

    .venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/statement_account.py 'C:/Users/Wego/Downloads/3102.xls.html'

本statement數量已是股；舊委託表可能是張。没有成交時間留空，委託時間不能冒充成交時間。券商價金是整數截斷，不能拿價乘股數取代淨交割。現賣減股數；券賣開空、券買回補。

多頭FIFO實現＝淨收款−匹配買入實付；未平倉成本含實際買入費用。融券核對保證金、擔保品及完整收付；3055實付147000、回補實收75948，損失71052。利息24是加回；借券費117已確認。保證金／擔保品重複列示不能當收入。完整數字與獨立現金橋接見 STATEMENT_RECONCILIATION_2026-10-01.md。

reconcile_strategy_fills(root)按日期／委託原號／股票／方向／價對齐historical fills，更新費稅收付並沿用策略。跨策略同價成交按股數拆到分，尾列補餘差。保存before/after；新增平倉沿用可確定原買策略。56→58列与物理59筆是不同粒度，不能硬要求行數相等。

完整statement存在時，owner_account優先呼叫statement_account，不再疊加先前貼文。來源日以後有新成交時必須補來源，不能用舊statement暗示已涵蓋。無完整statement時的舊fallback可用已確定成交減股數，但未知費用要明示未知，不能估值冒充實收。

原 statement_reference 中2886僅1股賣出淨收50、無原買成本，收付包含但不補零成本。Owner 新提供損益畫面有買入40、賣出50、損益10，且明確要求此測試買賣不計；因此主績效排除整筆，而不是減50元。原始現金來源仍保存。未提供股息與成本調整不臆造。

## 最新券商損益畫面（10/2 主指標正典）

本次兩圖安全轉錄為 broker_realized_report_2026-10-02.csv（22列，1列測試單排除）及 broker_positions_report_2026-10-02.csv（9列）。原圖含姓名帳號，只存 Quant_Card/processed；GitHub 只發布白名單 CSV、來源hash與收據，不發布原圖。

- 已實現畫面小計−9551，扣除測試獲利10後 **−9561**。現股已實現61491，融券−71052。
- 目前持股成本675866、畫面現值674499，未實現試算 **−1367**；與已實現合計 **−10928**。畫面「損益不含稅費」未勾選，這是含預估賣出費稅的試算，不能再扣一次。
- 報價來源為10/2盤中截圖，精確報價時間未提供。不能塞進官方 price_history.csv，也不能稱官方收盤或即時連線。每日行情自動更新只刷新另標日期的技術量測與四策略曲線；券商快照等待新的來源圖。
- 已實現圖成交年份印2025，完整HTML印2026。按唯一委託單號／股票／股數／價格／淨收付核對21筆，但保留兩來源日期與UNRESOLVED年份差異，不能默默改年。查詢起2025-08-01、止2026-10-02。
- 券商已實現配對成本與成交FIFO成本有4857的損益差；逐筆來源差在主頁折疊區，原因未提供，不推測配息，不把差額寫成現金收入。
- scripts/broker_pnl_snapshot.py套用畫面到owner_account；獨立 statement_account 數字與現金橋留在 statement_reference，所有既有成交／四策略分配不改。公開 account_reconciliation.json 分開保存兩個口徑。
- 每筆損益等式、兩圖小計、股票／股數／買入成本覆蓋及賣出淨收款都需驗證。新實際成交晚於画面涵蓋日期或無法完整配對就fail closed，不靜默把舊券商快照稱新績效。

驗證 tests/test_broker_pnl_snapshot.py 與完整tests。安全轉錄器 Quant_Card/import_broker_pnl_20261002.py 只重播本批已核對資料，不是通用OCR；不要每日重跑或改舊receipt電腦時間。明細/收據/intake與主頁同步發布；不把 screenshot 淨估值傳成另一種gross官方市值。

## 建置、驗證、發布

先fetch與fast-forward乾淨tracked tree，保留其他AI未提交檔。官方行情取既有fetch_prices.py或遠端自動更新。所有持股用同日官方價格且不早於最新成交；缺行情就fail closed，不用舊價掩飾。

    .venv/Scripts/python.exe Quant_Card/build_cards_csv.py
    .venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/build_dashboard.py
    .venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/build_prep.py
    .venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/build_positions.py
    .venv/Scripts/python.exe 66.performance_accumulation_dashboard/scripts/build_watch.py
    .venv/Scripts/python.exe -m pytest -q 66.performance_accumulation_dashboard/tests
    git -C 66.performance_accumulation_dashboard diff --check
    git -C 66.performance_accumulation_dashboard status --short

build_dashboard也重建intake安全下載。共用持股helper重建股數、成本和官方市值供子頁，不改原快照。test_statement_account golden amounts、現金橋接、未知成本、利息方向、CSV投影、超賣／重複拒絕及逐欄來源都是驗收。未實現價格會刷新，測試應驗不變量而非鎖每日行情。

發布前所有新公開檔檢查無原帳號姓名。只明確清單add，禁止git add .；不提交主repo或其他AI notebook。沒有Git author時可單命令使用 git -c user.name=Codex -c user.email=codex@users.noreply.github.com，不改全域。只有Owner授權才push此独立repo。

Pages需包含main、realized、prep、positions、watch、intake及historical子頁。push後看Actions成功，再HTTP回讀全部現行頁、新CSV与receipt，驗主3數字、最新日期、71052、9檔與投信偏好。只push成功不等於上線；公開圖及收據存主專案本批evidence。

## 歷史

10/1缺費用時曾發布短單試算70959與累積試算-14322.60；已由本版實際費稅取代，不能重新加進績效。舊批次／frozen history保持歷史。舊Excel未更新，頁面標舊資料，CSV是本次正典。理論卡、同日OHLC、收盤價、可變現估費都不是實際成交。
## 2026-10-02：累積曲線口徑修正

頁首券商損益是帳戶績效；四策略現股成交重建採每策略設定50萬預算，排除帳戶融券，不能稱完整帳戶NAV。現持股虧損與先前平倉獲利須分列，不能只看曲線正負。

卡片表頭採原始百分比（圖內100＋表頭，座標／tooltip顯示表頭百分比），不重新定基、不複利。換股及不同進場日期使它不能當累積策略報酬，也不能直接與benchmark比輸贏。

真正理論累積圖使用既有 `inputs/strategy_nav.csv`，四策略須至少兩個共同日期；缺少來源淨值顯示 `WAITING_STRATEGY_NAV`。淨值須已納入歷次平倉、持股、現金與成本，外部入出金須已消除其影響；`note` 說明策略版本、權重、多空、成交時点、費用及股息處理。禁止用 `strategy_card_returns.csv` 補成NAV。

查核與缺口見 [RETURN_BASIS_AUDIT_2026-10-02.md](RETURN_BASIS_AUDIT_2026-10-02.md)。新增 `tests/test_return_basis.py` 驗卡片原負報酬、缺NAV拒畫、換股後平倉利益仍累積及共同期間benchmark。主專案的 `Quant_Card/audit_return_basis_20261002.py` 重播來源hash與金額。
