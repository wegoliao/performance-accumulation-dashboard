"""Tomorrow's watch sheet: one row per name that matters, with the levels to watch.

The strategy card prints each member's return against its own entry price and
nothing else -- no path, no peak, no drawdown, no memory of what the sleeve
has done since the card started. This page adds the missing memory from the
card history and public daily bars, then lists, per name, the prices at which
something the owner cares about would change:

* the card's entry price      -- below it the card's own position is negative
* the high since card entry   -- how far the name has given back from its best
* the 20-session low and MA20 -- the two nearest places the stock stopped before
* the owner's cost            -- for held names, where the account's P&L flips
* ATR(14)                     -- how far this name typically moves in one session

An "alert" here is a level that was crossed at the last close or sits within
one ATR of it. It is a fact about distance, not an instruction. Nothing here
watches the tape live: the sheet is built from official closes after the
session and read during the next one.

Pure standard library. No network, no broker, no order path.
"""

from __future__ import annotations

import csv
import html
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_mainline2 as m2  # noqa: E402
import build_positions as bpos  # noqa: E402
import build_prep as prep  # noqa: E402

INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
SITE = ROOT / "watch"
EXCLUDED = {"2886"}
ORDER = {"出": 0, "抱-held": 1, "抱-nothold": 2, "進": 3}


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any(row.values())]


def card_entry_date(strategy: str, code: str, history: list[dict[str, str]]) -> date | None:
    """First card day this (strategy, name) appeared as 進 or 抱 in the current run."""
    rows = [r for r in history if r["strategy_id"].strip() == strategy and r["stock_code"].strip() == code]
    if not rows:
        return None
    # walk backwards from the newest row until the membership breaks
    rows.sort(key=lambda r: r["asof_date"])
    start = rows[-1]
    for prev, cur in zip(reversed(rows[:-1]), reversed(rows)):
        gap = (datetime.strptime(cur["asof_date"], "%Y-%m-%d").date() - datetime.strptime(prev["asof_date"], "%Y-%m-%d").date()).days
        if gap > 5 or prev["signal"].strip().startswith("出"):
            break
        start = prev
    d = datetime.strptime(start["asof_date"], "%Y-%m-%d").date()
    return d


def sleeve_cumulative(card_returns: list[dict[str, str]]) -> dict[str, list[tuple[date, float]]]:
    """Chain the card's printed header returns day by day: (1+r_t)/(1+r_{t-1}) compounding is
    not what the card means -- the header is 'members vs their entries', so the only honest
    cumulative series is the header itself, kept as a path. We return that path."""
    out: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for row in card_returns:
        try:
            out[row["strategy_id"].strip()].append(
                (datetime.strptime(row["asof_date"], "%Y-%m-%d").date(), float(row["display_return_pct"]))
            )
        except (KeyError, ValueError):
            continue
    return {k: sorted(v) for k, v in out.items()}


def analyse_row(sig: dict[str, str], held: dict[str, dict[str, str]], bars, fills, history) -> dict[str, Any] | None:
    code = sig["stock_code"].strip()
    if code in EXCLUDED or code not in bars:
        return None
    series = bars[code]
    last = series[-1]
    close = last["close"]
    entry = float(sig["entry_price"]) if sig.get("entry_price") else None
    signal = sig["signal"].strip()
    since = card_entry_date(sig["strategy_id"].strip(), code, history)
    path = [b for b in series if since and b["date"] >= since] or [last]
    peak = max(b["high"] for b in path)
    peak_close = max(b["close"] for b in path)
    trough = min(b["low"] for b in path)
    closes = [b["close"] for b in series]
    ma20 = bpos.sma(closes, 20)
    low20 = min(b["low"] for b in series[-20:])
    atr = bpos.atr(series)
    pos = held.get(code)
    row: dict[str, Any] = {
        "code": code, "name": sig["stock_name"].strip(), "strategy": sig["strategy_id"].strip(),
        "signal": signal, "effective": sig.get("effective_date", ""), "entry": entry, "close": close,
        "vs_entry": (close / entry - 1) * 100 if entry else None,
        "since": since, "peak": peak, "peak_close": peak_close, "trough": trough,
        "from_peak": (close / peak_close - 1) * 100 if peak_close else None,
        "ma20": ma20, "low20": low20, "atr": atr, "atr_pct": atr / close * 100 if atr else None,
        "held": pos is not None,
        "shares": float(pos["shares"]) if pos else None,
        "cost": float(pos["avg_cost"]) if pos else None,
        "pnl": float(pos["unrealized_pnl_twd"]) if pos else None,
        "ret": float(pos["unrealized_return_pct"]) if pos else None,
        "day_chg": (close / series[-2]["close"] - 1) * 100 if len(series) > 1 else None,
    }
    # alerts: crossed at the close, or within one ATR
    alerts: list[str] = []
    band = atr or 0.0
    if entry and close < entry:
        alerts.append(f"低於卡片進場價 {entry:g}（{row['vs_entry']:+.1f}%）")
    elif entry and close - entry <= band:
        alerts.append(f"距卡片進場價 {entry:g} 不到一個 ATR")
    if row["from_peak"] is not None and atr and (peak_close - close) >= 2 * atr:
        alerts.append(f"自進場後高點 {peak_close:g} 回撤 {row['from_peak']:+.1f}%（≥ 2 ATR）")
    if close < low20:
        alerts.append(f"收在 20 日低 {low20:g} 之下")
    elif close - low20 <= band:
        alerts.append(f"距 20 日低 {low20:g} 不到一個 ATR")
    if ma20 and close < ma20:
        alerts.append(f"收在 MA20 {ma20:,.2f} 之下")
    if pos and row["cost"] and close < row["cost"] and row["cost"] - close <= band:
        alerts.append(f"距你的成本 {row['cost']:,.2f} 不到一個 ATR")
    if signal.startswith("出"):
        alerts.insert(0, f"卡片出訊號，{sig.get('effective_date') or '—'} 生效")
    row["alerts"] = alerts
    kind = "出" if signal.startswith("出") else ("進" if signal == "進" else ("抱-held" if pos else "抱-nothold"))
    row["kind"] = kind
    return row


def overdue_rows(held, bars, fills, history) -> list[dict[str, Any]]:
    """Card exits still held from earlier cards (the newest card no longer lists them)."""
    sold: dict[str, list[date]] = defaultdict(list)
    for f in fills:
        if f["side"] == "SELL":
            sold[f["stock_code"]].append(f["date"])
    out = []
    seen = set()
    for r in reversed(history):
        code = r["stock_code"].strip()
        if not r["signal"].strip().startswith("出") or code in seen or code not in held or code in EXCLUDED:
            continue
        asof = datetime.strptime(r["asof_date"], "%Y-%m-%d").date()
        if any(d >= asof for d in sold[code]):
            continue
        seen.add(code)
        row = analyse_row(r, held, bars, fills, history)
        if row:
            row["kind"] = "出"
            row["alerts"] = [a for a in row["alerts"] if not a.startswith("卡片出訊號")]
            row["alerts"].insert(0, f"卡片 {r['asof_date']} 出（{r.get('effective_date') or '—'} 生效），仍持有")
            out.append(row)
    return out


# ---------------------------------------------------------------------- render


def f2(v, d=2):
    return "—" if v is None else f"{v:,.{d}f}"


def pct(v, d=1):
    return "—" if v is None else f"{v:+.{d}f}%"


def cls(v):
    return "neutral" if v is None else ("positive" if v > 0 else "negative" if v < 0 else "neutral")


def table_rows(rows: list[dict[str, Any]]) -> str:
    out = []
    for r in rows:
        held_txt = (
            f'{r["shares"]:,.0f} 股 @{r["cost"]:,.2f}<br><span class="{cls(r["pnl"])}">{r["pnl"]:+,.0f}（{r["ret"]:+.2f}%）</span>'
            if r["held"] else '<span class="neutral">未持有</span>'
        )
        alerts = "".join(f"<li>{html.escape(a)}</li>" for a in r["alerts"]) or '<li class="neutral">無</li>'
        sig = r["signal"]
        badge = f'<span class="b sell">{html.escape(sig)}</span>' if sig.startswith("出") else (f'<span class="b buy">{sig}</span>' if sig == "進" else sig)
        out.append(
            "<tr>"
            f'<td><b>{r["code"]} {html.escape(r["name"])}</b><br><small>{html.escape(m2.STRATEGY_LABELS.get(r["strategy"], r["strategy"]))} {badge}</small></td>'
            f'<td class="num">{f2(r["entry"])}<br><small>{r["since"].isoformat() if r["since"] else "—"} 起</small></td>'
            f'<td class="num"><b>{r["close"]:g}</b><br><small class="{cls(r["day_chg"])}">今日 {pct(r["day_chg"])}</small></td>'
            f'<td class="num {cls(r["vs_entry"])}">{pct(r["vs_entry"])}</td>'
            f'<td class="num">{r["peak_close"]:g}<br><small class="{cls(r["from_peak"])}">{pct(r["from_peak"])}</small></td>'
            f'<td class="num">{f2(r["low20"])} / {f2(r["ma20"])}</td>'
            f'<td class="num">{f2(r["atr"])}<br><small>{pct(r["atr_pct"])}</small></td>'
            f"<td>{held_txt}</td>"
            f'<td><ul class="al">{alerts}</ul></td>'
            "</tr>"
        )
    return "".join(out)


def sleeve_panel(paths: dict[str, list[tuple[date, float]]]) -> str:
    cards = []
    for sid, label in m2.STRATEGY_LABELS.items():
        p = paths.get(sid) or []
        if not p:
            continue
        last = p[-1][1]
        peak = max(v for _, v in p)
        first = p[0]
        cards.append(
            f'<div class="stat"><b class="{cls(last)}">{last:+.1f}%</b>'
            f'<span>{html.escape(label)} 卡片今日 · 起 {first[0].isoformat()} {first[1]:+.1f}% · 期間最高 {peak:+.1f}% · 距最高 {last - peak:+.1f}pp</span></div>'
        )
    return "".join(cards)


def build() -> tuple[Path, dict[str, Any]]:
    bars = m2.load_bars()
    fills = m2.load_fills()
    signals = read(INPUTS / "latest_strategy_signals.csv")
    history = read(INPUTS / "signal_history.csv")
    card_returns = read(INPUTS / "strategy_card_returns.csv")
    snapshot_day, held = prep.latest_holdings()
    held = {k: v for k, v in held.items() if v["category"].strip() != "融券"}

    rows = []
    for sig in signals:
        if sig["signal"].strip().startswith("("):
            continue
        r = analyse_row(sig, held, bars, fills, history)
        if r:
            rows.append(r)
    listed = {r["code"] for r in rows}
    for r in overdue_rows(held, bars, fills, history):
        if r["code"] not in listed:
            rows.append(r)
            listed.add(r["code"])
    # held names not on any card at all
    for code, pos in held.items():
        if code not in listed and code in bars:
            sig = {"strategy_id": "UNASSIGNED", "stock_code": code, "stock_name": pos["stock_name"], "signal": "不在卡上", "entry_price": ""}
            r = analyse_row(sig, held, bars, fills, history)
            if r:
                r["kind"] = "抱-held"
                r["alerts"].insert(0, "不在任何卡上")
                rows.append(r)
    rows.sort(key=lambda r: (ORDER.get(r["kind"], 9), -(len(r["alerts"])), r["code"]))

    price_asof = max(s[-1]["date"] for s in bars.values())
    signal_asof = max(datetime.strptime(r["asof_date"], "%Y-%m-%d").date() for r in signals)
    session = prep.m2.next_session(price_asof) if hasattr(prep.m2, "next_session") else None
    style = re.search(r"<style>.*?</style>", prep.TEMPLATE, re.S).group(0).replace("{{", "{").replace("}}", "}")
    extra = """<style>
ul.al{margin:0;padding-left:16px;font-size:12px}ul.al li{margin:2px 0}
table{min-width:1100px}td{vertical-align:top}
.k{display:inline-block;padding:2px 8px;border-radius:6px;background:var(--raise);font-size:12px;margin-right:6px}
</style>"""
    groups = {"出": "卡片說出（含逾期）", "抱-held": "卡片說抱，你持有", "抱-nothold": "卡片說抱，你沒有", "進": "卡片說進"}
    sections = []
    for kind, title in groups.items():
        sub = [r for r in rows if r["kind"] == kind]
        if not sub:
            continue
        sections.append(
            f'<article class="panel"><h2>{title} <small>{len(sub)} 檔</small></h2><div class="table-wrap"><table><thead><tr>'
            "<th>股票</th><th class=\"num\">卡片進場價</th><th class=\"num\">最新收盤</th><th class=\"num\">距進場</th>"
            "<th class=\"num\">進場後最高收</th><th class=\"num\">20日低 / MA20</th><th class=\"num\">ATR(14)</th><th>你的部位</th><th>警訊（收盤已跨或一個 ATR 內）</th>"
            f"</tr></thead><tbody>{table_rows(sub)}</tbody></table></div></article>"
        )
    n_alert = sum(1 for r in rows if r["alerts"])
    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>跟盤表 · {price_asof.isoformat()} 收盤後</title>
<meta name="description" content="下一個交易日的跟盤表：每一檔卡片名單與持股的進場價、進場後高點、20 日低、MA20、ATR 與已跨過的位置。純量測。">
{style}{extra}</head><body><div class="wrap">
<header>
  <div class="eyebrow">Watch sheet &middot; built from closes, read during the next session</div>
  <h1>跟盤表 · {price_asof.isoformat()} 收盤後</h1>
  <p class="lede">卡片只印「相對進場價幾 %」，沒有路徑、沒有高點、沒有回撤。這頁把記憶補回來：每一檔<b>從卡片進場那天到現在</b>走過的最高點、回撤，
  加上它自己的 20 日低與 MA20，還有一天通常走多遠（ATR）。「警訊」＝收盤已經跨過、或離不到一個 ATR 的位置。它是距離，不是指令。</p>
  <div class="meta">
    <span>卡片日 <code>{signal_asof.isoformat()}</code></span>
    <span>收盤 <code>{price_asof.isoformat()}</code></span>
    <span>庫存快照 <code>{snapshot_day.isoformat()}</code></span>
    <span>有警訊 <code>{n_alert} / {len(rows)}</code></span>
    <span><a href="../">&larr; 實際績效</a></span><span><a href="../positions/">持股體檢</a></span><span><a href="../prep/">備戰頁</a></span>
  </div>
</header>
<div class="notice"><b>這頁不看盤。</b>它用官方收盤算出明天要盯的位置；盤中價格請你自己對。3055 融券不在卡上，不列。兆豐（2886）依指示不計。
庫存用的是最後一次貼給我的股數（{snapshot_day.isoformat()}）——之後有買賣，貼成交或庫存，這頁才會對。</div>

<article class="panel"><h2>四張卡自己的路徑</h2>
<div class="sub">卡片表頭是「成員相對各自進場價」的平均，它本身就是一條路徑。這裡把每張卡從第一天到今天的表頭排成序列，看它的最高點與現在的距離。</div>
<div class="stat-row">{sleeve_panel(sleeve_cumulative(card_returns))}</div></article>

{"".join(sections)}

<footer>資料：inputs/latest_strategy_signals.csv、signal_history.csv、strategy_card_returns.csv、price_history.csv、holdings_snapshot_{snapshot_day.isoformat()}.csv、actual_fills.csv。
沒有券商連線、沒有委託路徑、沒有買賣價格建議。</footer>
</div></body></html>
"""
    SITE.mkdir(parents=True, exist_ok=True)
    target = SITE / "index.html"
    target.write_text(page, encoding="utf-8", newline="\n")
    receipt = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "signal_asof": signal_asof.isoformat(), "price_asof": price_asof.isoformat(), "snapshot_asof": snapshot_day.isoformat(),
        "rows": len(rows), "rows_with_alerts": n_alert,
        "alerts": {r["code"]: r["alerts"] for r in rows if r["alerts"]},
        "boundaries": ["NO_LIVE_FEED", "NO_PRICE_RECOMMENDATION", "NO_BROKER_LOGIN", "NO_ORDER_PATH"],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "watch_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return target, receipt


if __name__ == "__main__":
    target, receipt = build()
    print(f"SUCCESS: {target}")
    print(f"ROWS: {receipt['rows']} ALERTS: {receipt['rows_with_alerts']}")
