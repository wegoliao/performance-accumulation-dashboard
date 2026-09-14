"""Per-position technical check-up for every name the account holds.

The owner asked, with no cash left to add, where each losing position's
support sits so he can check them one by one. This page answers with the
levels that can be computed from public daily bars and his own fills, and
stops there:

* moving averages (20 / 60 / 120 sessions) and which side of each the close is
* swing lows and highs over 20 / 60 / 120 sessions, and the low since his entry
* the six-month volume profile's point of control and 70% value area
* the average true range, i.e. how far this stock typically travels in a day
* what closing the position at each level below the close would realise,
  net of fee and tax, against his cost

A "support" here is a price the stock has stopped at before. It is a fact
about the past. Whether it holds again is not something this page, or anything
else in this repo, can know. The short position (融券) is shown with the same
levels read the other way round: for a short, the levels above are where the
loss grows.

Pure standard library. No network, no broker, no order path.
"""

from __future__ import annotations

import csv
import html
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_mainline2 as m2  # noqa: E402
import build_prep as prep  # noqa: E402

INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
SITE = ROOT / "positions"
EXCLUDED = {"2886"}          # owner: 兆豐不要算
ATR_WINDOW = 14
CHART_SESSIONS = 120
FEE_RATE = 0.001425
TAX_RATE = 0.003


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any(row.values())]


def net_proceeds(shares: float, price: float) -> float:
    gross = shares * price
    return gross - int(gross * FEE_RATE) - int(gross * TAX_RATE)


def sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def atr(bars: list[dict[str, Any]], window: int = ATR_WINDOW) -> float | None:
    if len(bars) < window + 1:
        return None
    ranges = []
    for prev, bar in zip(bars[-window - 1:-1], bars[-window:]):
        ranges.append(
            max(bar["high"] - bar["low"], abs(bar["high"] - prev["close"]), abs(bar["low"] - prev["close"]))
        )
    return sum(ranges) / len(ranges)


def card_status(code: str, signals: list[dict[str, str]], history: list[dict[str, str]]) -> str:
    """What the newest card says about this name, or the last thing any card said."""
    for row in signals:
        if row["stock_code"].strip() == code:
            sig = row["signal"].strip()
            return f'{m2.STRATEGY_LABELS.get(row["strategy_id"].strip(), row["strategy_id"])} {sig}'
    last = [row for row in history if row["stock_code"].strip() == code and not row["signal"].strip().startswith("(")]
    if last:
        row = last[-1]
        return (
            f'{m2.STRATEGY_LABELS.get(row["strategy_id"].strip(), row["strategy_id"])} '
            f'{row["signal"].strip()}（{row["asof_date"]} 卡，之後不在卡上）'
        )
    return "不在任何卡上"


def first_fill_date(code: str, fills: list[dict[str, Any]]) -> date | None:
    days = [row["date"] for row in fills if row["stock_code"] == code and row["side"] == "BUY"]
    return min(days) if days else None


def analyse(position: dict[str, str], bars: list[dict[str, Any]], fills, signals, history) -> dict[str, Any]:
    code = position["stock_code"].strip()
    shares = float(position["shares"])
    cost_basis = float(position["cost_basis_twd"])
    avg_cost = float(position["avg_cost"])
    close = float(position["last_price"])
    is_short = position["category"].strip() == "融券"
    closes = [bar["close"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    profile = m2.volume_profile(bars)
    entry_day = first_fill_date(code, fills)
    since_entry = [bar for bar in bars if entry_day and bar["date"] >= entry_day]

    levels: list[dict[str, Any]] = []

    def add(label: str, price: float | None, kind: str) -> None:
        if price is None or price <= 0:
            return
        levels.append({"label": label, "price": price, "kind": kind})

    add("MA20", sma(closes, 20), "ma")
    add("MA60", sma(closes, 60), "ma")
    add("MA120", sma(closes, 120), "ma")
    add("20 日最低", min(lows[-20:]), "low")
    add("60 日最低", min(lows[-60:]), "low")
    add("120 日最低", min(lows[-120:]), "low")
    add("20 日最高", max(highs[-20:]), "high")
    add("60 日最高", max(highs[-60:]), "high")
    add("120 日最高", max(highs[-120:]), "high")
    add("POC（六月量能最大價位）", profile["poc"], "profile")
    add("價值區下緣", profile["value_low"], "profile")
    add("價值區上緣", profile["value_high"], "profile")
    if since_entry:
        add("進場以來最低", min(bar["low"] for bar in since_entry), "entry")
        add("進場以來最高", max(bar["high"] for bar in since_entry), "entry")
    add("你的成本", avg_cost, "cost")

    for level in levels:
        level["gap_pct"] = (level["price"] / close - 1.0) * 100.0
        # what closing here would realise against cost (long) -- for a short the
        # sign flips: proceeds were taken at entry, cost to cover is here
        if is_short:
            level["pnl_at"] = cost_basis - shares * level["price"] - int(shares * level["price"] * FEE_RATE)
        else:
            level["pnl_at"] = net_proceeds(shares, level["price"]) - cost_basis
    below = sorted((l for l in levels if l["price"] < close), key=lambda l: -l["price"])
    above = sorted((l for l in levels if l["price"] >= close), key=lambda l: l["price"])

    day_range = atr(bars)
    return {
        "code": code,
        "name": position["stock_name"].strip(),
        "category": position["category"].strip(),
        "is_short": is_short,
        "shares": shares,
        "avg_cost": avg_cost,
        "cost_basis": cost_basis,
        "close": close,
        "value": float(position["current_value_twd"]),
        "pnl": float(position["unrealized_pnl_twd"]),
        "ret_pct": float(position["unrealized_return_pct"]),
        "card": card_status(code, signals, history),
        "entry_day": entry_day,
        "atr": day_range,
        "atr_pct": day_range / close * 100.0 if day_range else None,
        "atr_twd": day_range * shares if day_range else None,
        "ma20": sma(closes, 20),
        "ma60": sma(closes, 60),
        "ma120": sma(closes, 120),
        "close_pct": m2.percentile_of(profile, close),
        "profile": profile,
        "below": below,
        "above": above,
        "bars": bars[-CHART_SESSIONS:],
        "sessions": len(bars),
    }


# ---------------------------------------------------------------------- render


def chart_svg(row: dict[str, Any], width: int = 640, height: int = 220) -> str:
    bars = row["bars"]
    if len(bars) < 5:
        return '<div class="neutral">行情不足，無法畫圖</div>'
    pad_l, pad_r, pad_t, pad_b = 8, 64, 10, 22
    xs = lambda i: pad_l + i / (len(bars) - 1) * (width - pad_l - pad_r)
    lo = min(bar["low"] for bar in bars)
    hi = max(bar["high"] for bar in bars)
    for level in ("avg_cost",):
        lo, hi = min(lo, row[level]), max(hi, row[level])
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.04, hi + span * 0.04
    ys = lambda p: pad_t + (hi - p) / (hi - lo) * (height - pad_t - pad_b)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img">']
    # candles as high-low wicks plus open-close body
    bw = max(1.2, (width - pad_l - pad_r) / len(bars) * 0.6)
    for i, bar in enumerate(bars):
        x = xs(i)
        up = bar["close"] >= bar["open"]
        colour = "var(--red)" if up else "var(--green)"   # TW convention: red up, green down
        parts.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{ys(bar["high"]):.1f}" y2="{ys(bar["low"]):.1f}" stroke="{colour}" stroke-width="1"/>')
        top, bot = ys(max(bar["open"], bar["close"])), ys(min(bar["open"], bar["close"]))
        parts.append(f'<rect x="{x - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(1.0, bot - top):.1f}" fill="{colour}"/>')
    # moving averages
    closes_all = [bar["close"] for bar in bars]
    for window, colour in ((20, "var(--accent)"), (60, "var(--gold)")):
        pts = []
        for i in range(len(bars)):
            if i + 1 >= window:
                pts.append(f"{xs(i):.1f},{ys(sum(closes_all[i + 1 - window:i + 1]) / window):.1f}")
        if pts:
            parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{colour}" stroke-width="1.4"/>')
    # cost line and label
    yc = ys(row["avg_cost"])
    parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{yc:.1f}" y2="{yc:.1f}" stroke="var(--ink)" stroke-dasharray="5 4" stroke-width="1.2"/>')
    parts.append(f'<text x="{width - pad_r + 4}" y="{yc + 4:.1f}" font-size="10.5" fill="var(--ink)">成本 {row["avg_cost"]:g}</text>')
    # close label
    yl = ys(row["close"])
    parts.append(f'<text x="{width - pad_r + 4}" y="{yl + 4:.1f}" font-size="10.5" font-weight="700" fill="var(--accent)">收 {row["close"]:g}</text>')
    # entry marker
    if row["entry_day"]:
        for i, bar in enumerate(bars):
            if bar["date"] >= row["entry_day"]:
                parts.append(f'<line x1="{xs(i):.1f}" x2="{xs(i):.1f}" y1="{pad_t}" y2="{height - pad_b}" stroke="var(--muted)" stroke-dasharray="2 3"/>')
                parts.append(f'<text x="{xs(i) + 3:.1f}" y="{height - pad_b - 4}" font-size="10" fill="var(--muted)">進場</text>')
                break
    parts.append(f'<text x="{pad_l}" y="{height - 6}" font-size="10" fill="var(--muted)">{bars[0]["date"].isoformat()}</text>')
    parts.append(f'<text x="{width - pad_r - 70}" y="{height - 6}" font-size="10" fill="var(--muted)">{bars[-1]["date"].isoformat()}</text>')
    parts.append("</svg>")
    return "".join(parts)


def level_rows(levels: list[dict[str, Any]], close: float) -> str:
    if not levels:
        return '<tr><td colspan="4" class="neutral">無</td></tr>'
    out = []
    for level in levels:
        out.append(
            "<tr>"
            f'<td>{html.escape(level["label"])}</td>'
            f'<td class="num">{level["price"]:,.2f}</td>'
            f'<td class="num {m2.value_class(level["gap_pct"])}">{level["gap_pct"]:+.1f}%</td>'
            f'<td class="num {m2.value_class(level["pnl_at"])}">{level["pnl_at"]:+,.0f}</td>'
            "</tr>"
        )
    return "".join(out)


def position_card(row: dict[str, Any]) -> str:
    side = "融券（放空）" if row["is_short"] else "現股"
    ma_line = " · ".join(
        f'{label} {value:,.2f}（{"上" if row["close"] >= value else "下"}）'
        for label, value in (("MA20", row["ma20"]), ("MA60", row["ma60"]), ("MA120", row["ma120"]))
        if value
    )
    atr_txt = (
        f'{row["atr"]:,.2f}（{row["atr_pct"]:.1f}%，這個部位一天約 NT$ {row["atr_twd"]:,.0f}）'
        if row["atr"] else "—"
    )
    entry_txt = row["entry_day"].isoformat() if row["entry_day"] else "—"
    lower_title = "上方（空單在這裡虧更多）" if row["is_short"] else "下方（過去停過的位置）"
    upper_title = "下方（空單在這裡回補的損益）" if row["is_short"] else "上方（回到這裡的損益）"
    first, second = (row["above"], row["below"]) if row["is_short"] else (row["below"], row["above"])
    warn = (
        '<div class="case"><b>這是放空部位。</b>股價上漲是它的虧損，所以下表把「上方」擺在前面。'
        '它不是策略部位，卡片對它沒有指示。</div>'
        if row["is_short"] else ""
    )
    return f"""
<article class="panel" id="s{row['code']}">
  <h2>{row['code']} {html.escape(row['name'])} <small>{side} · {html.escape(row['card'])}</small></h2>
  <div class="stat-row">
    <div class="stat"><b>{row['close']:,.2f}</b><span>最新收盤</span></div>
    <div class="stat"><b>{row['avg_cost']:,.2f}</b><span>成本均價 · {row['shares']:,.0f} 股</span></div>
    <div class="stat"><b class="{m2.value_class(row['pnl'])}">{row['pnl']:+,.0f}</b><span>未實現 {row['ret_pct']:+.2f}%</span></div>
    <div class="stat"><b>{m2.pct(row['close_pct'], digits=0)}</b><span>六月成交量在收盤以下的比例</span></div>
  </div>
  <div class="chart-wrap">{chart_svg(row)}</div>
  <div class="legend"><span><i style="background:var(--accent)"></i>MA20</span><span><i style="background:var(--gold)"></i>MA60</span><span><i style="background:var(--ink)"></i>你的成本</span><span>紅漲綠跌</span></div>
  <p class="sub">均線：{ma_line}<br>ATR(14) 日均真實波幅：{atr_txt}<br>首筆進場：{entry_txt} · 資料 {row['sessions']} 個交易日</p>
  {warn}
  <div class="two">
    <div><h3>{lower_title}</h3><div class="table-wrap"><table class="lv"><thead><tr><th>位置</th><th class="num">價格</th><th class="num">距收盤</th><th class="num">在這裡了結（淨）</th></tr></thead><tbody>{level_rows(first, row['close'])}</tbody></table></div></div>
    <div><h3>{upper_title}</h3><div class="table-wrap"><table class="lv"><thead><tr><th>位置</th><th class="num">價格</th><th class="num">距收盤</th><th class="num">在這裡了結（淨）</th></tr></thead><tbody>{level_rows(second, row['close'])}</tbody></table></div></div>
  </div>
</article>"""


def summary_table(rows: list[dict[str, Any]]) -> str:
    out = []
    for row in rows:
        nearest = row["above"][0] if row["is_short"] else (row["below"][0] if row["below"] else None)
        near_txt = f'{nearest["label"]} {nearest["price"]:,.2f}（{nearest["gap_pct"]:+.1f}%）' if nearest else "—"
        out.append(
            "<tr>"
            f'<td><a href="#s{row["code"]}">{row["code"]} {html.escape(row["name"])}</a></td>'
            f'<td>{html.escape(row["card"])}</td>'
            f'<td class="num">{row["value"]:,.0f}</td>'
            f'<td class="num {m2.value_class(row["pnl"])}">{row["pnl"]:+,.0f}<br><small>{row["ret_pct"]:+.2f}%</small></td>'
            f'<td class="num">{row["atr_twd"]:,.0f}</td>'
            f'<td>{near_txt}</td>'
            "</tr>"
        )
    return "".join(out)


def build() -> tuple[Path, dict[str, Any]]:
    bars = m2.load_bars()
    fills = m2.load_fills()
    signals = read(INPUTS / "latest_strategy_signals.csv")
    history = read(INPUTS / "signal_history.csv")
    snapshot_day, held = prep.latest_holdings()
    rows = []
    for code, position in held.items():
        if code in EXCLUDED or code not in bars:
            continue
        rows.append(analyse(position, bars[code], fills, signals, history))
    rows.sort(key=lambda r: r["pnl"])  # worst first
    price_asof = max(series[-1]["date"] for series in bars.values())
    signal_asof = max(datetime.strptime(r["asof_date"], "%Y-%m-%d").date() for r in signals)

    style = re.search(r"<style>.*?</style>", prep.TEMPLATE, re.S).group(0).replace("{{", "{").replace("}}", "}")
    extra = """<style>
.chart-wrap{margin:10px 0 6px}.chart{width:100%;height:220px;display:block}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:820px){.two{grid-template-columns:1fr}}
.two h3{font-size:14px;margin:8px 0 6px;color:var(--muted)}
table.lv{min-width:0}
.panel h2 small{font-size:13px;font-weight:500;color:var(--muted);margin-left:8px}
</style>"""
    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>持股體檢 · {snapshot_day.isoformat()} 每一檔的位置</title>
<meta name="description" content="每一檔持股的均線、波段高低、分價分布與在各價位了結的淨損益。純量測，不含買賣建議。">
{style}{extra}</head><body><div class="wrap">
<header>
  <div class="eyebrow">Positions &middot; measurement only</div>
  <h1>持股體檢 · 每一檔現在站在哪裡</h1>
  <p class="lede">沒有新資金的時候，問題從「買什麼」變成「每一檔在哪裡」。這頁把每一檔持股放回它自己的
  <b>均線、波段高低、六個月分價分布</b>，並算出在每一個位置了結的淨損益 —— 讓你一檔一檔自己確認。</p>
  <div class="meta">
    <span>庫存快照 <code>{snapshot_day.isoformat()}</code></span>
    <span>行情截止 <code>{price_asof.isoformat()}</code></span>
    <span>最新卡片 <code>{signal_asof.isoformat()}</code></span>
    <span><a href="../">&larr; 實際績效</a></span><span><a href="../prep/">備戰頁</a></span><span><a href="../history/">歷史存檔</a></span>
  </div>
</header>
<div class="notice"><b>「支撐」在這裡的意思是：這檔股票過去在這個價位停過。</b>它是一個事實，不是一個預測。
均線、波段低點、價值區下緣都只是「曾經」，會不會再守住，這頁不知道，這個 repo 裡也沒有東西知道。
每一列最右邊的「在這裡了結」是扣掉手續費與交易稅後對成本的淨損益 —— 認賠認多少，先看數字再決定。</div>

<article class="panel"><h2>總覽 · 虧最多的排前面</h2>
<div class="sub">「一天典型波動」是 ATR(14) × 股數，是這個部位一天通常會晃的金額。「最近的下方位置」是收盤以下第一個過去停過的價位（放空部位改看上方）。</div>
<div class="table-wrap"><table><thead><tr><th>股票</th><th>卡片現況</th><th class="num">市值</th><th class="num">未實現</th><th class="num">一天典型波動</th><th>最近的下方位置</th></tr></thead>
<tbody>{summary_table(rows)}</tbody></table></div></article>

{"".join(position_card(row) for row in rows)}

<footer>資料：inputs/price_history.csv（TWSE／TPEx 公開日資料）、inputs/holdings_snapshot_{snapshot_day.isoformat()}.csv（你貼的庫存）、inputs/actual_fills.csv（你的成交）。
沒有券商連線、沒有委託路徑、沒有買賣價格建議。兆豐（2886）依指示不計。</footer>
</div></body></html>
"""
    SITE.mkdir(parents=True, exist_ok=True)
    target = SITE / "index.html"
    target.write_text(page, encoding="utf-8", newline="\n")
    receipt = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot_asof": snapshot_day.isoformat(),
        "price_asof": price_asof.isoformat(),
        "positions": [row["code"] for row in rows],
        "boundaries": ["NO_PRICE_RECOMMENDATION", "NO_BROKER_LOGIN", "NO_ORDER_PATH", "DESCRIPTIVE_LEVELS_ONLY"],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "positions_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return target, receipt


def main() -> int:
    target, receipt = build()
    print(f"SUCCESS: {target}")
    print(f"POSITIONS: {len(receipt['positions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
