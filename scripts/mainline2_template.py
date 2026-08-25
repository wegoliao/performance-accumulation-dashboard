"""HTML shell for the mainline-2 tracker.

Kept apart from the builder on purpose: the builder computes, the template
renders, and neither has to be read to understand the other. Every visible
number arrives through a ``{{PLACEHOLDER}}``.
"""

TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>主線二 · 未持有訊號追蹤</title>
<meta name="description" content="四策略卡上有訊號、但整戶零部位的個股：分價分布、進場落點量表與容量。純模擬追蹤。">
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --raise:#1c2430; --ink:#e6edf3; --muted:#8b949e;
  --line:#26303b; --accent:#58a6ff; --green:#3fb950; --red:#f85149; --gold:#d29922;
  --va:rgba(88,166,255,.14); --bin:#39485a; --poc:#d29922;
}
:root[data-theme="light"]{
  --bg:#f6f8fa; --panel:#ffffff; --raise:#f0f3f6; --ink:#1f2328; --muted:#59636e;
  --line:#d8dee4; --accent:#0969da; --green:#1a7f37; --red:#cf222e; --gold:#9a6700;
  --va:rgba(9,105,218,.12); --bin:#c2cbd6; --poc:#9a6700;
}
@media (prefers-color-scheme: light){
  :root:not([data-theme="dark"]){
    --bg:#f6f8fa; --panel:#ffffff; --raise:#f0f3f6; --ink:#1f2328; --muted:#59636e;
    --line:#d8dee4; --accent:#0969da; --green:#1a7f37; --red:#cf222e; --gold:#9a6700;
    --va:rgba(9,105,218,.12); --bin:#c2cbd6; --poc:#9a6700;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.6;
  font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,-apple-system,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:30px 20px 80px}
a{color:var(--accent)}
.eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:700}
h1{font-size:clamp(27px,4.2vw,40px);margin:8px 0 10px;letter-spacing:-.02em;line-height:1.15}
.lede{color:var(--muted);max-width:66ch;margin:0}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:14px;font-size:13px;color:var(--muted)}
.meta code{background:var(--raise);padding:1px 6px;border-radius:4px;font-size:12px}
.notice{margin:22px 0 30px;border:1px solid var(--gold);border-left-width:3px;border-radius:10px;
  background:rgba(210,153,34,.08);padding:14px 18px;font-size:14px}
.notice b{color:var(--gold)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:16px}
.panel h2{font-size:19px;margin:0 0 4px;letter-spacing:-.01em}
.panel .sub{color:var(--muted);font-size:13.5px;margin:0 0 18px;max-width:78ch}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 17px}
.card .v{font-size:25px;font-weight:750;letter-spacing:-.02em;line-height:1.2}
.card .k{font-size:12.5px;color:var(--muted);margin-top:5px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
.tab{appearance:none;border:1px solid var(--line);background:var(--raise);color:var(--muted);
  border-radius:99px;padding:8px 16px;font-size:13.5px;cursor:pointer;font-family:inherit;font-weight:600}
.tab[aria-selected="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
.view[hidden]{display:none}
.view-note{font-size:13px;color:var(--muted);margin:0 0 16px;padding-left:11px;border-left:2px solid var(--line)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-bottom:14px}
.legend i{display:inline-block;width:15px;height:3px;vertical-align:middle;margin-right:6px;border-radius:2px}
.legend .sw{display:inline-block;width:15px;height:10px;vertical-align:middle;margin-right:6px;border-radius:2px}
.ladders{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.ladder-card{margin:0;background:var(--raise);border:1px solid var(--line);border-radius:12px;padding:13px}
.ladder-card figcaption{display:flex;justify-content:space-between;align-items:baseline;
  font-size:13.5px;margin-bottom:9px;gap:8px}
.ladder{width:100%;height:230px;display:block}
.ladder-meta{display:flex;justify-content:space-between;font-size:11.5px;color:var(--muted);margin-top:8px;gap:8px}
.bin{fill:var(--bin)} .bin.poc{fill:var(--poc)} .va{fill:var(--va)}
.poc-line{stroke:var(--poc);stroke-width:1.4;stroke-dasharray:4 3}
.now-line{stroke:var(--green);stroke-width:1.6}
.entry-line{stroke:var(--accent);stroke-width:1.4;stroke-dasharray:2 3}
.tag{font-size:9px;font-family:ui-monospace,Consolas,monospace}
.poc-line-t{fill:var(--poc)} .now-line-t{fill:var(--green)} .entry-line-t{fill:var(--accent)}
.rail-row{display:grid;grid-template-columns:150px 62px 1fr 62px 48px;gap:10px;align-items:center;
  padding:9px 0;border-bottom:1px solid var(--line);font-size:13px}
.rail-row:last-child{border-bottom:none}
.rail-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rail-lo,.rail-hi{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.rail-hi{text-align:right}
.rail-pct{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
.rail-svg{width:100%;height:38px;display:block}
.rail{stroke:var(--line);stroke-width:2}
.entry-dot{fill:var(--accent)} .now-dot{fill:var(--green);stroke:var(--panel);stroke-width:1.5}
.matrix-row{display:grid;grid-template-columns:150px 1fr 48px;gap:12px;align-items:center;
  padding:7px 0;font-size:13px}
.matrix-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.matrix-pct{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
.matrix-svg{width:100%;height:22px;display:block}
.cell{fill:var(--accent)}
.matrix-axis{display:grid;grid-template-columns:150px 1fr 48px;gap:12px;font-size:11px;color:var(--muted)}
.matrix-axis span:nth-child(2){display:flex;justify-content:space-between}
.landing{width:100%;height:150px;display:block;overflow:visible}
.grid{stroke:var(--line);stroke-width:1}
.axis{fill:var(--muted);font-size:10.5px}
.buy-dot{fill:var(--green);fill-opacity:.75}
.sell-dot{fill:var(--red);fill-opacity:.75}
.mean-line{stroke:var(--gold);stroke-width:2}
.mean-t{fill:var(--gold);font-weight:700}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:640px}
th{text-align:left;color:var(--muted);font-weight:650;font-size:11.5px;letter-spacing:.04em;
  border-bottom:1px solid var(--line);padding:9px 8px;white-space:nowrap}
td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
small{color:var(--muted)}
.positive{color:var(--green)} .negative{color:var(--red)} .neutral{color:var(--muted)}
.muted{color:var(--muted)}
ol.steps{margin:0;padding-left:20px;font-size:14px}
ol.steps li{margin-bottom:9px}
footer{margin-top:40px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}
@media(max-width:820px){
  .rail-row{grid-template-columns:110px 1fr 44px}
  .rail-lo,.rail-hi{display:none}
  .matrix-row,.matrix-axis{grid-template-columns:110px 1fr 44px}
}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="eyebrow">Mainline 2 &middot; paper track</div>
  <h1>主線二：有訊號、沒買的那些</h1>
  <p class="lede">四張策略卡上印出來、但整戶零部位的個股。這條線不配資金、不進 sleeve、不影響任何一個實際績效數字，
  它只回答一件事：這些名字過去半年實際成交在哪些價位，而現在的價格站在那個分布的什麼位置。</p>
  <div class="meta">
    <span>訊號日 <code>{{SIGNAL_ASOF}}</code></span>
    <span>行情截止 <code>{{PRICE_ASOF}}</code></span>
    <span>分價視窗 <code>{{PROFILE_WINDOW}}</code></span>
    <span><a href="../">&larr; 主線一 實際績效</a></span>
    <span><a href="../claude/">稽核盤點</a></span>
  </div>
</header>

<div class="notice">
  <b>這是量測，不是建議。</b>
  分價分布是「過去成交發生在哪裡」的紀錄，不是「未來應該在哪裡買」的預測。本頁不排序推薦、不指定價位、不產生委託。
  POC 與價值區是描述統計名詞，出現在這裡不代表任何一個價格比另一個好。實際要不要買、買多少、什麼價位，是你的決定。
</div>

<section class="cards">
  <div class="card"><div class="v">{{ROSTER_N}}</div><div class="k">主線二檔數：卡上有訊號、整戶零部位</div></div>
  <div class="card"><div class="v">{{HELD_N}}</div><div class="k">主線一檔數：有實際成交與成本</div></div>
  <div class="card"><div class="v">{{BUY_POSITION}}</div><div class="k">你的買進落在當日高低區間的平均位置</div></div>
  <div class="card"><div class="v">{{PROFILE_DAYS}}</div><div class="k">每檔分價所用的交易日數</div></div>
</section>

<article class="panel">
  <h2>主線一 · 你的成交實際落在哪裡</h2>
  <p class="sub">這是本 repo 唯一能對「進場點」說的實證：把每一筆成交價放回當天自己的最高／最低區間。
  0% 是當日最低、100% 是當日最高。買進落點越低越省，賣出落點越高越好。這不預測未來，它衡量的是已經發生的執行品質。</p>
  <div class="legend">
    <span><i style="background:var(--green)"></i>買進成交</span>
    <span><i style="background:var(--red)"></i>賣出成交</span>
    <span><i style="background:var(--gold)"></i>平均落點</span>
  </div>
  {{LANDING_CHART}}
  <div class="table-wrap" style="margin-top:18px">
    <table><thead><tr><th>成交日</th><th>別</th><th>股票</th><th class="num">成交價</th>
    <th class="num">當日區間</th><th class="num">區間位置</th><th class="num">對當日收盤</th></tr></thead>
    <tbody>{{LANDING_TABLE}}</tbody></table>
  </div>
</article>

<article class="panel">
  <h2>主線二 · 三種追蹤介面</h2>
  <p class="sub">同一份資料，三種看法。做法不同是因為要回答的問題不同：看單檔深度、掃全部相對位置、還是找價位群聚。
  三個都留著，用久了你會知道哪一種真的在用。</p>
  <div class="tabs" role="tablist">
    <button class="tab" role="tab" aria-selected="true" data-view="ladder">A · 分價階梯</button>
    <button class="tab" role="tab" aria-selected="false" data-view="rail">B · 進場軌道</button>
    <button class="tab" role="tab" aria-selected="false" data-view="matrix">C · 熱力矩陣</button>
  </div>

  <div class="view" data-view="ladder">
    <p class="view-note">單檔深度。價格在縱軸，橫條長度是該價位累積的成交量。
    看得到量能堆在哪一段、現價離那一段多遠。適合決定「這一檔要不要細看」。</p>
    <div class="legend">
      <span><span class="sw" style="background:var(--bin)"></span>各價位成交量</span>
      <span><span class="sw" style="background:var(--poc)"></span>POC 最大量價位</span>
      <span><span class="sw" style="background:var(--va)"></span>價值區（70% 量能）</span>
      <span><i style="background:var(--green)"></i>現價</span>
      <span><i style="background:var(--accent)"></i>卡片訊號價</span>
    </div>
    <div class="ladders">{{LADDER_CARDS}}</div>
  </div>

  <div class="view" data-view="rail" hidden>
    <p class="view-note">全檔掃描。每一列是一檔，軌道左端是半年最低、右端是半年最高。
    一眼看完七檔誰在高位、誰在低位、誰貼著價值區。適合每天早上掃一次。</p>
    <div class="legend">
      <span><span class="sw" style="background:var(--va)"></span>價值區</span>
      <span><i style="background:var(--poc)"></i>POC</span>
      <span><i style="background:var(--green)"></i>現價</span>
      <span><i style="background:var(--accent)"></i>訊號價</span>
    </div>
    {{RAIL_ROWS}}
  </div>

  <div class="view" data-view="matrix" hidden>
    <p class="view-note">群聚檢查。橫軸切成 20 個等寬價格區間，顏色越深代表該區間成交量佔比越高。
    綠線是現價位置、藍線是訊號價位置。適合看「量能是集中在一段，還是散得很平」。</p>
    <div class="legend">
      <span><span class="sw" style="background:var(--accent)"></span>成交量佔比（越深越多）</span>
      <span><i style="background:var(--green)"></i>現價</span>
      <span><i style="background:var(--accent)"></i>訊號價</span>
    </div>
    <div class="matrix-axis"><span></span><span><b>低價</b><b>高價</b></span><span></span></div>
    {{MATRIX_ROWS}}
  </div>
</article>

<article class="panel">
  <h2>主線二 · 完整數字</h2>
  <p class="sub">POC 是半年內成交量最大的價位；價值區是涵蓋 70% 量能的價格帶；現價分位是「半年成交量有多少比例發生在現價以下」。
  容量欄是以 NT$5 萬單筆（50 萬 sleeve 分十檔）除以 20 日均量，數字越大代表你的單越難不驚動盤面。</p>
  <div class="table-wrap">
    <table><thead><tr><th>策略</th><th>股票</th><th class="num">卡片進場</th><th class="num">收盤</th>
    <th class="num">卡片報酬</th><th class="num">POC</th><th class="num">現價對 POC</th>
    <th class="num">現價分位</th><th class="num">價值區</th><th class="num">20 日均量</th></tr></thead>
    <tbody>{{ROSTER_ROWS}}</tbody></table>
  </div>
</article>

<article class="panel">
  <h2>接下來怎麼追</h2>
  <p class="sub">主線二要能長成有用的東西，靠的是每天留下同一組欄位，而不是每天換一種看法。</p>
  <ol class="steps">
    <li><b>每天收盤後把卡片存進 <code>latest_strategy_signals.csv</code>。</b>主線二的名單就是「卡上有、庫存沒有」的差集，名單會自己浮現，不需要人工維護。</li>
    <li><b>行情由 <code>watchlist.csv</code> 帶著走。</b>新進主線二的個股寫進去，隔天的抓取就會自動補它的 OHLCV，分價表不會開天窗。</li>
    <li><b>任何一檔從主線二轉成實際部位，就寫一筆 <code>signal_fills.csv</code>。</b>訊號價、次日開盤、實際成交價三個數字一起留，履約落差帳才會有樣本。</li>
    <li><b>累積 30 筆成交之後，回頭看「區間位置」那張圖。</b>那時候平均落點才有統計意義，也才知道要不要改下單方式。</li>
    <li><b>先量再改。</b>目前 {{BUY_N}} 筆買進的樣本量還不足以下結論，任何「改用限價／改掛開盤」的決定都應該等樣本夠了再談。</li>
  </ol>
</article>

<footer>
  <p>資料來源：TWSE／TPEx 公開日線（{{PROFILE_WINDOW}}）、owner 策略卡 {{SIGNAL_ASOF}}、成交簿 <code>actual_fills.csv</code>。
  分價分布以日線 [最低, 最高] 均勻分攤當日成交量估算，非逐筆分價表；交易所不公開個股逐筆分價，這個近似看不到盤中集中度。</p>
  <p>本頁純模擬追蹤，不含券商登入、不產生委託、不改動任何實際成交檔。生成時間 <code>{{GENERATED_AT}}</code>。</p>
  <p><a href="../">&larr; 主線一 實際績效</a> &middot; <a href="../claude/">稽核盤點</a></p>
</footer>

</div>
<script>
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  var views = Array.prototype.slice.call(document.querySelectorAll(".view"));
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      tabs.forEach(function (other) {
        other.setAttribute("aria-selected", String(other === tab));
      });
      views.forEach(function (view) {
        view.hidden = view.dataset.view !== tab.dataset.view;
      });
    });
  });
})();
</script>
</body>
</html>
"""
