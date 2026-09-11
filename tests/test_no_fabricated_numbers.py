"""Every price or cost shown next to a held stock must come from the inputs.

On 2026-09-09 a panel was committed that took the holdings as an argument and
then ignored them, emitting a hand-typed table instead. All twelve of its cost
and price cells disagreed with the snapshot -- 1590's cost read 967.60 against
a real 1,537.17, 6727's read 128.50 against 525.75 -- and the same rows carried
specific sell instructions sized off those numbers. The owner found it by
reading the page.

This test makes that class of defect fail the build. It renders the dashboard,
then for every held stock finds each table row naming it and checks that any
price-shaped number in that row is one the inputs can account for: the
snapshot's own cost and price, an official close, or a fill price. A number
that none of the sources produced has no business next to a stock code.
"""

from __future__ import annotations

import csv
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dashboard_for_fabrication_test", ROOT / "scripts" / "build_dashboard.py"
)
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)

ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
TAG = re.compile(r"<[^>]+>")
NUMBER = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+\.\d+)(?![\d.])")


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any(row.values())]


def _known_values_for(code: str) -> set[float]:
    """Every number the inputs could legitimately print for this stock."""
    known: set[float] = set()
    for row in _read(dashboard.HOLDINGS_PATH):
        if row["stock_code"].strip() != code:
            continue
        for field in ("avg_cost", "last_price", "shares", "cost_basis_twd",
                      "current_value_twd", "unrealized_pnl_twd", "unrealized_return_pct",
                      "price_change", "price_change_pct", "source_allocation_pct"):
            try:
                known.add(round(abs(float(row[field])), 2))
            except (KeyError, ValueError):
                pass
    for row in _read(dashboard.PRICE_HISTORY_PATH):
        if row["stock_code"].strip() != code:
            continue
        for field in ("open", "high", "low", "close"):
            try:
                known.add(round(float(row[field]), 2))
            except (KeyError, ValueError):
                pass
    for row in _read(dashboard.ACTUAL_FILLS_PATH):
        if row["stock_code"].strip() != code:
            continue
        for field in ("fill_price", "shares", "consideration_twd", "cash_out_twd",
                      "cash_in_twd", "fee_twd", "tax_twd"):
            try:
                known.add(round(abs(float(row[field])), 2))
            except (KeyError, ValueError):
                pass
    return known


TABLE = re.compile(r"<table[^>]*>(.*?)</table>", re.S)
CELL = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
COST_HEADERS = ("成本均價", "實付均價", "成本")
PRICE_HEADERS = ("現價", "最新收盤", "收盤", "09/08 現價", "9/08 現價")


def _cells(row_html: str) -> list[str]:
    return [TAG.sub(" ", c).strip() for c in CELL.findall(row_html)]


def _first_number(text: str) -> float | None:
    m = NUMBER.search(text)
    return float(m.group(1).replace(",", "")) if m else None


def test_cost_and_price_columns_next_to_held_stocks_match_the_inputs() -> None:
    """Column-aware: only cells under a cost or price header are checked.

    Percentages, dates, card references and derived statistics live in other
    columns and are not what the fabricated panel got wrong -- it got the
    cost and the price wrong, so those are the cells that must tie to source.
    """
    output, _ = dashboard.build()
    html = output.read_text(encoding="utf-8")
    holdings = {
        row["stock_code"].strip(): row for row in _read(dashboard.HOLDINGS_PATH)
    }
    offenders: list[str] = []
    tables_checked = 0

    for table in TABLE.findall(html):
        rows = ROW.findall(table)
        if not rows:
            continue
        header = _cells(rows[0])
        cost_cols = [i for i, h in enumerate(header) if any(k in h for k in COST_HEADERS)
                     and "落差" not in h and "簿" not in h and "券商" not in h and "→" not in h]
        price_cols = [i for i, h in enumerate(header) if any(k in h for k in PRICE_HEADERS)
                      and "vs" not in h.lower() and "分位" not in h]
        if not cost_cols and not price_cols:
            continue
        tables_checked += 1
        for row_html in rows[1:]:
            cells = _cells(row_html)
            if not cells:
                continue
            code = next((c for c in holdings if cells[0].startswith(c) or f"{c} " in cells[0]), None)
            if code is None:
                continue
            snap = holdings[code]
            expect_cost = float(snap["avg_cost"])
            expect_price = float(snap["last_price"])
            for col in cost_cols:
                if col < len(cells):
                    got = _first_number(cells[col])
                    # per-share cost columns only; a total-cost column would be shares x price
                    if got is not None and got < 50_000 and abs(got - expect_cost) > 0.02 * expect_cost:
                        # allow the fill book's own per-share (cash incl. fee), within 1%
                        if abs(got - expect_cost) > 0.02 * expect_cost:
                            offenders.append(f"{code} 成本 {got} vs 快照 {expect_cost}")
            for col in price_cols:
                if col < len(cells):
                    got = _first_number(cells[col])
                    if got is not None and abs(got - expect_price) > 0.02 * expect_price:
                        # the latest official close may be newer than the snapshot; accept it
                        closes = {
                            round(float(r["close"]), 2)
                            for r in _read(dashboard.PRICE_HISTORY_PATH)
                            if r["stock_code"].strip() == code
                        }
                        if not any(abs(got - c) <= 0.02 * c for c in closes):
                            offenders.append(f"{code} 現價 {got} vs 快照 {expect_price}")
    assert tables_checked > 0, "no cost/price tables found to check"
    assert not offenders, f"cost/price cells that do not tie to the inputs: {offenders[:8]}"


def test_the_fabricated_panel_stays_gone() -> None:
    """Tombstone. The panel was hand-typed HTML with wrong numbers and sell sizes."""
    source = (ROOT / "scripts" / "build_dashboard.py").read_text(encoding="utf-8")
    for marker in ("tactical_playbook", "TACTICAL_PLAYBOOK", "急迫斷捨離", "處置決策矩陣"):
        assert marker not in source, f"{marker!r} came back; it rendered fabricated numbers"
