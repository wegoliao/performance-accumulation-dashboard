"""Inline SVG chart renderers for the 66 performance lane.

Standard library only. Every function returns an SVG (or HTML) fragment that is
embedded directly in the offline dashboard, so the page keeps working with no
network, no CDN and no JavaScript charting library.
"""

from __future__ import annotations

import html
import math
from datetime import date
from typing import Any, Sequence

PALETTE = ["#57d3a2", "#f5bd58", "#72a7ff", "#d689ff", "#ff7f7f", "#4dd0c8"]
GREEN = "#57d3a2"
RED = "#ff7f7f"
GOLD = "#f5bd58"
BLUE = "#72a7ff"
MUTED = "#899791"


def empty_panel(status: str, *lines: str) -> str:
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    return (
        '<div class="empty-chart"><div class="empty-icon">↗</div>'
        f"<b>{html.escape(status)}</b>{body}</div>"
    )


def _nice_bounds(values: Sequence[float], pad_ratio: float = 0.10) -> tuple[float, float]:
    low, high = min(values), max(values)
    if math.isclose(low, high):
        pad = abs(low) * 0.05 or 1.0
        return low - pad, high + pad
    pad = (high - low) * pad_ratio
    return low - pad, high + pad


def _robust_bounds(values: Sequence[float]) -> tuple[float, float]:
    """Bounds that frame the bulk of the data instead of the extremes.

    Uses the inter-quartile range so one runaway holding cannot flatten the
    rest of the plot. Callers are expected to clamp and flag anything outside.
    """
    ordered = sorted(values)
    if len(ordered) < 4:
        return _nice_bounds(values, 0.14)

    def quantile(fraction: float) -> float:
        position = fraction * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    q1, q3 = quantile(0.25), quantile(0.75)
    spread = q3 - q1
    if math.isclose(spread, 0.0):
        return _nice_bounds(values, 0.14)
    low = max(min(ordered), q1 - 1.5 * spread)
    high = min(max(ordered), q3 + 1.5 * spread)
    pad = (high - low) * 0.16
    return low - pad, high + pad


def _fmt_axis(value: float, percent: bool) -> str:
    return f"{value * 100:.1f}%" if percent else f"{value:,.1f}"


# ------------------------------------------------------------------ line/area


def line_chart(
    series: dict[str, Sequence[tuple[date, float]]],
    *,
    percent: bool = False,
    height: int = 320,
    fill_first: bool = False,
    reference: float | None = None,
    label: str = "chart",
) -> str:
    usable = {name: list(points) for name, points in series.items() if len(points) >= 2}
    if not usable:
        return empty_panel("WAITING_HISTORY", "資料點不足兩筆，無法畫出曲線。")

    all_points = [point for points in usable.values() for point in points]
    dates = sorted({point[0] for point in all_points})
    min_date, max_date = dates[0], dates[-1]
    span_days = max((max_date - min_date).days, 1)
    values = [point[1] for point in all_points]
    if reference is not None:
        values = values + [reference]
    low, high = _nice_bounds(values)

    width = 960
    left, top, right, bottom = 64, 22, 18, 40
    plot_w, plot_h = width - left - right, height - top - bottom

    def x_of(day: date) -> float:
        return left + ((day - min_date).days / span_days) * plot_w

    def y_of(value: float) -> float:
        return top + (high - value) / (high - low) * plot_h

    parts: list[str] = []
    for i in range(5):
        y = top + plot_h * i / 4
        value = high - (high - low) * i / 4
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" class="grid-line"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis-text" text-anchor="end">'
            f"{html.escape(_fmt_axis(value, percent))}</text>"
        )
    if reference is not None and low < reference < high:
        y = y_of(reference)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" class="ref-line"/>'
        )

    legend: list[str] = []
    for index, (name, points) in enumerate(usable.items()):
        color = PALETTE[index % len(PALETTE)]
        coords = [f"{x_of(day):.1f},{y_of(value):.1f}" for day, value in points]
        if fill_first and index == 0:
            baseline = y_of(max(low, reference if reference is not None else low))
            parts.append(
                f'<polygon points="{x_of(points[0][0]):.1f},{baseline:.1f} '
                + " ".join(coords)
                + f' {x_of(points[-1][0]):.1f},{baseline:.1f}" fill="{color}" opacity="0.10"/>'
            )
        parts.append(
            f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" '
            'stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        last_day, last_value = points[-1]
        parts.append(
            f'<circle cx="{x_of(last_day):.1f}" cy="{y_of(last_value):.1f}" r="3.5" fill="{color}"/>'
        )
        legend.append(
            f'<span><i style="background:{color}"></i>{html.escape(name)} '
            f'<b>{html.escape(_fmt_axis(points[-1][1], percent))}</b></span>'
        )

    # Quarterly x-axis ticks keep long windows readable.
    ticks: list[date] = []
    for day in dates:
        if not ticks or (day.year, (day.month - 1) // 3) != (ticks[-1].year, (ticks[-1].month - 1) // 3):
            ticks.append(day)
    for day in ticks:
        parts.append(
            f'<text x="{x_of(day):.1f}" y="{height - 12}" class="axis-text" text-anchor="middle">'
            f"{day:%Y-%m}</text>"
        )

    return (
        '<div class="chart-legend">' + "".join(legend) + "</div>"
        f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(label)}">' + "".join(parts) + "</svg>"
    )


def underwater_chart(series: Sequence[tuple[date, float]], height: int = 200) -> str:
    points = list(series)
    if len(points) < 2:
        return empty_panel("WAITING_HISTORY", "需要至少兩日 equity curve 才能畫回撤。")
    width = 960
    left, top, right, bottom = 64, 18, 18, 34
    plot_w, plot_h = width - left - right, height - top - bottom
    min_date, max_date = points[0][0], points[-1][0]
    span_days = max((max_date - min_date).days, 1)
    worst = min(value for _, value in points)
    low = min(worst * 1.12, -0.005)

    def x_of(day: date) -> float:
        return left + ((day - min_date).days / span_days) * plot_w

    def y_of(value: float) -> float:
        return top + (0.0 - value) / (0.0 - low) * plot_h

    coords = [f"{x_of(day):.1f},{y_of(value):.1f}" for day, value in points]
    parts = [
        f'<polygon points="{x_of(min_date):.1f},{top:.1f} ' + " ".join(coords)
        + f' {x_of(max_date):.1f},{top:.1f}" fill="{RED}" opacity="0.20"/>',
        f'<polyline points="{" ".join(coords)}" fill="none" stroke="{RED}" stroke-width="2"/>',
    ]
    for i in range(4):
        y = top + plot_h * i / 3
        value = 0.0 - (0.0 - low) * i / 3
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" class="grid-line"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis-text" text-anchor="end">{value * 100:.1f}%</text>'
        )
    trough_day, trough_value = min(points, key=lambda item: item[1])
    parts.append(
        f'<circle cx="{x_of(trough_day):.1f}" cy="{y_of(trough_value):.1f}" r="4" fill="{RED}"/>'
        f'<text x="{x_of(trough_day):.1f}" y="{y_of(trough_value) - 10:.1f}" class="axis-text" '
        f'text-anchor="middle" fill="{RED}">{trough_value * 100:.2f}% · {trough_day:%m/%d}</text>'
    )
    return (
        f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" aria-label="水下回撤圖">'
        + "".join(parts)
        + "</svg>"
    )


# --------------------------------------------------------------------- heatmap


MONTH_LABELS = ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"]


def monthly_heatmap(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return empty_panel("WAITING_HISTORY", "需要跨月的 equity curve 才能拆出月報酬。")
    extreme = max(abs(row["return"]) for row in rows) or 0.01
    by_year: dict[int, dict[int, float]] = {}
    for row in rows:
        by_year.setdefault(row["year"], {})[row["month"]] = row["return"]

    header = "".join(f"<th>{label}</th>" for label in MONTH_LABELS)
    body: list[str] = []
    for year in sorted(by_year):
        cells: list[str] = []
        compounded = 1.0
        for month in range(1, 13):
            value = by_year[year].get(month)
            if value is None:
                cells.append('<td class="heat-cell heat-empty">·</td>')
                continue
            compounded *= 1.0 + value
            intensity = min(abs(value) / extreme, 1.0)
            color = GREEN if value >= 0 else RED
            cells.append(
                f'<td class="heat-cell" style="background:{color}{int(intensity * 200 + 30):02x}">'
                f"{value * 100:+.1f}%</td>"
            )
        year_return = compounded - 1.0
        klass = "positive" if year_return >= 0 else "negative"
        body.append(
            f'<tr><th class="heat-year">{year}</th>{"".join(cells)}'
            f'<td class="heat-total {klass}">{year_return * 100:+.1f}%</td></tr>'
        )
    return (
        '<div class="table-wrap"><table class="heatmap"><thead><tr><th></th>'
        + header
        + "<th>年度</th></tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


# --------------------------------------------------------------------- scatter


def risk_return_scatter(points: Sequence[dict[str, Any]], benchmark: dict[str, Any] | None = None) -> str:
    usable = [row for row in points if row.get("volatility") and row.get("annualised_return") is not None]
    if len(usable) < 2:
        return empty_panel("WAITING_MIN_20_RETURNS", "每檔至少 20 筆日報酬才估年化波動度。")
    width, height = 960, 380
    left, top, right, bottom = 66, 22, 22, 46
    plot_w, plot_h = width - left - right, height - top - bottom

    xs = [row["volatility"] for row in usable]
    ys = [row["annualised_return"] for row in usable]
    if benchmark and benchmark.get("volatility"):
        xs.append(benchmark["volatility"])
        ys.append(benchmark["annualised_return"])
    x_low, x_high = _nice_bounds(xs, 0.14)
    # A single 10-bagger annualises into the hundreds of percent and would
    # squash every other holding onto the floor. Frame the plot on the robust
    # middle of the distribution and pin the outliers to the edge instead,
    # keeping their true value visible in the label rather than hiding it.
    y_low, y_high = _robust_bounds(ys + [0.0])
    x_low = max(x_low, 0.0)
    max_value = max(row["end_value"] for row in usable) or 1.0

    def x_of(value: float) -> float:
        return left + (value - x_low) / (x_high - x_low) * plot_w

    def y_of(value: float) -> float:
        return top + (y_high - value) / (y_high - y_low) * plot_h

    parts: list[str] = []
    for i in range(5):
        y = top + plot_h * i / 4
        value = y_high - (y_high - y_low) * i / 4
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" class="grid-line"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis-text" text-anchor="end">{value * 100:.0f}%</text>'
        )
        x = left + plot_w * i / 4
        x_value = x_low + (x_high - x_low) * i / 4
        parts.append(
            f'<text x="{x:.1f}" y="{height - 22}" class="axis-text" text-anchor="middle">{x_value * 100:.0f}%</text>'
        )
    if y_low < 0 < y_high:
        parts.append(
            f'<line x1="{left}" y1="{y_of(0):.1f}" x2="{width - right}" y2="{y_of(0):.1f}" class="ref-line"/>'
        )

    clamped: list[str] = []
    for row in sorted(usable, key=lambda item: item["end_value"], reverse=True):
        radius = 6 + 22 * math.sqrt(row["end_value"] / max_value)
        color = GREEN if row["annualised_return"] >= 0 else RED
        actual = row["annualised_return"]
        shown = min(max(actual, y_low), y_high)
        off_scale = not math.isclose(shown, actual)
        cx, cy = x_of(row["volatility"]), y_of(shown)
        tooltip = (
            f'{html.escape(row["stock_code"])} {html.escape(row["stock_name"])} · '
            f'年化 {actual * 100:+.1f}% · 波動 {row["volatility"] * 100:.1f}% · '
            f'部位 NT$ {row["end_value"]:,.0f}'
            + ("（超出座標範圍，已標在邊緣）" if off_scale else "")
        )
        if off_scale:
            # Pin to the edge as a triangle carrying its real number, so an
            # off-scale holding reads as "beyond the axis", never as "at the top".
            size = max(radius * 0.72, 7.0)
            tip = cy - size if actual > shown else cy + size
            parts.append(
                f'<polygon points="{cx:.1f},{tip:.1f} {cx - size:.1f},{cy:.1f} {cx + size:.1f},{cy:.1f}" '
                f'fill="{color}" opacity="0.30" stroke="{color}" stroke-width="1.6">'
                f"<title>{tooltip}</title></polygon>"
                f'<text x="{cx:.1f}" y="{cy + (14 if actual < shown else -size - 4):.1f}" '
                f'class="scatter-label" text-anchor="middle" fill="{color}">'
                f'{html.escape(row["stock_code"])} {actual * 100:+,.0f}%</text>'
            )
            clamped.append(f'{row["stock_code"]} {row["stock_name"]} {actual * 100:+,.0f}%')
            continue
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius:.1f}" fill="{color}" opacity="0.24" '
            f'stroke="{color}" stroke-width="1.4"><title>{tooltip}</title></circle>'
            f'<text x="{cx:.1f}" y="{cy + 3.5:.1f}" class="scatter-label" text-anchor="middle">'
            f'{html.escape(row["stock_code"])}</text>'
        )
    if benchmark and benchmark.get("volatility"):
        cx, cy = x_of(benchmark["volatility"]), y_of(benchmark["annualised_return"])
        parts.append(
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="none" stroke="{GOLD}" stroke-width="2.4"/>'
            f'<text x="{cx:.1f}" y="{cy - 13:.1f}" class="axis-text" text-anchor="middle" fill="{GOLD}">'
            f'{html.escape(benchmark["label"])}</text>'
        )
    parts.append(
        f'<text x="{left + plot_w / 2:.1f}" y="{height - 6}" class="axis-text" text-anchor="middle">'
        "年化波動度 →</text>"
    )
    note = (
        f'<div class="chart-legend"><span>▲ 超出座標範圍（真實值已標在圖上）：'
        f'{html.escape("、".join(clamped))}</span></div>'
        if clamped
        else ""
    )
    return (
        note
        + f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" aria-label="風險報酬散布圖">'
        + "".join(parts)
        + "</svg>"
    )


# ------------------------------------------------------------------- histogram


def histogram_chart(bins: Sequence[dict[str, Any]], mean: float | None = None) -> str:
    if not bins:
        return empty_panel("WAITING_HISTORY", "需要足夠日報酬才能畫分布。")
    width, height = 960, 240
    left, top, right, bottom = 46, 18, 18, 40
    plot_w, plot_h = width - left - right, height - top - bottom
    peak = max(row["count"] for row in bins) or 1
    slot = plot_w / len(bins)
    parts: list[str] = []
    for index, row in enumerate(bins):
        bar_h = row["count"] / peak * plot_h
        x = left + index * slot
        color = GREEN if row["low"] >= 0 else RED
        parts.append(
            f'<rect x="{x + 1.5:.1f}" y="{top + plot_h - bar_h:.1f}" width="{slot - 3:.1f}" '
            f'height="{bar_h:.1f}" fill="{color}" opacity="0.72" rx="2"><title>'
            f'{row["low"] * 100:+.2f}% ~ {row["high"] * 100:+.2f}%：{row["count"]} 天</title></rect>'
        )
    zero_ratio = None
    low, high = bins[0]["low"], bins[-1]["high"]
    if low < 0 < high:
        zero_ratio = (0.0 - low) / (high - low)
        x = left + zero_ratio * plot_w
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" class="ref-line"/>')
    if mean is not None and low < mean < high:
        x = left + (mean - low) / (high - low) * plot_w
        parts.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="{GOLD}" '
            'stroke-width="1.6" stroke-dasharray="4 3"/>'
            f'<text x="{x:.1f}" y="{top - 4}" class="axis-text" text-anchor="middle" fill="{GOLD}">'
            f"平均 {mean * 100:+.2f}%</text>"
        )
    parts.append(
        f'<line x1="{left}" y1="{top + plot_h:.1f}" x2="{width - right}" y2="{top + plot_h:.1f}" class="grid-line"/>'
        f'<text x="{left}" y="{height - 14}" class="axis-text">{low * 100:+.2f}%</text>'
        f'<text x="{width - right}" y="{height - 14}" class="axis-text" text-anchor="end">{high * 100:+.2f}%</text>'
    )
    return (
        f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" aria-label="日報酬分布">'
        + "".join(parts)
        + "</svg>"
    )


# ------------------------------------------------------------------- waterfall


def waterfall_chart(rows: Sequence[dict[str, Any]], total_label: str = "合計") -> str:
    items = [row for row in rows if row.get("value_change_twd") is not None]
    if not items:
        return empty_panel("WAITING_PRICE_HISTORY", "需要期初與期末價格才能拆解貢獻。")
    ordered = sorted(items, key=lambda row: row["value_change_twd"], reverse=True)
    total = sum(row["value_change_twd"] for row in ordered)

    running = 0.0
    steps: list[tuple[str, float, float, float]] = []
    for row in ordered:
        start = running
        running += row["value_change_twd"]
        steps.append((f'{row["stock_code"]} {row["stock_name"]}', start, running, row["value_change_twd"]))

    levels = [0.0, total] + [value for _, start, value, _ in steps for value in (start, value)]
    low, high = _nice_bounds(levels, 0.12)
    width, height = 960, 320
    left, top, right, bottom = 76, 20, 18, 74
    plot_w, plot_h = width - left - right, height - top - bottom
    slot = plot_w / (len(steps) + 1)

    def y_of(value: float) -> float:
        return top + (high - value) / (high - low) * plot_h

    parts: list[str] = []
    for i in range(5):
        y = top + plot_h * i / 4
        value = high - (high - low) * i / 4
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" class="grid-line"/>'
            f'<text x="{left - 8}" y="{y + 4:.1f}" class="axis-text" text-anchor="end">{value / 1000:,.0f}k</text>'
        )
    if low < 0 < high:
        parts.append(
            f'<line x1="{left}" y1="{y_of(0):.1f}" x2="{width - right}" y2="{y_of(0):.1f}" class="ref-line"/>'
        )

    for index, (name, start, end, delta) in enumerate(steps):
        x = left + index * slot
        top_y, bottom_y = y_of(max(start, end)), y_of(min(start, end))
        bar_h = max(bottom_y - top_y, 1.6)
        color = GREEN if delta >= 0 else RED
        parts.append(
            f'<rect x="{x + slot * 0.16:.1f}" y="{top_y:.1f}" width="{slot * 0.68:.1f}" '
            f'height="{bar_h:.1f}" fill="{color}" opacity="0.82" rx="2"><title>'
            f"{html.escape(name)}：NT$ {delta:+,.0f}</title></rect>"
            f'<text class="waterfall-label" transform="translate({x + slot * 0.5:.1f},'
            f'{height - 60}) rotate(48)">{html.escape(name)}</text>'
        )
    x = left + len(steps) * slot
    total_color = GOLD
    top_y, bottom_y = y_of(max(total, 0.0)), y_of(min(total, 0.0))
    parts.append(
        f'<rect x="{x + slot * 0.16:.1f}" y="{top_y:.1f}" width="{slot * 0.68:.1f}" '
        f'height="{max(bottom_y - top_y, 1.6):.1f}" fill="{total_color}" opacity="0.9" rx="2"><title>'
        f"{html.escape(total_label)}：NT$ {total:+,.0f}</title></rect>"
        f'<text class="waterfall-label" transform="translate({x + slot * 0.5:.1f},{height - 60}) rotate(48)">'
        f"{html.escape(total_label)}</text>"
    )
    return (
        f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" aria-label="個股貢獻瀑布圖">'
        + "".join(parts)
        + "</svg>"
    )


# ------------------------------------------------------------------------ misc


def correlation_bars(rows: Sequence[dict[str, Any]]) -> str:
    usable = [row for row in rows if row.get("beta") is not None]
    if not usable:
        return empty_panel("WAITING_MIN_20_RETURNS", "每檔至少 20 筆日報酬才估 Beta。")
    ordered = sorted(usable, key=lambda row: row["beta"], reverse=True)
    peak = max(abs(row["beta"]) for row in ordered) or 1.0
    rendered: list[str] = []
    for row in ordered:
        beta = row["beta"]
        width = abs(beta) / peak * 48.0
        offset = 50.0 if beta >= 0 else 50.0 - width
        klass = "positive" if beta >= 0 else "negative"
        corr = row.get("correlation")
        corr_text = f"ρ {corr:+.2f}" if corr is not None else "ρ n/a"
        rendered.append(
            '<div class="bar-row">'
            f'<div class="bar-label"><b>{html.escape(row["stock_code"])}</b> {html.escape(row["stock_name"])}</div>'
            '<div class="bar-track"><span class="bar-axis"></span>'
            f'<span class="bar-fill {klass}" style="left:{offset:.2f}%;width:{width:.2f}%"></span></div>'
            f'<div class="bar-value">β {beta:+.2f}<br><small>{corr_text}</small></div>'
            "</div>"
        )
    return "".join(rendered)
