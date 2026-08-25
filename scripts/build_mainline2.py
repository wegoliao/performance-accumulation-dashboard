"""Mainline 2: the signal names the owner did NOT buy.

Mainline 1 is the money lane -- 22 fills, four NT$500k sleeves, a real cost
basis. Mainline 2 is everything the four strategy cards printed that never
became a position. It is a paper track and nothing else: no sleeve, no cash,
no order path, and every number here is descriptive.

What it answers:

* Which card members have zero shares anywhere in the account.
* Where each of those names has actually traded over the last six months --
  the volume-at-price profile, its point of control, and its value area.
* Where today's close and the card's stated entry price sit inside that
  profile, as a percentile.
* How much of a NT$50k slot each name's average daily volume could absorb.
* For mainline 1, where the owner's real fills landed inside the day's own
  high-low range -- the only empirical statement this repo can make about
  entry timing, because it is measured from settled fills.

What it deliberately does NOT do: recommend a price, rank the names by
attractiveness, or forecast anything. A volume profile is a record of where
trades happened, not a prediction of where they will happen.

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
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mainline2_template  # noqa: E402  (local module, loaded by path)

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
SITE = ROOT / "mainline2"

PRICE_PATH = INPUTS / "price_history.csv"
WATCHLIST_PATH = INPUTS / "watchlist.csv"
SIGNALS_PATH = INPUTS / "latest_strategy_signals.csv"
FILLS_PATH = INPUTS / "actual_fills.csv"

PROFILE_BINS = 44
VALUE_AREA = 0.70
SLOT_TWD = 50_000.0          # one NT$500k sleeve spread over ten names
PARTICIPATION_CAP = 0.05     # 5% of average daily volume
AVG_VOLUME_DAYS = 20

STRATEGY_LABELS = {
    "TRUST": "投信",
    "YOY": "YOY",
    "MARGIN": "融資",
    "BREAKOUT": "突破",
}


class BuildError(ValueError):
    """Input contract violation that must fail closed."""


# --------------------------------------------------------------------- loading


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise BuildError(f"missing required input: {path.name}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any(row.values())]


def to_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_bars() -> dict[str, list[dict[str, Any]]]:
    """OHLCV bars per stock, ascending by date. Rows without volume are kept."""
    bars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_csv(PRICE_PATH):
        code = (row.get("stock_code") or "").strip()
        close = to_float(row.get("close"))
        if not code or close is None or close <= 0:
            continue
        low = to_float(row.get("low")) or close
        high = to_float(row.get("high")) or close
        if high < low:
            low, high = high, low
        bars[code].append(
            {
                "date": datetime.strptime(row["asof_date"].strip(), "%Y-%m-%d").date(),
                "open": to_float(row.get("open")) or close,
                "high": high,
                "low": low,
                "close": close,
                "volume": to_float(row.get("volume")) or 0.0,
            }
        )
    return {code: sorted(rows, key=lambda r: r["date"]) for code, rows in bars.items()}


def load_watchlist() -> list[dict[str, str]]:
    return [row for row in read_csv(WATCHLIST_PATH) if row.get("track") == "MAINLINE2"]


def load_signals() -> tuple[date, dict[tuple[str, str], dict[str, Any]]]:
    rows = read_csv(SIGNALS_PATH)
    asof = max(datetime.strptime(r["asof_date"].strip(), "%Y-%m-%d").date() for r in rows)
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if datetime.strptime(row["asof_date"].strip(), "%Y-%m-%d").date() != asof:
            continue
        latest[(row["strategy_id"].strip(), row["stock_code"].strip())] = {
            "stock_name": row.get("stock_name", "").strip(),
            "industry": row.get("industry", "").strip(),
            "entry_price": to_float(row.get("entry_price")),
            "entry_display": (row.get("entry_display") or "").strip(),
            "close": to_float(row.get("close")),
            "signed_return_pct": to_float(row.get("signed_return_pct")),
            "signal": (row.get("signal") or "").strip(),
            "quality_note": (row.get("quality_note") or "").strip(),
        }
    return asof, latest


def load_fills() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_csv(FILLS_PATH):
        rows.append(
            {
                "strategy_id": row["strategy_id"].strip(),
                "stock_code": row["stock_code"].strip(),
                "stock_name": row.get("stock_name", "").strip(),
                "side": row["side"].strip().upper(),
                "date": datetime.strptime(row["fill_date"].strip(), "%Y-%m-%d").date(),
                "price": float(str(row["fill_price"]).replace(",", "")),
                "shares": float(str(row["shares"]).replace(",", "")),
            }
        )
    return rows


# ------------------------------------------------------------------- profiling


def volume_profile(bars: Sequence[dict[str, Any]], bins: int = PROFILE_BINS) -> dict[str, Any]:
    """Volume-at-price built by spreading each daily bar across its own range.

    This is an approximation and is labelled as one on the page: the exchange
    publishes a daily bar, not a tick tape, so the only honest assumption is
    that the day's volume was spread evenly between its low and its high.
    Intraday concentration is invisible here, which is exactly why the page
    never calls the point of control a "fair price".
    """
    if not bars:
        raise BuildError("cannot profile an empty bar series")
    low = min(bar["low"] for bar in bars)
    high = max(bar["high"] for bar in bars)
    if high <= low:
        high = low * 1.0001 + 0.0001
    width = (high - low) / bins
    edges = [low + index * width for index in range(bins + 1)]
    volume = [0.0] * bins

    for bar in bars:
        span = bar["high"] - bar["low"]
        if span <= 0 or bar["volume"] <= 0:
            index = min(int((bar["close"] - low) / width), bins - 1)
            volume[max(index, 0)] += bar["volume"]
            continue
        for index in range(bins):
            overlap = min(edges[index + 1], bar["high"]) - max(edges[index], bar["low"])
            if overlap > 0:
                volume[index] += bar["volume"] * overlap / span

    total = sum(volume)
    if total <= 0:
        raise BuildError("profile has no volume")

    poc_index = max(range(bins), key=lambda index: volume[index])
    low_index = high_index = poc_index
    covered = volume[poc_index]
    while covered < total * VALUE_AREA and (low_index > 0 or high_index < bins - 1):
        below = volume[low_index - 1] if low_index > 0 else -1.0
        above = volume[high_index + 1] if high_index < bins - 1 else -1.0
        if above >= below:
            high_index += 1
            covered += volume[high_index]
        else:
            low_index -= 1
            covered += volume[low_index]

    return {
        "low": low,
        "high": high,
        "width": width,
        "edges": edges,
        "volume": volume,
        "total": total,
        "poc": (edges[poc_index] + edges[poc_index + 1]) / 2.0,
        "poc_index": poc_index,
        "value_low": edges[low_index],
        "value_high": edges[high_index + 1],
        "value_share": covered / total,
        "bars": len(bars),
        "first": bars[0]["date"].isoformat(),
        "last": bars[-1]["date"].isoformat(),
    }


def percentile_of(profile: dict[str, Any], price: float) -> float | None:
    """Share of profiled volume that traded below ``price``."""
    if price is None:
        return None
    if price <= profile["low"]:
        return 0.0
    if price >= profile["high"]:
        return 1.0
    below = 0.0
    for index, edge in enumerate(profile["edges"][:-1]):
        upper = profile["edges"][index + 1]
        if price >= upper:
            below += profile["volume"][index]
        elif price > edge:
            below += profile["volume"][index] * (price - edge) / (upper - edge)
            break
        else:
            break
    return below / profile["total"]


def range_position(bar: dict[str, Any], price: float) -> float | None:
    """Where ``price`` sits inside a single day's own high-low range (0..1)."""
    span = bar["high"] - bar["low"]
    if span <= 0:
        return None
    return max(0.0, min(1.0, (price - bar["low"]) / span))


def capacity(bars: Sequence[dict[str, Any]], price: float) -> dict[str, Any]:
    recent = [bar for bar in bars[-AVG_VOLUME_DAYS:] if bar["volume"] > 0]
    if not recent or price <= 0:
        return {"avg_volume": None, "slot_shares": None, "participation": None,
                "capacity_twd": None}
    avg_volume = sum(bar["volume"] for bar in recent) / len(recent)
    slot_shares = SLOT_TWD / price
    return {
        "avg_volume": avg_volume,
        "slot_shares": slot_shares,
        "participation": slot_shares / avg_volume if avg_volume else None,
        "capacity_twd": avg_volume * PARTICIPATION_CAP * price,
        "days": len(recent),
    }


# ----------------------------------------------------------------- composition


def held_positions(fills: Sequence[dict[str, Any]]) -> dict[str, float]:
    held: dict[str, float] = defaultdict(float)
    for fill in fills:
        held[fill["stock_code"]] += fill["shares"] * (1 if fill["side"] == "BUY" else -1)
    return {code: shares for code, shares in held.items() if shares > 1e-9}


def build_roster(
    signals: dict[tuple[str, str], dict[str, Any]],
    bars: dict[str, list[dict[str, Any]]],
    held: dict[str, float],
) -> list[dict[str, Any]]:
    """Every card member with zero shares anywhere in the account."""
    roster: list[dict[str, Any]] = []
    for (strategy_id, code), signal in sorted(signals.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if code in held:
            continue
        series = bars.get(code)
        if not series:
            roster.append({"strategy_id": strategy_id, "stock_code": code,
                           "status": "NO_PRICE_HISTORY", **signal})
            continue
        profile = volume_profile(series)
        last = series[-1]
        entry = signal["entry_price"]
        roster.append(
            {
                "strategy_id": strategy_id,
                "stock_code": code,
                "status": "OK",
                "profile": profile,
                "last_bar": last,
                "close": last["close"],
                "close_pct": percentile_of(profile, last["close"]),
                "entry_pct": percentile_of(profile, entry) if entry else None,
                "in_value_area": profile["value_low"] <= last["close"] <= profile["value_high"],
                "poc_gap": last["close"] / profile["poc"] - 1.0,
                "capacity": capacity(series, last["close"]),
                **signal,
            }
        )
    return roster


def fill_landings(
    fills: Sequence[dict[str, Any]], bars: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Where each real fill landed inside its own day's high-low range."""
    landings: list[dict[str, Any]] = []
    for fill in sorted(fills, key=lambda row: (row["date"], row["stock_code"])):
        series = bars.get(fill["stock_code"], [])
        bar = next((row for row in series if row["date"] == fill["date"]), None)
        if bar is None:
            landings.append({**fill, "status": "NO_BAR", "position": None})
            continue
        landings.append(
            {
                **fill,
                "status": "OK",
                "bar": bar,
                "position": range_position(bar, fill["price"]),
                "vs_close": fill["price"] / bar["close"] - 1.0,
                "vs_open": fill["price"] / bar["open"] - 1.0,
            }
        )
    return landings


# --------------------------------------------------------------------- render


def fmt(value: float | None, digits: int = 2, dash: str = "—") -> str:
    if value is None:
        return dash
    return f"{value:,.{digits}f}"


def pct(value: float | None, sign: bool = False, digits: int = 1, dash: str = "—") -> str:
    if value is None:
        return dash
    return f"{value * 100:{'+' if sign else ''}.{digits}f}%"


def value_class(value: float | None) -> str:
    if value is None:
        return "neutral"
    return "positive" if value > 0 else ("negative" if value < 0 else "neutral")


def ladder_svg(row: dict[str, Any], width: int = 300, height: int = 250) -> str:
    """UI A -- price ladder: volume bars stacked on a vertical price axis."""
    profile = row["profile"]
    bins = len(profile["volume"])
    peak = max(profile["volume"]) or 1.0
    bar_height = height / bins
    parts: list[str] = []

    top = profile["high"]
    span = profile["high"] - profile["low"]

    def y_of(price: float) -> float:
        return (top - price) / span * height

    parts.append(
        f'<rect x="0" y="{y_of(profile["value_high"]):.1f}" width="{width}" '
        f'height="{max(y_of(profile["value_low"]) - y_of(profile["value_high"]), 1):.1f}" '
        'class="va"/>'
    )
    for index, volume in enumerate(profile["volume"]):
        bar_width = volume / peak * (width - 60)
        y = height - (index + 1) * bar_height
        cls = "bin poc" if index == profile["poc_index"] else "bin"
        parts.append(
            f'<rect x="0" y="{y:.1f}" width="{bar_width:.1f}" '
            f'height="{max(bar_height - 0.6, 0.6):.1f}" class="{cls}"/>'
        )
    for price, cls, label in (
        (profile["poc"], "poc-line", "POC"),
        (row["close"], "now-line", "現價"),
        (row["entry_price"], "entry-line", "訊號"),
    ):
        if price is None:
            continue
        y = y_of(price)
        if not (0 <= y <= height):
            continue
        parts.append(f'<line x1="0" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" class="{cls}"/>')
        parts.append(
            f'<text x="{width - 2}" y="{max(y - 3, 9):.1f}" class="tag {cls}-t" '
            f'text-anchor="end">{label} {price:,.2f}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="ladder" '
        f'preserveAspectRatio="none" role="img">{"".join(parts)}</svg>'
    )


def rail_svg(row: dict[str, Any], width: int = 560, height: int = 40) -> str:
    """UI B -- entry rail: one horizontal lane per name, six-month range."""
    profile = row["profile"]
    span = profile["high"] - profile["low"]
    mid = height / 2

    def x_of(price: float) -> float:
        return (price - profile["low"]) / span * width

    parts = [f'<line x1="0" y1="{mid}" x2="{width}" y2="{mid}" class="rail"/>']
    parts.append(
        f'<rect x="{x_of(profile["value_low"]):.1f}" y="{mid - 9:.1f}" '
        f'width="{max(x_of(profile["value_high"]) - x_of(profile["value_low"]), 1):.1f}" '
        f'height="18" class="va"/>'
    )
    parts.append(
        f'<line x1="{x_of(profile["poc"]):.1f}" y1="{mid - 13}" '
        f'x2="{x_of(profile["poc"]):.1f}" y2="{mid + 13}" class="poc-line"/>'
    )
    if row["entry_price"]:
        parts.append(
            f'<circle cx="{x_of(row["entry_price"]):.1f}" cy="{mid}" r="4.5" class="entry-dot"/>'
        )
    parts.append(f'<circle cx="{x_of(row["close"]):.1f}" cy="{mid}" r="5.5" class="now-dot"/>')
    return (
        f'<svg viewBox="0 0 {width} {height}" class="rail-svg" '
        f'preserveAspectRatio="none" role="img">{"".join(parts)}</svg>'
    )


def matrix_row_svg(row: dict[str, Any], buckets: int = 20, width: int = 420, height: int = 22) -> str:
    """UI C -- heat matrix: volume share per price decile, current price marked."""
    profile = row["profile"]
    bins = len(profile["volume"])
    per = bins / buckets
    cells: list[float] = []
    for index in range(buckets):
        start = int(index * per)
        end = int((index + 1) * per)
        cells.append(sum(profile["volume"][start:end]) / profile["total"])
    peak = max(cells) or 1.0
    cell_width = width / buckets
    parts: list[str] = []
    for index, share in enumerate(cells):
        parts.append(
            f'<rect x="{index * cell_width:.1f}" y="0" width="{cell_width - 0.8:.1f}" '
            f'height="{height}" class="cell" fill-opacity="{0.08 + 0.92 * share / peak:.3f}"/>'
        )
    pos = row["close_pct"]
    if pos is not None:
        parts.append(
            f'<line x1="{pos * width:.1f}" y1="-2" x2="{pos * width:.1f}" '
            f'y2="{height + 2}" class="now-line"/>'
        )
    if row["entry_pct"] is not None:
        parts.append(
            f'<line x1="{row["entry_pct"] * width:.1f}" y1="-2" '
            f'x2="{row["entry_pct"] * width:.1f}" y2="{height + 2}" class="entry-line"/>'
        )
    return (
        f'<svg viewBox="-1 -3 {width + 2} {height + 6}" class="matrix-svg" '
        f'preserveAspectRatio="none" role="img">{"".join(parts)}</svg>'
    )


def landing_svg(landings: Sequence[dict[str, Any]], width: int = 640, height: int = 130) -> str:
    """Where real fills landed inside the day's own range, 0 = low, 1 = high."""
    usable = [row for row in landings if row.get("position") is not None]
    if not usable:
        return '<p class="muted">尚無可對照日線的成交。</p>'
    buys = [row for row in usable if row["side"] == "BUY"]
    sells = [row for row in usable if row["side"] == "SELL"]
    parts: list[str] = []
    for index in range(11):
        x = index / 10 * width
        parts.append(f'<line x1="{x:.1f}" y1="24" x2="{x:.1f}" y2="{height - 26}" class="grid"/>')
    for label, x, anchor in (("當日最低", 0, "start"), ("當日最高", width, "end")):
        parts.append(f'<text x="{x}" y="14" class="axis" text-anchor="{anchor}">{label}</text>')
    for group, y, cls in ((buys, height * 0.38, "buy-dot"), (sells, height * 0.68, "sell-dot")):
        for row in group:
            parts.append(
                f'<circle cx="{row["position"] * width:.1f}" cy="{y:.1f}" r="5" class="{cls}">'
                f'<title>{row["stock_code"]} {html.escape(row["stock_name"])} '
                f'{row["date"].isoformat()} @{row["price"]:g} · '
                f'區間位置 {row["position"] * 100:.0f}%</title></circle>'
            )
        if group:
            mean = sum(row["position"] for row in group) / len(group)
            parts.append(
                f'<line x1="{mean * width:.1f}" y1="{y - 15:.1f}" '
                f'x2="{mean * width:.1f}" y2="{y + 15:.1f}" class="mean-line"/>'
            )
            parts.append(
                f'<text x="{mean * width:.1f}" y="{y - 19:.1f}" class="axis mean-t" '
                f'text-anchor="middle">平均 {mean * 100:.0f}%</text>'
            )
    parts.append(f'<text x="0" y="{height * 0.38 + 4:.1f}" class="axis" dx="-2" '
                 f'text-anchor="end">買</text>')
    parts.append(f'<text x="0" y="{height * 0.68 + 4:.1f}" class="axis" dx="-2" '
                 f'text-anchor="end">賣</text>')
    return (
        f'<svg viewBox="-26 0 {width + 34} {height}" class="landing" role="img">'
        f'{"".join(parts)}</svg>'
    )


# ------------------------------------------------------------------ assembling


def roster_rows(roster: Sequence[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in roster:
        if item["status"] != "OK":
            rows.append(
                f'<tr><td>{html.escape(STRATEGY_LABELS.get(item["strategy_id"], ""))}</td>'
                f'<td>{item["stock_code"]} {html.escape(item.get("stock_name", ""))}</td>'
                f'<td colspan="8" class="neutral">無行情歷史，先補資料再談分價</td></tr>'
            )
            continue
        cap = item["capacity"]
        rows.append(
            "<tr>"
            f'<td>{html.escape(STRATEGY_LABELS.get(item["strategy_id"], ""))}</td>'
            f'<td><b>{item["stock_code"]}</b> {html.escape(item["stock_name"])}'
            f'<br><small>{html.escape(item["industry"])}</small></td>'
            f'<td class="num">{fmt(item["entry_price"])}</td>'
            f'<td class="num">{fmt(item["close"])}</td>'
            f'<td class="num {value_class(item["signed_return_pct"])}">'
            f'{fmt(item["signed_return_pct"], 1)}%</td>'
            f'<td class="num">{fmt(item["profile"]["poc"])}</td>'
            f'<td class="num {value_class(item["poc_gap"])}">{pct(item["poc_gap"], sign=True)}</td>'
            f'<td class="num">{pct(item["close_pct"], digits=0)}</td>'
            f'<td class="num">{"在" if item["in_value_area"] else "外"}</td>'
            f'<td class="num">{fmt(cap["avg_volume"], 0)}'
            f'<br><small>佔比 {pct(cap["participation"], digits=2)}</small></td>'
            "</tr>"
        )
    return "".join(rows)


def ladder_cards(roster: Sequence[dict[str, Any]]) -> str:
    cards: list[str] = []
    for item in roster:
        if item["status"] != "OK":
            continue
        profile = item["profile"]
        cards.append(
            '<figure class="ladder-card">'
            f'<figcaption><b>{item["stock_code"]} {html.escape(item["stock_name"])}</b>'
            f'<span class="muted">{html.escape(STRATEGY_LABELS.get(item["strategy_id"], ""))}</span>'
            "</figcaption>"
            f"{ladder_svg(item)}"
            '<div class="ladder-meta">'
            f'<span>價值區 {fmt(profile["value_low"])}–{fmt(profile["value_high"])}</span>'
            f'<span>現價分位 {pct(item["close_pct"], digits=0)}</span>'
            "</div></figure>"
        )
    return "".join(cards)


def rail_rows(roster: Sequence[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in roster:
        if item["status"] != "OK":
            continue
        profile = item["profile"]
        rows.append(
            '<div class="rail-row">'
            f'<div class="rail-name"><b>{item["stock_code"]}</b> '
            f'{html.escape(item["stock_name"])}</div>'
            f'<div class="rail-lo">{fmt(profile["low"])}</div>'
            f'<div class="rail-body">{rail_svg(item)}</div>'
            f'<div class="rail-hi">{fmt(profile["high"])}</div>'
            f'<div class="rail-pct">{pct(item["close_pct"], digits=0)}</div>'
            "</div>"
        )
    return "".join(rows)


def matrix_rows(roster: Sequence[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in roster:
        if item["status"] != "OK":
            continue
        rows.append(
            '<div class="matrix-row">'
            f'<div class="matrix-name"><b>{item["stock_code"]}</b> '
            f'{html.escape(item["stock_name"])}</div>'
            f'<div class="matrix-body">{matrix_row_svg(item)}</div>'
            f'<div class="matrix-pct">{pct(item["close_pct"], digits=0)}</div>'
            "</div>"
        )
    return "".join(rows)


def landing_table(landings: Sequence[dict[str, Any]]) -> str:
    usable = [row for row in landings if row.get("position") is not None]
    if not usable:
        return '<tr><td colspan="7" class="neutral">尚無可對照日線的成交。</td></tr>'
    rows: list[str] = []
    for row in sorted(usable, key=lambda item: item["date"], reverse=True):
        rows.append(
            "<tr>"
            f'<td>{row["date"].isoformat()}</td>'
            f'<td>{"買" if row["side"] == "BUY" else "賣"}</td>'
            f'<td>{row["stock_code"]} {html.escape(row["stock_name"])}</td>'
            f'<td class="num">{row["price"]:g}</td>'
            f'<td class="num">{row["bar"]["low"]:g} – {row["bar"]["high"]:g}</td>'
            f'<td class="num"><b>{row["position"] * 100:.0f}%</b></td>'
            f'<td class="num {value_class(row["vs_close"])}">{pct(row["vs_close"], sign=True, digits=2)}</td>'
            "</tr>"
        )
    return "".join(rows)


def landing_stats(landings: Sequence[dict[str, Any]]) -> dict[str, Any]:
    buys = [row for row in landings if row.get("position") is not None and row["side"] == "BUY"]
    sells = [row for row in landings if row.get("position") is not None and row["side"] == "SELL"]
    def mean(rows: Sequence[dict[str, Any]], key: str) -> float | None:
        return sum(row[key] for row in rows) / len(rows) if rows else None
    return {
        "buy_n": len(buys),
        "sell_n": len(sells),
        "buy_position": mean(buys, "position"),
        "sell_position": mean(sells, "position"),
        "buy_vs_close": mean(buys, "vs_close"),
    }


# ---------------------------------------------------------------------- build


def build() -> tuple[Path, dict[str, Any]]:
    bars = load_bars()
    fills = load_fills()
    signal_asof, signals = load_signals()
    held = held_positions(fills)
    roster = build_roster(signals, bars, held)
    landings = fill_landings(fills, bars)
    stats = landing_stats(landings)

    profiled = [item for item in roster if item["status"] == "OK"]
    if not profiled:
        raise BuildError("no mainline-2 name has price history; run fetch_prices first")

    price_asof = max(series[-1]["date"] for series in bars.values())
    window_first = min(item["profile"]["first"] for item in profiled)
    window_last = max(item["profile"]["last"] for item in profiled)
    profile_days = max(item["profile"]["bars"] for item in profiled)

    replacements = {
        "{{SIGNAL_ASOF}}": signal_asof.isoformat(),
        "{{PRICE_ASOF}}": price_asof.isoformat(),
        "{{PROFILE_WINDOW}}": f"{window_first} ~ {window_last}",
        "{{PROFILE_DAYS}}": str(profile_days),
        "{{ROSTER_N}}": str(len(roster)),
        "{{HELD_N}}": str(len(held)),
        "{{BUY_N}}": str(stats["buy_n"]),
        "{{BUY_POSITION}}": pct(stats["buy_position"], digits=0),
        "{{LANDING_CHART}}": landing_svg(landings),
        "{{LANDING_TABLE}}": landing_table(landings),
        "{{LADDER_CARDS}}": ladder_cards(roster),
        "{{RAIL_ROWS}}": rail_rows(roster),
        "{{MATRIX_ROWS}}": matrix_rows(roster),
        "{{ROSTER_ROWS}}": roster_rows(roster),
        "{{GENERATED_AT}}": datetime.now().isoformat(timespec="seconds"),
    }

    page = mainline2_template.TEMPLATE
    for marker, value in replacements.items():
        page = page.replace(marker, value)
    leftover = [marker for marker in replacements if marker in page]
    if leftover:
        raise BuildError(f"unsubstituted markers remain: {leftover}")

    SITE.mkdir(parents=True, exist_ok=True)
    target = SITE / "index.html"
    target.write_text(page, encoding="utf-8", newline="\n")

    receipt = {
        "generated_at": replacements["{{GENERATED_AT}}"],
        "signal_asof": signal_asof.isoformat(),
        "price_asof": price_asof.isoformat(),
        "profile_window": [window_first, window_last],
        "mainline2_count": len(roster),
        "mainline1_positions": len(held),
        "roster": [
            {
                "strategy_id": item["strategy_id"],
                "stock_code": item["stock_code"],
                "stock_name": item.get("stock_name", ""),
                "status": item["status"],
                "close": item.get("close"),
                "entry_price": item.get("entry_price"),
                "poc": item["profile"]["poc"] if item["status"] == "OK" else None,
                "value_low": item["profile"]["value_low"] if item["status"] == "OK" else None,
                "value_high": item["profile"]["value_high"] if item["status"] == "OK" else None,
                "close_percentile": item.get("close_pct"),
                "in_value_area": item.get("in_value_area"),
                "avg_volume_20d": item["capacity"]["avg_volume"] if item["status"] == "OK" else None,
                "slot_participation": item["capacity"]["participation"] if item["status"] == "OK" else None,
            }
            for item in roster
        ],
        "fill_landing": {
            "buy_count": stats["buy_n"],
            "sell_count": stats["sell_n"],
            "buy_mean_range_position": stats["buy_position"],
            "sell_mean_range_position": stats["sell_position"],
            "buy_mean_vs_close": stats["buy_vs_close"],
        },
        "boundaries": [
            "PAPER_TRACK_ONLY",
            "NO_BROKER_LOGIN",
            "NO_ORDER_PATH",
            "VOLUME_PROFILE_IS_DAILY_BAR_APPROXIMATION",
        ],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "mainline2_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return target, receipt


def main() -> int:
    target, receipt = build()
    print(f"SUCCESS: {target}")
    print(f"MAINLINE2: {receipt['mainline2_count']} names, profile {receipt['profile_window'][0]} ~ {receipt['profile_window'][1]}")
    landing = receipt["fill_landing"]
    if landing["buy_mean_range_position"] is not None:
        print(
            f"FILL_LANDING: {landing['buy_count']} buys, mean range position "
            f"{landing['buy_mean_range_position'] * 100:.0f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
