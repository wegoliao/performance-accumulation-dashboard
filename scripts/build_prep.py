"""Preparation page for the next session's card instructions.

This page does NOT tell anyone what to buy, what to sell, or at what price. It
cannot: nothing in this repo can see the future, and picking entries for
someone else is advice, not measurement.

What it does is put the measurable facts next to each name the cards flagged,
so the owner walks in with the distribution instead of a hunch:

* where the stock has actually traded over six months (volume-at-price, its
  point of control and 70% value area) and where the last close sits inside it
* the last ten sessions' own high-low ranges, which is the width a limit order
  has to live inside
* how deep the book is relative to an NT$50k slot
* and, from the owner's own settled fills, where his orders have historically
  landed inside the day's range

That last one is the point. 3624 on 2026-09-10 opened 100.50, low 100.00,
closed limit-up at 113.50. A limit at 100 was a bid at the exact low of the
day -- it needed the tape to trade THROUGH the day's floor, and it did not.
His filled buys average 35% of the range; bidding at 0% is a different
instrument, and this page shows what that costs in fill probability.

Pure standard library. No network, no broker, no order path.
"""

from __future__ import annotations

import csv
import html
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_mainline2 as m2  # noqa: E402  (reuse the profiling block)

INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
SITE = ROOT / "prep"
RANGE_SESSIONS = 10


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any(row.values())]


def latest_holdings() -> tuple[date, dict[str, dict[str, str]]]:
    path = sorted(INPUTS.glob("holdings_snapshot_????-??-??.csv"))[-1]
    rows = read(path)
    day = datetime.strptime(rows[0]["asof_date"], "%Y-%m-%d").date()
    return day, {row["stock_code"].strip(): row for row in rows}


def action_list(signals: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Non-bracketed 進/出 rows from the newest card, with their effective date."""
    out: list[dict[str, Any]] = []
    for row in signals:
        raw = row["signal"].strip()
        if raw.startswith("("):
            continue
        action = raw.strip("()*")
        if action not in {"進", "出"}:
            continue
        out.append(
            {
                "strategy_id": row["strategy_id"].strip(),
                "code": row["stock_code"].strip(),
                "name": row.get("stock_name", "").strip(),
                "action": action,
                "effective": row.get("effective_date", "").strip(),
                "card_entry": row.get("entry_price", "").strip(),
                "card_display": row.get("entry_display", "").strip(),
                "close": float(row["close"]) if row.get("close") else None,
            }
        )
    return out


def fill_landing_stats(fills: list[dict[str, Any]], bars: dict[str, list[dict[str, Any]]]):
    landings = m2.fill_landings(fills, bars)
    usable = [row for row in landings if row.get("position") is not None]
    buys = [row for row in usable if row["side"] == "BUY"]
    sells = [row for row in usable if row["side"] == "SELL"]

    def mean(rows, key):
        return sum(row[key] for row in rows) / len(rows) if rows else None

    deciles = [0] * 10
    for row in buys:
        deciles[min(int(row["position"] * 10), 9)] += 1
    return {
        "buys": len(buys),
        "sells": len(sells),
        "buy_mean": mean(buys, "position"),
        "sell_mean": mean(sells, "position"),
        "deciles": deciles,
        "lowest": min((row["position"] for row in buys), default=None),
    }


def recent_ranges(series: list[dict[str, Any]], count: int = RANGE_SESSIONS):
    rows = series[-count:]
    spans = [
        (bar["high"] - bar["low"]) / bar["low"] * 100.0
        for bar in rows
        if bar["low"] > 0 and bar["high"] > bar["low"]
    ]
    return {
        "bars": rows,
        "median_span_pct": sorted(spans)[len(spans) // 2] if spans else None,
        "max_span_pct": max(spans) if spans else None,
    }


def build() -> tuple[Path, dict[str, Any]]:
    bars = m2.load_bars()
    fills = m2.load_fills()
    signal_asof, signals_map = m2.load_signals()
    signals = read(INPUTS / "latest_strategy_signals.csv")
    snapshot_day, held = latest_holdings()
    actions = action_list(signals)
    landing = fill_landing_stats(fills, bars)

    price_asof = max(series[-1]["date"] for series in bars.values())
    rows: list[dict[str, Any]] = []
    for item in actions:
        series = bars.get(item["code"])
        entry: dict[str, Any] = {**item, "held": item["code"] in held}
        if held.get(item["code"]):
            position = held[item["code"]]
            entry["shares"] = float(position["shares"])
            entry["unrealized_pct"] = float(position["unrealized_return_pct"])
            entry["unrealized_twd"] = float(position["unrealized_pnl_twd"])
        if not series:
            entry["status"] = "NO_PRICE_HISTORY"
            rows.append(entry)
            continue
        profile = m2.volume_profile(series)
        last = series[-1]
        card_entry = None
        try:
            card_entry = float(item["card_entry"]) if item["card_entry"] else None
        except ValueError:
            card_entry = None
        entry.update(
            {
                "status": "OK",
                "profile": profile,
                "last": last,
                "close_pct": m2.percentile_of(profile, last["close"]),
                "card_entry": card_entry,
                "card_pct": m2.percentile_of(profile, card_entry) if card_entry else None,
                "in_value_area": profile["value_low"] <= last["close"] <= profile["value_high"],
                "poc_gap": last["close"] / profile["poc"] - 1.0,
                "capacity": m2.capacity(series, last["close"]),
                "ranges": recent_ranges(series),
            }
        )
        rows.append(entry)

    SITE.mkdir(parents=True, exist_ok=True)
    page = render(rows, landing, signal_asof, price_asof, snapshot_day)
    target = SITE / "index.html"
    target.write_text(page, encoding="utf-8", newline="\n")

    receipt = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "signal_asof": signal_asof.isoformat(),
        "price_asof": price_asof.isoformat(),
        "snapshot_asof": snapshot_day.isoformat(),
        "actions": len(rows),
        "profiled": sum(1 for row in rows if row["status"] == "OK"),
        "missing_history": [row["code"] for row in rows if row["status"] != "OK"],
        "fill_landing": landing,
        "boundaries": [
            "NO_PRICE_RECOMMENDATION",
            "NO_BROKER_LOGIN",
            "NO_ORDER_PATH",
            "DESCRIPTIVE_STATISTICS_ONLY",
        ],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "prep_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target, receipt


# ---------------------------------------------------------------------- render


def fmt(value, digits=2, dash="—"):
    return dash if value is None else f"{value:,.{digits}f}"


def pct(value, sign=False, digits=1, dash="—"):
    if value is None:
        return dash
    return f"{value * 100:{'+' if sign else ''}.{digits}f}%"


def cls(value):
    if value is None:
        return "neutral"
    return "positive" if value > 0 else ("negative" if value < 0 else "neutral")


def rail(row: dict, width: int = 460, height: int = 46) -> str:
    """Six-month range as a rail: value area shaded, POC, close, card entry."""
    profile = row["profile"]
    span = profile["high"] - profile["low"]
    mid = height / 2

    def x_of(price):
        return (price - profile["low"]) / span * width

    parts = [f'<line x1="0" y1="{mid}" x2="{width}" y2="{mid}" class="rail"/>']
    parts.append(
        f'<rect x="{x_of(profile["value_low"]):.1f}" y="{mid - 10:.1f}" '
        f'width="{max(x_of(profile["value_high"]) - x_of(profile["value_low"]), 1):.1f}" '
        f'height="20" class="va"/>'
    )
    parts.append(
        f'<line x1="{x_of(profile["poc"]):.1f}" y1="{mid - 14}" '
        f'x2="{x_of(profile["poc"]):.1f}" y2="{mid + 14}" class="poc"/>'
    )
    # last ten sessions' own ranges, so the width a limit order lives in is visible
    for bar in row["ranges"]["bars"]:
        lo, hi = x_of(bar["low"]), x_of(bar["high"])
        parts.append(
            f'<line x1="{lo:.1f}" y1="{mid + 16:.1f}" x2="{hi:.1f}" y2="{mid + 16:.1f}" '
            'class="daily"/>'
        )
    if row.get("card_entry"):
        parts.append(
            f'<circle cx="{x_of(row["card_entry"]):.1f}" cy="{mid}" r="4.5" class="card-dot">'
            f'<title>卡片參考價 {row["card_entry"]:g}</title></circle>'
        )
    parts.append(
        f'<circle cx="{x_of(row["last"]["close"]):.1f}" cy="{mid}" r="5.5" class="now-dot">'
        f'<title>最新收盤 {row["last"]["close"]:g}</title></circle>'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="rail-svg" '
        f'preserveAspectRatio="none" role="img">{"".join(parts)}</svg>'
    )


def action_rows(rows: list[dict]) -> str:
    out = []
    for row in sorted(rows, key=lambda r: (r["action"], r["strategy_id"], r["code"])):
        label = m2.STRATEGY_LABELS.get(row["strategy_id"], row["strategy_id"])
        badge = (
            '<span class="b sell">出</span>' if row["action"] == "出"
            else '<span class="b buy">進</span>'
        )
        holding = (
            f'<span class="positive">持有 {row["shares"]:,.0f} 股</span>'
            f'<br><small class="{cls(row.get("unrealized_twd"))}">'
            f'{row["unrealized_pct"]:+.2f}%</small>'
            if row.get("held") else '<span class="neutral">零部位</span>'
        )
        if row["status"] != "OK":
            out.append(
                f'<tr><td>{badge}</td><td>{html.escape(label)}</td>'
                f'<td><b>{row["code"]}</b> {html.escape(row["name"])}</td>'
                f'<td>{holding}</td>'
                '<td colspan="5" class="neutral">尚無行情歷史，無法量測</td></tr>'
            )
            continue
        profile, ranges = row["profile"], row["ranges"]
        cap = row["capacity"]
        out.append(
            "<tr>"
            f"<td>{badge}</td><td>{html.escape(label)}</td>"
            f'<td><b>{row["code"]}</b> {html.escape(row["name"])}<br>'
            f'<small>卡片價 {fmt(row["card_entry"]) if row["card_entry"] else html.escape(row["card_display"])}</small></td>'
            f"<td>{holding}</td>"
            f'<td class="num"><b>{fmt(row["last"]["close"])}</b><br>'
            f'<small>分位 {pct(row["close_pct"], digits=0)}</small></td>'
            f'<td class="rail-cell">{rail(row)}'
            f'<div class="rail-scale"><span>{fmt(profile["low"])}</span>'
            f'<span>{fmt(profile["high"])}</span></div></td>'
            f'<td class="num">{fmt(profile["poc"])}<br>'
            f'<small class="{cls(row["poc_gap"])}">{pct(row["poc_gap"], sign=True)}</small></td>'
            f'<td class="num">{pct((ranges["median_span_pct"] or 0) / 100, digits=1)}<br>'
            f'<small>最大 {pct((ranges["max_span_pct"] or 0) / 100, digits=1)}</small></td>'
            f'<td class="num">{fmt(cap["avg_volume"], 0)}<br>'
            f'<small>單量佔 {pct(cap["participation"], digits=3)}</small></td>'
            "</tr>"
        )
    return "".join(out)


def landing_block(landing: dict) -> str:
    peak = max(landing["deciles"]) or 1
    bars = "".join(
        f'<div class="dec"><div class="dec-bar" style="height:{count / peak * 100:.0f}%">'
        f'<span>{count or ""}</span></div><div class="dec-x">{index * 10}</div></div>'
        for index, count in enumerate(landing["deciles"])
    )
    lowest = landing["lowest"]
    return (
        '<div class="landing-wrap">'
        f'<div class="decs">{bars}</div>'
        '<div class="dec-axis"><span>0% = 當日最低</span><span>100% = 當日最高</span></div>'
        "</div>"
        f'<div class="stat-row">'
        f'<div class="stat"><b>{landing["buys"]}</b><span>已成交買進</span></div>'
        f'<div class="stat"><b>{pct(landing["buy_mean"], digits=0)}</b><span>平均落點</span></div>'
        f'<div class="stat"><b>{pct(lowest, digits=0) if lowest is not None else "—"}</b>'
        f"<span>歷來最低落點</span></div>"
        f'<div class="stat"><b>{pct(landing["sell_mean"], digits=0)}</b><span>賣出平均落點</span></div>'
        "</div>"
    )


TEMPLATE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>備戰頁 · {EFFECTIVE} 卡片動作量測</title>
<meta name="description" content="策略卡下一個交易日的進出名單，配上分價分布、近期日內區間與容量。純量測，不含買賣建議。">
<style>
:root{{--bg:#0d1117;--panel:#161b22;--raise:#1c2430;--ink:#e6edf3;--muted:#8b949e;
--line:#26303b;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--gold:#d29922;
--va:rgba(88,166,255,.16);--poc:#d29922}}
:root[data-theme="light"]{{--bg:#f6f8fa;--panel:#fff;--raise:#f0f3f6;--ink:#1f2328;--muted:#59636e;
--line:#d8dee4;--accent:#0969da;--green:#1a7f37;--red:#cf222e;--gold:#9a6700;
--va:rgba(9,105,218,.13);--poc:#9a6700}}
@media(prefers-color-scheme:light){{:root:not([data-theme="dark"]){{--bg:#f6f8fa;--panel:#fff;
--raise:#f0f3f6;--ink:#1f2328;--muted:#59636e;--line:#d8dee4;--accent:#0969da;--green:#1a7f37;
--red:#cf222e;--gold:#9a6700;--va:rgba(9,105,218,.13);--poc:#9a6700}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);line-height:1.6;
font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,-apple-system,sans-serif;
-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1240px;margin:0 auto;padding:30px 20px 80px}}
a{{color:var(--accent)}}
.eyebrow{{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:700}}
h1{{font-size:clamp(26px,4vw,38px);margin:8px 0 10px;letter-spacing:-.02em;line-height:1.15}}
.lede{{color:var(--muted);max-width:70ch;margin:0}}
.meta{{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:14px;font-size:13px;color:var(--muted)}}
.meta code{{background:var(--raise);padding:1px 6px;border-radius:4px;font-size:12px}}
.notice{{margin:22px 0 26px;border:1px solid var(--gold);border-left-width:3px;border-radius:10px;
background:rgba(210,153,34,.08);padding:15px 18px;font-size:14px}}
.notice b{{color:var(--gold)}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:16px}}
.panel h2{{font-size:19px;margin:0 0 4px;letter-spacing:-.01em}}
.panel .sub{{color:var(--muted);font-size:13.5px;margin:0 0 18px;max-width:82ch}}
.table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
table{{width:100%;border-collapse:collapse;font-size:13px;min-width:960px}}
th{{text-align:left;color:var(--muted);font-weight:650;font-size:11.5px;letter-spacing:.04em;
border-bottom:1px solid var(--line);padding:9px 8px;white-space:nowrap}}
td{{padding:11px 8px;border-bottom:1px solid var(--line);vertical-align:middle}}
tbody tr:last-child td{{border-bottom:none}}
td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
small{{color:var(--muted)}}
.positive{{color:var(--green)}} .negative{{color:var(--red)}} .neutral{{color:var(--muted)}}
.b{{display:inline-block;font-weight:800;font-size:13px;border-radius:6px;padding:3px 10px}}
.b.sell{{background:rgba(248,81,73,.16);color:var(--red)}}
.b.buy{{background:rgba(63,185,80,.16);color:var(--green)}}
.rail-cell{{min-width:300px}}
.rail-svg{{width:100%;height:46px;display:block}}
.rail{{stroke:var(--line);stroke-width:2}}
.va{{fill:var(--va)}}
.poc{{stroke:var(--poc);stroke-width:1.6;stroke-dasharray:4 3}}
.daily{{stroke:var(--muted);stroke-width:2.4;opacity:.45;stroke-linecap:round}}
.card-dot{{fill:var(--accent)}}
.now-dot{{fill:var(--green);stroke:var(--panel);stroke-width:1.5}}
.rail-scale{{display:flex;justify-content:space-between;font-size:10.5px;color:var(--muted);
margin-top:2px;font-variant-numeric:tabular-nums}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-bottom:14px}}
.legend i{{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px;border-radius:2px}}
.legend .sw{{display:inline-block;width:14px;height:10px;vertical-align:middle;margin-right:6px;border-radius:2px}}
.landing-wrap{{margin-bottom:14px}}
.decs{{display:flex;align-items:flex-end;gap:5px;height:130px}}
.dec{{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%}}
.dec-bar{{background:var(--accent);border-radius:4px 4px 0 0;min-height:3px;position:relative;
display:flex;align-items:flex-start;justify-content:center}}
.dec-bar span{{font-size:11px;font-weight:700;color:var(--bg);margin-top:2px}}
.dec-x{{text-align:center;font-size:10px;color:var(--muted);margin-top:5px}}
.dec-axis{{display:flex;justify-content:space-between;font-size:11.5px;color:var(--muted);margin-top:4px}}
.stat-row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-top:16px}}
.stat{{background:var(--raise);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.stat b{{display:block;font-size:23px;font-weight:750;letter-spacing:-.02em}}
.stat span{{font-size:12px;color:var(--muted)}}
.case{{background:var(--raise);border-left:3px solid var(--gold);border-radius:0 10px 10px 0;
padding:14px 18px;margin-top:16px;font-size:13.5px}}
.case b{{color:var(--gold)}}
.case table{{min-width:0;margin-top:10px;font-size:12.5px}}
footer{{margin-top:40px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px}}
</style>
</head>
<body><div class="wrap">

<header>
  <div class="eyebrow">Preparation &middot; measurement only</div>
  <h1>備戰頁 · {EFFECTIVE} 卡片動作</h1>
  <p class="lede">策略卡對下一個交易日開出的每一個進出，配上這檔股票<b>實際成交過的價格分布</b>、
  近十個交易日<b>自己的日內區間</b>，以及你的單相對於它的量能有多大。</p>
  <div class="meta">
    <span>訊號日 <code>{SIGNAL_ASOF}</code></span>
    <span>生效日 <code>{EFFECTIVE}</code></span>
    <span>行情截止 <code>{PRICE_ASOF}</code></span>
    <span>庫存快照 <code>{SNAPSHOT_ASOF}</code></span>
    <span><a href="../">&larr; 實際績效</a></span>
    <span><a href="../mainline2/">主線二</a></span>
  </div>
</header>

<div class="notice">
  <b>這頁沒有買賣建議，也不會有。</b>
  它不推薦任何一檔股票、不指定任何一個價位、不判斷方向。上面每一個數字都是<b>已經發生的事</b>——
  這檔股票過去成交在哪些價位、最近幾天自己波動多寬、你的單相對它的量能多大。
  把分布擺在你面前，決定仍然是你的。<br>
  這條支線同樣<b>不連券商、不取即時報價、不產生委託</b>。所有價格都是交易所公開收盤，最新為 <code>{PRICE_ASOF}</code>。
</div>

<article class="panel">
  <h2>你的單實際落在哪裡 · {BUYS} 筆已成交買進</h2>
  <p class="sub">把每一筆成交價放回<b>當天自己的最高／最低區間</b>，0% 是當日最低、100% 是當日最高。
  這是本 repo 唯一能對「掛單位置」說的實證，因為它從已結算成交算出來，不是模擬。</p>
  {LANDING}
  <div class="case">
    <b>3624 光頡 · 2026-09-10 的實例</b><br>
    當天 開 100.50、<b>最低 100.00</b>、高 113.50、收 113.50（漲停）。掛 100 是<b>掛在當日的絕對地板</b>——
    它需要盤中「跌破」100.00 才會輪到你，而那天沒有跌破，100.00 就是最低點。<br>
    你已成交的 {BUYS} 筆買進平均落在區間 <b>{BUY_MEAN}</b>，歷來最低的一筆也在 <b>{LOWEST}</b>。
    掛在 0% 和掛在 {BUY_MEAN} 是兩種不同的東西：前者買到會更便宜，但<b>經常買不到</b>。
    這個取捨要怎麼選是你的決定，這裡只把兩邊的代價量出來。
  </div>
</article>

<article class="panel">
  <h2>{EFFECTIVE} 的 {ACTIONS} 個動作 · 逐檔量測</h2>
  <p class="sub">括號部位依 owner 2026-09-05 指示排除，不列在這裡。
  <b>「出」只有你仍持有的才需要動作</b>，已經不在庫的列出來是為了完整，不是要你做什麼。</p>
  <div class="legend">
    <span><span class="sw" style="background:var(--va)"></span>價值區（70% 量能）</span>
    <span><i style="background:var(--poc)"></i>POC 最大量價位</span>
    <span><i style="background:var(--muted)"></i>近 10 日各自的日內區間</span>
    <span><i style="background:var(--green)"></i>最新收盤</span>
    <span><i style="background:var(--accent)"></i>卡片參考價</span>
  </div>
  <div class="table-wrap"><table><thead><tr>
    <th>動作</th><th>策略</th><th>股票</th><th>部位</th>
    <th class="num">最新收盤</th><th>六個月價格分布</th>
    <th class="num">POC</th><th class="num">日內區間<br>中位數</th>
    <th class="num">20 日均量</th>
  </tr></thead><tbody>{ROWS}</tbody></table></div>
</article>

<footer>
  <p>分價分布以日線 [最低, 最高] 均勻分攤當日成交量估算，非逐筆分價表——交易所不公開個股逐筆分價，
  這個近似看不到盤中集中度。日內區間為近 {RANGE_SESSIONS} 個交易日各自的高低差。
  容量以 NT$5 萬單筆除以 20 日均量估算。</p>
  <p>不連券商、不取即時報價、不產生委託。生成時間 <code>{GENERATED_AT}</code>。</p>
  <p><a href="../">&larr; 實際績效</a> &middot; <a href="../mainline2/">主線二</a> &middot; <a href="../claude/">稽核盤點</a></p>
</footer>

</div></body></html>
"""


def render(rows, landing, signal_asof, price_asof, snapshot_day) -> str:
    effective = next(
        (row["effective"] for row in rows if row["effective"]), price_asof.isoformat()
    )
    return TEMPLATE.format(
        EFFECTIVE=effective,
        SIGNAL_ASOF=signal_asof.isoformat(),
        PRICE_ASOF=price_asof.isoformat(),
        SNAPSHOT_ASOF=snapshot_day.isoformat(),
        ACTIONS=len(rows),
        BUYS=landing["buys"],
        BUY_MEAN=pct(landing["buy_mean"], digits=0),
        LOWEST=pct(landing["lowest"], digits=0) if landing["lowest"] is not None else "—",
        LANDING=landing_block(landing),
        ROWS=action_rows(rows),
        RANGE_SESSIONS=RANGE_SESSIONS,
        GENERATED_AT=datetime.now().isoformat(timespec="seconds"),
    )


def main() -> int:
    target, receipt = build()
    print(f"SUCCESS: {target}")
    print(f"ACTIONS: {receipt['actions']} ({receipt['profiled']} 檔可量測)")
    if receipt["missing_history"]:
        print(f"NO_HISTORY: {receipt['missing_history']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
