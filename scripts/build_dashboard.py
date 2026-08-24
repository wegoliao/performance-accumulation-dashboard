"""Build the self-contained offline performance accumulation dashboard.

This side lane is intentionally read-only. It reads owner-supplied CSV files,
performs deterministic calculations, and writes static HTML plus a receipt.
It has no broker, credential, network, or order capability.
"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analytics  # noqa: E402  (local module, loaded by path)
import charts  # noqa: E402


TRADING_DAYS = 252
MIN_RISK_RETURN_OBS = 20
ROLLING_WINDOW = analytics.ROLLING_WINDOW
ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
HOLDINGS_PATH = INPUTS / "holdings_snapshot_2026-08-24.csv"
SUMMARY_PATH = INPUTS / "snapshot_summary_2026-08-24.csv"
ACCOUNT_NAV_PATH = INPUTS / "account_nav.csv"
STRATEGY_NAV_PATH = INPUTS / "strategy_nav.csv"
BENCHMARK_NAV_PATH = INPUTS / "benchmark_nav.csv"
PRICE_HISTORY_PATH = INPUTS / "price_history.csv"
LEDGER_PATH = INPUTS / "positions_ledger.csv"
ACTUAL_FILLS_PATH = INPUTS / "actual_fills.csv"
STRATEGY_CARD_PATH = INPUTS / "strategy_card_returns.csv"
STRATEGY_MARKS_PATH = INPUTS / "strategy_position_marks.csv"
BENCHMARK_LABELS = {"TAIEX": "加權指數", "0050": "0050 元大台灣50"}
STRATEGY_LABELS = {
    "TRUST": "投信",
    "YOY": "YOY",
    "MARGIN": "融資",
    "BREAKOUT": "突破",
}
STRATEGY_BUDGET_TWD = 500_000.0
EXPECTED_CARD_MEMBERS_2026_08_20 = {
    "TRUST": 5,
    "YOY": 6,
    "MARGIN": 10,
    "BREAKOUT": 4,
}


class InputError(ValueError):
    """Input contract violation that must fail closed."""


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle) if any(row.values())]


def required_float(value: str | None, field: str) -> float:
    if value is None or str(value).strip() == "":
        raise InputError(f"{field} is required")
    try:
        number = float(str(value).replace(",", ""))
    except ValueError as exc:
        raise InputError(f"{field} must be numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise InputError(f"{field} must be finite")
    return number


def optional_float(value: str | None, field: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return required_float(value, field)


def parse_date(value: str, field: str = "asof_date") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{field} must be YYYY-MM-DD: {value!r}") from exc


def load_holdings(path: Path = HOLDINGS_PATH) -> list[dict[str, Any]]:
    rows = read_csv(path)
    if not rows:
        raise InputError("holdings snapshot is empty")
    holdings: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_date: date | None = None
    for raw in rows:
        code = raw["stock_code"].strip()
        if not code or code in seen:
            raise InputError(f"stock_code must be unique and non-empty: {code!r}")
        seen.add(code)
        asof = parse_date(raw["asof_date"])
        if expected_date is None:
            expected_date = asof
        elif asof != expected_date:
            raise InputError("one holdings snapshot cannot mix asof dates")
        row: dict[str, Any] = dict(raw)
        row.update(
            {
                "asof_date": asof,
                "shares": required_float(raw["shares"], f"{code}.shares"),
                "yesterday_shares": required_float(
                    raw["yesterday_shares"], f"{code}.yesterday_shares"
                ),
                "current_value_twd": required_float(
                    raw["current_value_twd"], f"{code}.current_value_twd"
                ),
                "cost_basis_twd": required_float(
                    raw["cost_basis_twd"], f"{code}.cost_basis_twd"
                ),
                "avg_cost": required_float(raw["avg_cost"], f"{code}.avg_cost"),
                "last_price": required_float(
                    raw["last_price"], f"{code}.last_price"
                ),
                "price_change": required_float(
                    raw["price_change"], f"{code}.price_change"
                ),
                "price_change_pct": required_float(
                    raw["price_change_pct"], f"{code}.price_change_pct"
                ),
                "unrealized_pnl_twd": required_float(
                    raw["unrealized_pnl_twd"], f"{code}.unrealized_pnl_twd"
                ),
                "source_allocation_pct": optional_float(
                    raw.get("source_allocation_pct"), f"{code}.source_allocation_pct"
                ),
                "unrealized_return_pct": required_float(
                    raw["unrealized_return_pct"], f"{code}.unrealized_return_pct"
                ),
            }
        )
        if row["shares"] < 0 or row["current_value_twd"] < 0 or row["cost_basis_twd"] < 0:
            raise InputError(f"{code} contains a negative long-only balance")
        row["estimated_daily_price_contribution_twd"] = (
            row["shares"] * row["price_change"]
        )
        holdings.append(row)
    return holdings


def load_summary(path: Path = SUMMARY_PATH) -> dict[str, Any]:
    rows = read_csv(path)
    if len(rows) != 1:
        raise InputError("snapshot summary must contain exactly one row")
    raw = rows[0]
    numeric_fields = (
        "shares",
        "yesterday_shares",
        "current_value_twd",
        "buy_value_today_twd",
        "sell_value_today_twd",
        "cost_basis_twd",
        "unrealized_pnl_twd",
        "unrealized_return_pct",
    )
    result: dict[str, Any] = dict(raw)
    result["asof_date"] = parse_date(raw["asof_date"])
    for field in numeric_fields:
        result[field] = required_float(raw[field], f"summary.{field}")
    return result


def snapshot_analytics(
    holdings: list[dict[str, Any]], summary: dict[str, Any]
) -> dict[str, Any]:
    totals = {
        "shares": sum(row["shares"] for row in holdings),
        "yesterday_shares": sum(row["yesterday_shares"] for row in holdings),
        "current_value_twd": sum(row["current_value_twd"] for row in holdings),
        "cost_basis_twd": sum(row["cost_basis_twd"] for row in holdings),
        "unrealized_pnl_twd": sum(row["unrealized_pnl_twd"] for row in holdings),
    }
    for field in totals:
        if not math.isclose(totals[field], summary[field], abs_tol=0.01):
            raise InputError(
                f"snapshot subtotal mismatch for {field}: "
                f"rows={totals[field]} source={summary[field]}"
            )
    if not math.isclose(
        totals["cost_basis_twd"] + totals["unrealized_pnl_twd"],
        totals["current_value_twd"],
        abs_tol=0.01,
    ):
        raise InputError("cost basis + P/L does not equal source current value")
    total_value = totals["current_value_twd"]
    for row in holdings:
        row["calculated_allocation_pct"] = (
            row["current_value_twd"] / total_value * 100 if total_value else 0.0
        )
    weights = [row["current_value_twd"] / total_value for row in holdings]
    hhi = sum(weight * weight for weight in weights)
    winning = sum(row["unrealized_pnl_twd"] > 0 for row in holdings)
    losing = sum(row["unrealized_pnl_twd"] < 0 for row in holdings)
    gross_current_mark_value = sum(
        row["shares"] * row["last_price"] for row in holdings
    )
    estimated_daily_price_contribution = sum(
        row["estimated_daily_price_contribution_twd"] for row in holdings
    )
    gross_previous_mark_value = (
        gross_current_mark_value - estimated_daily_price_contribution
    )
    return {
        **totals,
        "positions": len(holdings),
        "unrealized_return": (
            totals["unrealized_pnl_twd"] / totals["cost_basis_twd"]
            if totals["cost_basis_twd"]
            else None
        ),
        "estimated_daily_price_contribution_twd": estimated_daily_price_contribution,
        "estimated_gross_daily_return": (
            estimated_daily_price_contribution / gross_previous_mark_value
            if gross_previous_mark_value > 0
            else None
        ),
        "gross_current_mark_value_twd": gross_current_mark_value,
        "gross_previous_mark_value_twd": gross_previous_mark_value,
        "max_weight": max(weights),
        "hhi": hhi,
        "effective_positions": 1.0 / hhi if hhi else None,
        "winning_positions": winning,
        "losing_positions": losing,
        "winning_position_ratio": winning / len(holdings),
        "source_subtotal_return": summary["unrealized_return_pct"] / 100.0,
        "reconciliation": "PASS",
    }


def load_account_nav(path: Path = ACCOUNT_NAV_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in read_csv(path):
        rows.append(
            {
                "date": parse_date(raw["asof_date"]),
                "value": required_float(
                    raw["portfolio_value_twd"], "portfolio_value_twd"
                ),
                "flow": required_float(
                    raw.get("external_cash_flow_twd") or "0",
                    "external_cash_flow_twd",
                ),
                "scope": raw.get("scope", ""),
                "note": raw.get("note", ""),
            }
        )
    rows.sort(key=lambda row: row["date"])
    dates = [row["date"] for row in rows]
    if len(set(dates)) != len(dates):
        raise InputError("account_nav contains duplicate dates")
    if any(row["value"] <= 0 for row in rows):
        raise InputError("portfolio_value_twd must be positive")
    return rows


def build_twr(rows: list[dict[str, Any]]) -> list[tuple[date, float]]:
    if not rows:
        return []
    curve = [(rows[0]["date"], 100.0)]
    previous_value = rows[0]["value"]
    index = 100.0
    for row in rows[1:]:
        daily_return = (row["value"] - row["flow"]) / previous_value - 1.0
        if not math.isfinite(daily_return) or daily_return <= -1.0:
            raise InputError(f"invalid TWR return on {row['date']}: {daily_return}")
        index *= 1.0 + daily_return
        curve.append((row["date"], index))
        previous_value = row["value"]
    return curve


def load_grouped_levels(path: Path, group_field: str, value_field: str) -> dict[str, list[tuple[date, float]]]:
    grouped: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for raw in read_csv(path):
        group = raw[group_field].strip()
        if not group:
            raise InputError(f"{path.name}.{group_field} is required")
        grouped[group].append(
            (
                parse_date(raw["asof_date"]),
                required_float(raw[value_field], f"{path.name}.{value_field}"),
            )
        )
    for group, values in grouped.items():
        values.sort(key=lambda item: item[0])
        if len({item[0] for item in values}) != len(values):
            raise InputError(f"{path.name} has duplicate date for {group}")
        if any(item[1] <= 0 for item in values):
            raise InputError(f"{path.name} levels must be positive for {group}")
    return dict(grouped)


def normalize_curve(curve: list[tuple[date, float]]) -> list[tuple[date, float]]:
    if not curve:
        return []
    base = curve[0][1]
    return [(day, value / base * 100.0) for day, value in curve]


def load_actual_fills(path: Path = ACTUAL_FILLS_PATH) -> list[dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in read_csv(path):
        trade_id = raw["trade_id"].strip()
        if not trade_id or trade_id in seen:
            raise InputError(f"actual_fills.trade_id must be unique: {trade_id!r}")
        seen.add(trade_id)
        strategy_id = raw["strategy_id"].strip()
        if strategy_id not in STRATEGY_LABELS:
            raise InputError(f"unknown four-strategy id: {strategy_id!r}")
        side = raw["side"].strip().upper()
        if side not in {"BUY", "SELL"}:
            raise InputError(f"{trade_id}.side must be BUY or SELL")
        row = {
            **raw,
            "strategy_id": strategy_id,
            "side": side,
            "date": parse_date(raw["fill_date"], "fill_date"),
            "shares": required_float(raw["shares"], f"{trade_id}.shares"),
            "price": required_float(raw["fill_price"], f"{trade_id}.fill_price"),
            "cash_out": required_float(raw["cash_out_twd"], f"{trade_id}.cash_out_twd"),
            "cash_in": required_float(raw["cash_in_twd"], f"{trade_id}.cash_in_twd"),
        }
        if row["shares"] <= 0 or row["price"] <= 0:
            raise InputError(f"{trade_id} shares and fill price must be positive")
        fills.append(row)
    fills.sort(key=lambda row: (row["date"], row["trade_id"]))
    return fills


def load_strategy_cards(
    path: Path = STRATEGY_CARD_PATH,
) -> dict[str, list[tuple[date, float]]]:
    curves: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for raw in read_csv(path):
        strategy_id = raw["strategy_id"].strip()
        if strategy_id not in STRATEGY_LABELS:
            raise InputError(f"unknown strategy card id: {strategy_id!r}")
        # Source card percentage is a displayed cumulative return of its held
        # members.  It is a level, not a daily return, so never compound it.
        level = 100.0 + required_float(
            raw["display_return_pct"], "display_return_pct"
        )
        curves[strategy_id].append((parse_date(raw["asof_date"]), level))
    for strategy_id, curve in curves.items():
        curve.sort(key=lambda item: item[0])
        if len({item[0] for item in curve}) != len(curve):
            raise InputError(f"strategy card has duplicate date for {strategy_id}")
    return dict(curves)


def load_supplemental_marks(
    path: Path = STRATEGY_MARKS_PATH,
) -> dict[date, dict[str, float]]:
    marks: dict[date, dict[str, float]] = defaultdict(dict)
    for raw in read_csv(path):
        day = parse_date(raw["asof_date"])
        code = raw["stock_code"].strip()
        if code in marks[day]:
            raise InputError(f"duplicate supplemental mark: {day} {code}")
        marks[day][code] = required_float(raw["close"], f"{day}.{code}.close")
    return dict(marks)


def estimated_liquidation_value(shares: float, close: float) -> float:
    """Mirror the broker screen's estimated fee + 0.3% transaction tax."""
    gross = shares * close
    return gross - int(gross * 0.001425) - int(gross * 0.003)


def build_four_strategy_actual(
    fills: list[dict[str, Any]],
    prices: dict[str, list[tuple[date, float]]],
    holdings: list[dict[str, Any]],
    asof: date,
    snapshot_asof: date,
) -> tuple[dict[str, list[tuple[date, float]]], list[tuple[date, float]], dict[str, Any]]:
    if not fills:
        raise InputError("actual_fills is empty; four-strategy actual curve unavailable")
    first_fill = min(row["date"] for row in fills)
    baseline = first_fill - timedelta(days=1)
    price_by_day = {
        day: {code: value for code, points in prices.items() for point_day, value in points if point_day == day}
        for day in sorted({point_day for points in prices.values() for point_day, _ in points})
    }
    trade_days = sorted(day for day in price_by_day if baseline <= day <= asof)
    if baseline not in trade_days:
        trade_days.insert(0, baseline)
    supplemental = load_supplemental_marks()
    source_holdings = {row["stock_code"]: row for row in holdings}
    curves: dict[str, list[tuple[date, float]]] = {}
    diagnostics: dict[str, Any] = {}

    active_by_strategy: dict[str, dict[str, float]] = {}
    for strategy_id in STRATEGY_LABELS:
        cash = STRATEGY_BUDGET_TWD
        positions: dict[str, float] = defaultdict(float)
        curve: list[tuple[date, float]] = []
        for day in trade_days:
            for fill in fills:
                if fill["date"] != day or fill["strategy_id"] != strategy_id:
                    continue
                code = fill["stock_code"].strip()
                if fill["side"] == "BUY":
                    cash -= fill["cash_out"]
                    positions[code] += fill["shares"]
                else:
                    if positions[code] + 1e-9 < fill["shares"]:
                        raise InputError(f"{strategy_id} sells more {code} than held")
                    cash += fill["cash_in"]
                    positions[code] -= fill["shares"]

            liquidation = 0.0
            for code, shares in positions.items():
                if shares <= 1e-9:
                    continue
                if day == snapshot_asof and code in source_holdings:
                    source = source_holdings[code]
                    liquidation += (
                        source["current_value_twd"] * shares / source["shares"]
                    )
                    continue
                close = price_by_day.get(day, {}).get(code)
                if close is None:
                    close = supplemental.get(day, {}).get(code)
                if close is None:
                    raise InputError(f"missing close for active {strategy_id} {code} on {day}")
                liquidation += estimated_liquidation_value(shares, close)
            curve.append((day, (cash + liquidation) / STRATEGY_BUDGET_TWD * 100.0))
        curves[strategy_id] = curve
        active_by_strategy[strategy_id] = {
            code: shares for code, shares in positions.items() if shares > 1e-9
        }
        diagnostics[strategy_id] = {
            "cash_twd": cash,
            "liquidation_value_twd": curve[-1][1] / 100.0 * STRATEGY_BUDGET_TWD - cash,
            "active_positions": active_by_strategy[strategy_id],
        }

    common_dates = sorted(set.intersection(*(set(dict(curve)) for curve in curves.values())))
    bundle = [
        (
            day,
            sum(dict(curves[strategy_id])[day] for strategy_id in STRATEGY_LABELS)
            / len(STRATEGY_LABELS),
        )
        for day in common_dates
    ]

    reconstructed: dict[str, float] = defaultdict(float)
    for positions in active_by_strategy.values():
        for code, shares in positions.items():
            reconstructed[code] += shares
    expected = {
        row["stock_code"]: row["shares"]
        for row in holdings
        if row["stock_code"] != "2886"
    }
    if reconstructed != expected:
        raise InputError(
            f"four-strategy active shares do not reconcile: fills={dict(reconstructed)} source={expected}"
        )
    diagnostics["reconciliation"] = "PASS_EXCLUDING_UNASSIGNED_2886"
    diagnostics["unassigned"] = {"2886": 1.0}
    diagnostics["last_fill_date"] = max(row["date"] for row in fills).isoformat()
    diagnostics["valuation_asof"] = asof.isoformat()
    diagnostics["valuation_basis"] = (
        "OWNER_SNAPSHOT_NET_LIQUIDATION"
        if asof == snapshot_asof
        else "OFFICIAL_CLOSE_ESTIMATED_LIQUIDATION_CARRY_FORWARD_POSITIONS"
    )
    diagnostics["bundle_current_pnl_twd"] = (
        bundle[-1][1] / 100.0 * STRATEGY_BUDGET_TWD * len(STRATEGY_LABELS)
        - STRATEGY_BUDGET_TWD * len(STRATEGY_LABELS)
    )
    return curves, bundle, diagnostics


def slice_and_normalize(
    curve: list[tuple[date, float]], start: date, end: date
) -> list[tuple[date, float]]:
    selected = [(day, value) for day, value in curve if start <= day <= end]
    return normalize_curve(selected)


def strategy_comparison_table(
    actual_curves: dict[str, list[tuple[date, float]]],
    card_curves: dict[str, list[tuple[date, float]]],
    diagnostics: dict[str, Any],
) -> str:
    common_asof = min(curve[-1][0] for curve in card_curves.values())
    rows: list[str] = []
    for strategy_id, label in STRATEGY_LABELS.items():
        actual = dict(actual_curves[strategy_id])
        card = dict(card_curves[strategy_id])
        actual_common = actual[common_asof] / 100.0 - 1.0
        theory_common = card[common_asof] / 100.0 - 1.0
        gap = actual_common - theory_common
        current = actual_curves[strategy_id][-1][1] / 100.0 - 1.0
        pnl = current * STRATEGY_BUDGET_TWD
        metrics = performance_metrics(actual_curves[strategy_id])
        actual_members = len(diagnostics[strategy_id]["active_positions"])
        expected_members = EXPECTED_CARD_MEMBERS_2026_08_20[strategy_id]
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(label)}</b><br><small>{strategy_id}</small></td>"
            f'<td class="num {css_value_class(current)}">{fmt_pct(current, sign=True)}</td>'
            f'<td class="num {css_value_class(pnl)}">NT$ {fmt_ntd(pnl, sign=True)}</td>'
            f'<td class="num {css_value_class(actual_common)}">{fmt_pct(actual_common, sign=True)}</td>'
            f'<td class="num {css_value_class(theory_common)}">{fmt_pct(theory_common, sign=True)}</td>'
            f'<td class="num {css_value_class(gap)}">{fmt_pct(gap, sign=True)}</td>'
            f'<td class="num">{actual_members}/{expected_members}</td>'
            f'<td class="num {css_value_class(metrics["max_drawdown"] or 0)}">{fmt_pct(metrics["max_drawdown"])}</td>'
            '<td class="num neutral">N/A<br><small>&lt;20 日報酬</small></td>'
            "</tr>"
        )
    return "".join(rows)


def returns_from_curve(curve: list[tuple[date, float]]) -> list[float]:
    return [curve[i][1] / curve[i - 1][1] - 1.0 for i in range(1, len(curve))]


def trailing_returns(curve: list[tuple[date, float]]) -> dict[str, float | None]:
    """Latest trading-observation windows plus full cumulative return."""
    windows = {"今日": 1, "近一週": 5, "近一月": 21, "近一季": 63}
    result: dict[str, float | None] = {}
    for label, observations in windows.items():
        result[label] = (
            curve[-1][1] / curve[-1 - observations][1] - 1.0
            if len(curve) > observations
            else None
        )
    # Taiwan can have fewer than 252 observations in a calendar year. Select
    # the closest available close around one year ago and require near-complete
    # calendar coverage so a partial history is never labelled as one year.
    result["近一年"] = None
    if len(curve) >= 2 and (curve[-1][0] - curve[0][0]).days >= 350:
        target = curve[-1][0] - timedelta(days=365)
        anchor = min(curve[:-1], key=lambda item: abs((item[0] - target).days))
        if abs((anchor[0] - target).days) <= 14:
            result["近一年"] = curve[-1][1] / anchor[1] - 1.0
    result["YTD"] = period_to_date(curve, "year")
    result["累計"] = (
        curve[-1][1] / curve[0][1] - 1.0 if len(curve) >= 2 else None
    )
    return result


def period_to_date(curve: list[tuple[date, float]], frequency: str) -> float | None:
    if len(curve) < 2:
        return None
    latest_day, latest_value = curve[-1]
    if frequency == "week":
        start = latest_day.fromisocalendar(latest_day.isocalendar().year, latest_day.isocalendar().week, 1)
    elif frequency == "month":
        start = latest_day.replace(day=1)
    elif frequency == "quarter":
        start = latest_day.replace(month=((latest_day.month - 1) // 3) * 3 + 1, day=1)
    elif frequency == "year":
        start = latest_day.replace(month=1, day=1)
    else:
        raise ValueError(f"unsupported frequency: {frequency}")
    previous = [value for day, value in curve if day < start]
    if not previous:
        return latest_value / curve[0][1] - 1.0 if curve[0][0] < latest_day else None
    return latest_value / previous[-1] - 1.0


def period_end_returns(
    curve: list[tuple[date, float]], frequency: str
) -> list[tuple[str, float]]:
    """Returns between consecutive weekly/monthly/quarterly/yearly closes."""
    if not curve:
        return []

    def key(day: date) -> str:
        if frequency == "week":
            iso = day.isocalendar()
            return f"{iso.year}-W{iso.week:02d}"
        if frequency == "month":
            return f"{day.year}-{day.month:02d}"
        if frequency == "quarter":
            return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
        if frequency == "year":
            return str(day.year)
        raise ValueError(f"unsupported frequency: {frequency}")

    period_ends: list[tuple[str, float]] = []
    for day, value in curve:
        period = key(day)
        if period_ends and period_ends[-1][0] == period:
            period_ends[-1] = (period, value)
        else:
            period_ends.append((period, value))
    return [
        (period_ends[index][0], period_ends[index][1] / period_ends[index - 1][1] - 1.0)
        for index in range(1, len(period_ends))
    ]


def performance_metrics(curve: list[tuple[date, float]]) -> dict[str, float | None]:
    empty = {
        "total_return": None,
        "cagr": None,
        "volatility": None,
        "sharpe": None,
        "sortino": None,
        "max_drawdown": None,
        "calmar": None,
        "worst_day": None,
        "positive_day_ratio": None,
    }
    if len(curve) < 2:
        return empty
    values = [item[1] for item in curve]
    returns = returns_from_curve(curve)
    elapsed_days = (curve[-1][0] - curve[0][0]).days
    total_return = values[-1] / values[0] - 1.0
    cagr = (
        (values[-1] / values[0]) ** (365.25 / elapsed_days) - 1.0
        if elapsed_days > 0
        else None
    )
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1.0)
    result = dict(empty)
    result.update(
        {
            "total_return": total_return,
            "cagr": cagr,
            "max_drawdown": max_drawdown,
            "calmar": cagr / abs(max_drawdown) if cagr is not None and max_drawdown < 0 else None,
            "worst_day": min(returns),
            "positive_day_ratio": sum(value > 0 for value in returns) / len(returns),
        }
    )
    if len(returns) < MIN_RISK_RETURN_OBS:
        return result
    volatility = statistics.stdev(returns) * math.sqrt(TRADING_DAYS)
    mean_annual = statistics.mean(returns) * TRADING_DAYS
    downside = [value for value in returns if value < 0]
    downside_vol = (
        statistics.stdev(downside) * math.sqrt(TRADING_DAYS)
        if len(downside) >= 2
        else None
    )
    result.update(
        {
            "volatility": volatility,
            "sharpe": mean_annual / volatility if volatility > 0 else None,
            "sortino": mean_annual / downside_vol if downside_vol and downside_vol > 0 else None,
        }
    )
    return result


def relative_metrics(
    actual_curve: list[tuple[date, float]], benchmark_curve: list[tuple[date, float]]
) -> dict[str, float | None]:
    empty = {
        "alpha": None,
        "beta": None,
        "r_squared": None,
        "tracking_error": None,
        "information_ratio": None,
        "overlap_returns": None,
    }
    actual = dict(normalize_curve(actual_curve))
    benchmark = dict(normalize_curve(benchmark_curve))
    common = sorted(set(actual) & set(benchmark))
    if len(common) < MIN_RISK_RETURN_OBS + 1:
        return empty
    actual_returns = [actual[common[i]] / actual[common[i - 1]] - 1.0 for i in range(1, len(common))]
    benchmark_returns = [benchmark[common[i]] / benchmark[common[i - 1]] - 1.0 for i in range(1, len(common))]
    x_mean = statistics.mean(benchmark_returns)
    y_mean = statistics.mean(actual_returns)
    denominator = sum((value - x_mean) ** 2 for value in benchmark_returns)
    if denominator == 0:
        return empty
    beta = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(benchmark_returns, actual_returns)
    ) / denominator
    intercept_daily = y_mean - beta * x_mean
    fitted = [intercept_daily + beta * value for value in benchmark_returns]
    residual_ss = sum((y - fit) ** 2 for y, fit in zip(actual_returns, fitted))
    total_ss = sum((y - y_mean) ** 2 for y in actual_returns)
    active = [y - x for y, x in zip(actual_returns, benchmark_returns)]
    tracking_error = statistics.stdev(active) * math.sqrt(TRADING_DAYS)
    return {
        "alpha": intercept_daily * TRADING_DAYS,
        "beta": beta,
        "r_squared": 1.0 - residual_ss / total_ss if total_ss > 0 else None,
        "tracking_error": tracking_error,
        "information_ratio": (
            statistics.mean(active) * TRADING_DAYS / tracking_error
            if tracking_error > 0
            else None
        ),
        "overlap_returns": float(len(active)),
    }


def fmt_ntd(value: float, sign: bool = False) -> str:
    prefix = "+" if sign and value > 0 else ""
    return f"{prefix}{value:,.0f}"


def fmt_pct(value: float | None, sign: bool = False) -> str:
    if value is None:
        return "—"
    prefix = "+" if sign and value > 0 else ""
    return f"{prefix}{value:.2%}"


def fmt_num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def css_value_class(value: float) -> str:
    return "positive" if value > 0 else "negative" if value < 0 else "neutral"


def metric_card(label: str, value: str, note: str, value_class: str = "") -> str:
    return (
        '<article class="metric-card">'
        f'<div class="metric-label">{html.escape(label)}</div>'
        f'<div class="metric-value {value_class}">{html.escape(value)}</div>'
        f'<div class="metric-note">{html.escape(note)}</div>'
        "</article>"
    )


def status_metric(label: str, value: str, status: str, meaning: str) -> str:
    status_class = "ok" if status in {"OK", "ACTUAL_FILLS_RECONCILED"} else "waiting"
    return (
        '<div class="status-metric">'
        f'<div><span class="status-dot {status_class}"></span><b>{html.escape(label)}</b></div>'
        f'<strong>{html.escape(value)}</strong>'
        f'<small>{html.escape(status)} · {html.escape(meaning)}</small>'
        "</div>"
    )


def diverging_bars(rows: Iterable[dict[str, Any]], value_key: str, suffix: str = "") -> str:
    ordered = sorted(rows, key=lambda row: abs(row[value_key]), reverse=True)
    maximum = max((abs(row[value_key]) for row in ordered), default=1.0) or 1.0
    rendered: list[str] = []
    for row in ordered:
        value = row[value_key]
        width = abs(value) / maximum * 48.0
        left = 50.0 if value >= 0 else 50.0 - width
        rendered.append(
            '<div class="bar-row">'
            f'<div class="bar-label"><b>{html.escape(row["stock_code"])}</b> {html.escape(row["stock_name"])}</div>'
            '<div class="bar-track"><span class="bar-axis"></span>'
            f'<span class="bar-fill {css_value_class(value)}" style="left:{left:.2f}%;width:{width:.2f}%"></span></div>'
            f'<div class="bar-value {css_value_class(value)}">{html.escape(fmt_ntd(value, sign=True))}{html.escape(suffix)}</div>'
            "</div>"
        )
    return "".join(rendered)


def allocation_bars(rows: Iterable[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for row in sorted(rows, key=lambda item: item["calculated_allocation_pct"], reverse=True):
        weight = row["calculated_allocation_pct"]
        rendered.append(
            '<div class="allocation-row">'
            f'<div><b>{html.escape(row["stock_code"])}</b> {html.escape(row["stock_name"])}</div>'
            f'<div class="allocation-track"><span style="width:{weight:.2f}%"></span></div>'
            f'<strong>{weight:.2f}%</strong>'
            "</div>"
        )
    return "".join(rendered)


def period_cards(curve: list[tuple[date, float]]) -> str:
    metrics = trailing_returns(curve)
    cards: list[str] = []
    for label, value in metrics.items():
        status = "OK" if value is not None else "BASELINE_ONLY"
        cards.append(
            '<article class="period-card">'
            f'<span>{html.escape(label)}</span>'
            f'<b class="{css_value_class(value or 0)}">{html.escape(fmt_pct(value, sign=True))}</b>'
            f'<small>{status}</small></article>'
        )
    return "".join(cards)


def historical_period_bars(curve: list[tuple[date, float]]) -> str:
    candidates = [
        ("月", period_end_returns(curve, "month")),
        ("週", period_end_returns(curve, "week")),
        ("季", period_end_returns(curve, "quarter")),
        ("年", period_end_returns(curve, "year")),
    ]
    label, values = next(((label, values) for label, values in candidates if values), ("月", []))
    if not values:
        return '<div class="mini-empty">BASELINE_ONLY · 累積第二個期間後顯示歷史期間報酬長條</div>'
    recent = values[-16:]
    maximum = max(abs(value) for _, value in recent) or 1.0
    rows = []
    for period, value in recent:
        rows.append(
            '<div class="period-bar-row">'
            f'<span>{html.escape(period)}</span><div class="period-bar-track">'
            f'<i class="{css_value_class(value)}" style="width:{abs(value) / maximum * 100:.2f}%"></i></div>'
            f'<b class="{css_value_class(value)}">{fmt_pct(value, sign=True)}</b></div>'
        )
    return f'<div class="period-kind">目前顯示：{label}報酬</div>' + "".join(rows)


def monthly_heatmap(curve: list[tuple[date, float]]) -> str:
    monthly = dict(period_end_returns(curve, "month"))
    if not monthly:
        return '<div class="mini-empty">BASELINE_ONLY · 至少跨兩個月份後顯示月度熱圖</div>'
    years = sorted({int(key[:4]) for key in monthly})
    header = "".join(f"<th>{month}月</th>" for month in range(1, 13))
    rows: list[str] = []
    for year in years:
        cells = []
        for month in range(1, 13):
            value = monthly.get(f"{year}-{month:02d}")
            if value is None:
                cells.append('<td class="heat-empty">—</td>')
            else:
                intensity = min(abs(value) / 0.15, 1.0)
                color = f"rgba(87,211,162,{0.18 + intensity * 0.62:.2f})" if value >= 0 else f"rgba(255,127,127,{0.18 + intensity * 0.62:.2f})"
                cells.append(f'<td style="background:{color}" class="{css_value_class(value)}">{value:+.1%}</td>')
        rows.append(f"<tr><th>{year}</th>{''.join(cells)}</tr>")
    return f'<div class="heat-wrap"><table class="heatmap"><thead><tr><th>年</th>{header}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def drawdown_visual(curve: list[tuple[date, float]]) -> str:
    if len(curve) < 2:
        return '<div class="mini-empty">BASELINE_ONLY · 第二個每日淨值後開始堆疊回撤</div>'
    peak = curve[0][1]
    values: list[tuple[date, float]] = []
    for day, level in curve:
        peak = max(peak, level)
        values.append((day, level / peak - 1.0))
    worst = min(value for _, value in values)
    width, height = 900, 220
    left, top, right, bottom = 48, 20, 18, 34
    span = max((values[-1][0] - values[0][0]).days, 1)
    plot_w, plot_h = width - left - right, height - top - bottom
    floor = min(worst * 1.12, -0.01)
    points = []
    for day, value in values:
        x = left + (day - values[0][0]).days / span * plot_w
        y = top + (0.0 - value) / (0.0 - floor) * plot_h
        points.append(f"{x:.1f},{y:.1f}")
    area = f"{left},{top} " + " ".join(points) + f" {width-right},{top}"
    return (
        f'<div class="drawdown-head"><span>最大回撤</span><b class="negative">{worst:.2%}</b></div>'
        f'<svg class="drawdown-chart" viewBox="0 0 {width} {height}">'
        f'<line x1="{left}" y1="{top}" x2="{width-right}" y2="{top}" class="grid-line"/>'
        f'<polygon points="{area}" fill="rgba(255,127,127,.20)"/>'
        f'<polyline points="{" ".join(points)}" fill="none" stroke="#ff7f7f" stroke-width="2.5"/>'
        f'<text x="{left}" y="{height-10}" class="axis-text">{values[0][0]}</text>'
        f'<text x="{width-right}" y="{height-10}" class="axis-text" text-anchor="end">{values[-1][0]}</text></svg>'
    )


def line_chart(series: dict[str, list[tuple[date, float]]]) -> str:
    usable = {name: normalize_curve(values) for name, values in series.items() if len(values) >= 2}
    if not usable:
        return (
            '<div class="empty-chart"><div class="empty-icon">↗</div>'
            '<b>WAITING_HISTORY</b><p>目前只有 2026-08-24 單一庫存快照。</p>'
            '<p>加入至少兩日 account NAV 後才會出現累積曲線；至少 20 筆日報酬後才顯示風險統計。</p></div>'
        )
    all_points = [(day, value) for values in usable.values() for day, value in values]
    dates = sorted({point[0] for point in all_points})
    min_date, max_date = dates[0], dates[-1]
    date_span = max((max_date - min_date).days, 1)
    values = [point[1] for point in all_points]
    low, high = min(values), max(values)
    padding = max((high - low) * 0.12, 1.0)
    low -= padding
    high += padding
    width, height = 920, 300
    left, top, right, bottom = 58, 24, 20, 42
    plot_w, plot_h = width - left - right, height - top - bottom
    colors = ["#57d3a2", "#f5bd58", "#72a7ff", "#d689ff", "#ff7f7f"]
    grid: list[str] = []
    for i in range(5):
        y = top + plot_h * i / 4
        value = high - (high - low) * i / 4
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="grid-line"/>'
            f'<text x="{left-8}" y="{y+4:.1f}" class="axis-text" text-anchor="end">{value:.1f}</text>'
        )
    paths: list[str] = []
    legend: list[str] = []
    for index, (name, points) in enumerate(usable.items()):
        color = colors[index % len(colors)]
        coords: list[str] = []
        for day, value in points:
            x = left + ((day - min_date).days / date_span) * plot_w
            y = top + (high - value) / (high - low) * plot_h
            coords.append(f"{x:.1f},{y:.1f}")
        paths.append(
            f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>'
        )
        legend.append(f'<span><i style="background:{color}"></i>{html.escape(name)}</span>')
    return (
        '<div class="chart-legend">' + "".join(legend) + '</div>'
        f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" aria-label="績效累積曲線">'
        + "".join(grid)
        + "".join(paths)
        + f'<text x="{left}" y="{height-12}" class="axis-text">{min_date.isoformat()}</text>'
        + f'<text x="{width-right}" y="{height-12}" class="axis-text" text-anchor="end">{max_date.isoformat()}</text>'
        + "</svg>"
    )


def holdings_table(holdings: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for row in sorted(holdings, key=lambda item: item["current_value_twd"], reverse=True):
        pnl_class = css_value_class(row["unrealized_pnl_twd"])
        day_class = css_value_class(row["price_change_pct"])
        rows.append(
            "<tr>"
            f'<td><b>{html.escape(row["stock_code"])}</b><br><small>{html.escape(row["stock_name"])}</small></td>'
            f'<td class="num">{row["shares"]:,.0f}</td>'
            f'<td class="num">{row["avg_cost"]:,.2f}</td>'
            f'<td class="num">{row["last_price"]:,.2f}</td>'
            f'<td class="num {day_class}">{row["price_change_pct"]:+.2f}%</td>'
            f'<td class="num">{row["current_value_twd"]:,.0f}</td>'
            f'<td class="num {pnl_class}">{row["unrealized_pnl_twd"]:+,.0f}</td>'
            f'<td class="num {pnl_class}">{row["unrealized_return_pct"]:+.2f}%</td>'
            f'<td class="num">{row["calculated_allocation_pct"]:.2f}%</td>'
            "</tr>"
        )
    return "".join(rows)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> tuple[Path, dict[str, Any]]:
    holdings = load_holdings()
    source_summary = load_summary()
    snapshot = snapshot_analytics(holdings, source_summary)
    account_rows = load_account_nav()
    legacy_account_curve = build_twr(account_rows)
    legacy_strategy_curves = load_grouped_levels(STRATEGY_NAV_PATH, "strategy_id", "equity_level")
    benchmark_curves = load_grouped_levels(BENCHMARK_NAV_PATH, "benchmark_id", "level")
    prices = analytics.load_price_history(PRICE_HISTORY_PATH)
    fills = load_actual_fills()
    card_curves = load_strategy_cards()
    actual_asof = (
        max(day for day, _ in prices["TAIEX"])
        if prices.get("TAIEX")
        else max(day for points in prices.values() for day, _ in points)
    )
    actual_strategy_curves, actual_curve, strategy_diagnostics = build_four_strategy_actual(
        fills,
        prices,
        holdings,
        actual_asof,
        source_summary["asof_date"],
    )
    ledger = analytics.load_ledger(LEDGER_PATH)
    mtm_curve, mtm_diagnostics = analytics.build_mtm_curve(ledger, prices, "TAIEX")
    analysis_curve = actual_curve
    analysis_basis = "ACTUAL_FOUR_STRATEGY_LIQUIDATION_NAV"
    actual_metrics = performance_metrics(analysis_curve)
    preferred_benchmark = "TAIEX" if "TAIEX" in benchmark_curves else "0050" if "0050" in benchmark_curves else next(iter(benchmark_curves), None)
    benchmark_analysis_curve = (
        slice_and_normalize(
            benchmark_curves[preferred_benchmark],
            analysis_curve[0][0],
            analysis_curve[-1][0],
        )
        if preferred_benchmark
        else []
    )
    relative = relative_metrics(
        analysis_curve,
        benchmark_analysis_curve,
    )

    series: dict[str, list[tuple[date, float]]] = {}
    series["四策略實際合計"] = actual_curve
    for strategy_id, curve in actual_strategy_curves.items():
        series[f"實際·{STRATEGY_LABELS[strategy_id]}"] = curve
    if preferred_benchmark and benchmark_analysis_curve:
        series[f"Benchmark·{BENCHMARK_LABELS.get(preferred_benchmark, preferred_benchmark)}"] = benchmark_analysis_curve
    theory_series = {
        f"理論卡·{STRATEGY_LABELS[strategy_id]}": curve
        for strategy_id, curve in card_curves.items()
    }
    theory_asof = min(curve[-1][0] for curve in card_curves.values())
    actual_bundle_pnl = strategy_diagnostics["bundle_current_pnl_twd"]

    header_cards = "".join(
        [
            metric_card("庫存現值", f"NT$ {fmt_ntd(snapshot['current_value_twd'])}", "來源畫面『現值』小計"),
            metric_card("付出成本", f"NT$ {fmt_ntd(snapshot['cost_basis_twd'])}", "來源畫面『付出成本』小計"),
            metric_card(
                "累積未實現損益",
                f"NT$ {fmt_ntd(snapshot['unrealized_pnl_twd'], sign=True)}",
                "現值 − 付出成本",
                css_value_class(snapshot["unrealized_pnl_twd"]),
            ),
            metric_card(
                "累積未實現報酬",
                fmt_pct(snapshot["unrealized_return"], sign=True),
                "未實現損益 ÷ 付出成本",
                css_value_class(snapshot["unrealized_return"] or 0),
            ),
            metric_card(
                "四策略實際損益",
                f"NT$ {fmt_ntd(actual_bundle_pnl, sign=True)}",
                "4×50 萬起始；含 3702 已實現損益，排除 2886 未歸屬 1 股",
                css_value_class(actual_bundle_pnl),
            ),
            metric_card(
                "理論卡截止",
                theory_asof.isoformat(),
                f"實際估值已到 {actual_asof.isoformat()}；理論未更新前不假造同日差異",
                "neutral",
            ),
            metric_card(
                "今日價格變動估算",
                f"NT$ {fmt_ntd(snapshot['estimated_daily_price_contribution_twd'], sign=True)}",
                f"約 {fmt_pct(snapshot['estimated_gross_daily_return'], sign=True)}；未含費稅／盤中交易",
                css_value_class(snapshot["estimated_daily_price_contribution_twd"]),
            ),
            metric_card("持股", f"{snapshot['positions']} 檔", f"帳面獲利 {snapshot['winning_positions']}／虧損 {snapshot['losing_positions']}"),
        ]
    )

    return_obs = max(len(analysis_curve) - 1, 0)
    history_status = "ACTUAL_FILLS_RECONCILED"
    risk_status = "OK" if return_obs >= MIN_RISK_RETURN_OBS else "WAITING_MIN_20_RETURNS"
    relative_status = "OK" if relative["beta"] is not None else "WAITING_MIN_20_COMMON_RETURNS"
    risk_metrics = "".join(
        [
            status_metric("四策略實際累計", fmt_pct(actual_metrics["total_return"], sign=True), history_status, "4×50 萬；成交現金流＋可變現價值"),
            status_metric("CAGR", fmt_pct(actual_metrics["cagr"], sign=True), "SHORT_SAMPLE", f"實際日曆日年化；僅 {(analysis_curve[-1][0]-analysis_curve[0][0]).days} 日，數值極不穩定"),
            status_metric("Sharpe", fmt_num(actual_metrics["sharpe"]), risk_status, "日報酬、252 日年化、rf=0"),
            status_metric("Sortino", fmt_num(actual_metrics["sortino"]), risk_status, "只以負報酬估 downside risk"),
            status_metric("MDD", fmt_pct(actual_metrics["max_drawdown"]), history_status, f"實際四策略合計曲線；{return_obs} 筆日報酬"),
            status_metric("Calmar", fmt_num(actual_metrics["calmar"]), "SHORT_SAMPLE", "CAGR ÷ |MDD|；短樣本不作穩健評價"),
            status_metric("Alpha", fmt_pct(relative["alpha"], sign=True), relative_status, "日 OLS intercept × 252、rf=0"),
            status_metric("Beta", fmt_num(relative["beta"]), relative_status, "相對主 benchmark 的日報酬斜率"),
            status_metric("Information Ratio", fmt_num(relative["information_ratio"]), relative_status, "主動報酬 ÷ tracking error"),
            status_metric("Tracking Error", fmt_pct(relative["tracking_error"]), relative_status, "日主動報酬波動 × √252"),
            status_metric("最大單一持股", fmt_pct(snapshot["max_weight"]), "OK", "依來源現值重算"),
            status_metric("有效持股數", fmt_num(snapshot["effective_positions"], 1), "OK", "1 ÷ HHI；越低代表越集中"),
        ]
    )

    taiex_curve = benchmark_curves.get("TAIEX", [])
    per_stock = (
        analytics.per_stock_stats(
            ledger,
            prices,
            mtm_curve[0][0],
            mtm_curve[-1][0],
            "TAIEX",
        )
        if len(mtm_curve) >= 2
        else []
    )
    contribution_rows = analytics.contribution_shares(per_stock)
    risk_scatter = charts.risk_return_scatter(per_stock)
    contribution_waterfall = charts.waterfall_chart(contribution_rows, "組合變動")
    return_distribution = charts.histogram_chart(
        analytics.return_histogram(analytics.returns_from_curve(mtm_curve)),
        statistics.mean(analytics.returns_from_curve(mtm_curve))
        if len(mtm_curve) >= 2
        else None,
    )
    correlation_chart = charts.correlation_bars(per_stock)

    template = """<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>績效累積圖 · 2026-08-24</title>
<style>
:root{--ink:#ecf4ef;--muted:#9eaaa5;--panel:#14231f;--panel2:#192c27;--line:#2a4039;--green:#57d3a2;--red:#ff7f7f;--gold:#f5bd58;--blue:#72a7ff;--bg:#0b1512}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#18362d 0,transparent 34%),var(--bg);color:var(--ink);font-family:"Segoe UI","Noto Sans TC",sans-serif;line-height:1.55}.wrap{max-width:1280px;margin:auto;padding:34px 24px 70px}.eyebrow{color:var(--green);font-weight:700;letter-spacing:.16em;font-size:12px;text-transform:uppercase}.hero{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;margin:8px 0 24px}.hero h1{font-size:clamp(34px,5vw,64px);line-height:1.02;margin:0;letter-spacing:-.04em}.hero p{max-width:560px;color:var(--muted);margin:8px 0 0}.badges{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.badge{border:1px solid var(--line);border-radius:999px;padding:6px 10px;font-size:12px;color:var(--muted)}.badge.good{border-color:#2c7259;color:var(--green)}.badge.warn{border-color:#745c2c;color:var(--gold)}.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.metric-card,.panel{background:linear-gradient(145deg,rgba(25,44,39,.94),rgba(17,31,27,.94));border:1px solid var(--line);border-radius:18px;box-shadow:0 20px 50px rgba(0,0,0,.18)}.metric-card{padding:18px;min-height:132px}.metric-label{font-size:13px;color:var(--muted)}.metric-value{font-size:25px;font-weight:750;margin:10px 0 4px;white-space:nowrap}.metric-note{font-size:12px;color:var(--muted)}.positive{color:var(--green)!important}.negative{color:var(--red)!important}.neutral{color:var(--muted)!important}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}.panel{padding:22px;overflow:hidden}.panel.full{grid-column:1/-1}.panel h2{font-size:20px;margin:0 0 4px}.panel .sub{color:var(--muted);font-size:13px;margin-bottom:18px}.callout{border-left:3px solid var(--gold);background:#2a2618;border-radius:8px;padding:12px 14px;color:#eadfbe;margin:16px 0}.bar-row{display:grid;grid-template-columns:150px 1fr 92px;gap:10px;align-items:center;margin:9px 0;font-size:12px}.bar-label{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.bar-track{height:12px;background:#0d1915;border-radius:999px;position:relative;overflow:hidden}.bar-axis{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#607169}.bar-fill{position:absolute;top:2px;bottom:2px;border-radius:999px}.bar-fill.positive{background:var(--green)}.bar-fill.negative{background:var(--red)}.bar-fill.neutral{background:#607169}.bar-value{text-align:right;font-variant-numeric:tabular-nums}.allocation-row{display:grid;grid-template-columns:150px 1fr 54px;gap:10px;align-items:center;font-size:12px;margin:8px 0}.allocation-track{height:8px;background:#0d1915;border-radius:99px;overflow:hidden}.allocation-track span{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--green));border-radius:99px}.allocation-row strong{text-align:right}.status-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.status-metric{background:#0f1d19;border:1px solid var(--line);border-radius:12px;padding:13px}.status-metric>div{font-size:12px}.status-metric strong{display:block;font-size:20px;margin:6px 0}.status-metric small{display:block;color:var(--muted);font-size:10px}.status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px}.status-dot.ok{background:var(--green);box-shadow:0 0 10px var(--green)}.status-dot.waiting{background:var(--gold);box-shadow:0 0 10px var(--gold)}.period-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}.period-card{background:#0f1d19;border:1px solid var(--line);border-radius:14px;padding:15px}.period-card span,.period-card small{display:block;color:var(--muted);font-size:11px}.period-card b{display:block;font-size:21px;margin:7px 0}.period-bar-row{display:grid;grid-template-columns:82px 1fr 70px;gap:10px;align-items:center;margin:9px 0;font-size:12px}.period-bar-track{height:10px;background:#0d1915;border-radius:99px;overflow:hidden}.period-bar-track i{display:block;height:100%;border-radius:99px}.period-bar-track i.positive{background:var(--green)}.period-bar-track i.negative{background:var(--red)}.period-kind{font-size:12px;color:var(--muted);margin-bottom:10px}.mini-empty{min-height:180px;border:1px dashed var(--line);border-radius:12px;display:flex;align-items:center;justify-content:center;color:var(--gold);text-align:center;padding:20px}.heat-wrap{overflow:auto}.heatmap{min-width:850px}.heatmap td{text-align:center;font-variant-numeric:tabular-nums;border:3px solid var(--panel);border-radius:7px}.heat-empty{background:#0f1d19;color:#5f6e68}.drawdown-head{display:flex;justify-content:space-between;margin-bottom:8px}.drawdown-chart{width:100%;height:auto;background:#0f1d19;border-radius:12px}.empty-chart{min-height:260px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:14px}.empty-chart b{color:var(--gold)}.empty-chart p{margin:4px;max-width:540px}.empty-icon{font-size:48px;color:var(--green)}.line-chart{width:100%;height:auto;background:#0f1d19;border-radius:12px}.grid-line{stroke:#2a4039;stroke-width:1}.axis-text{fill:#899791;font-size:11px}.chart-legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;font-size:12px;color:var(--muted)}.chart-legend i{display:inline-block;width:18px;height:3px;margin-right:6px;vertical-align:middle}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;color:var(--muted);font-weight:600;border-bottom:1px solid var(--line);padding:10px 8px;white-space:nowrap}td{padding:10px 8px;border-bottom:1px solid rgba(42,64,57,.55)}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}small{color:var(--muted)}.quality{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.quality article{background:#0f1d19;border-radius:12px;padding:15px;border:1px solid var(--line)}.quality b{display:block;margin-bottom:5px}.quality p{font-size:12px;color:var(--muted);margin:0}.footer{margin-top:22px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:20px}.mono{font-family:Consolas,monospace}.section-gap{margin-top:16px}@media(max-width:1050px){.metrics{grid-template-columns:repeat(3,1fr)}.period-grid{grid-template-columns:repeat(4,1fr)}.status-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:760px){.wrap{padding:22px 14px 50px}.hero{display:block}.grid{grid-template-columns:1fr}.panel.full{grid-column:auto}.metrics{grid-template-columns:repeat(2,1fr)}.period-grid{grid-template-columns:repeat(2,1fr)}.status-grid,.quality{grid-template-columns:1fr}.bar-row{grid-template-columns:100px 1fr 78px}.allocation-row{grid-template-columns:100px 1fr 48px}.metric-value{font-size:20px}}@media print{body{background:#fff;color:#111}.metric-card,.panel{box-shadow:none;background:#fff;border-color:#ccc}.metric-note,.panel .sub,small,.footer{color:#555}.positive{color:#087f5b!important}.negative{color:#c92a2a!important}}
</style>
</head>
<body><main class="wrap">
<div class="eyebrow">66 · PERFORMANCE ACCUMULATION</div>
<section class="hero"><div><h1>績效累積圖</h1><p>實際績效已改用 2026-08-10 起始、2026-08-11 起逐筆成交的四策略 equity curve。不再把今日持股倒推一年。理論卡與實際線分開標示截止日。</p><div class="badges"><span class="badge good">ACTUAL_FILLS_RECONCILED</span><span class="badge warn">THEORY_ASOF_2026-08-20</span><span class="badge warn">RISK_SAMPLE_{{RISK_OBS}}_RETURNS</span><span class="badge">NO_BROKER · NO_ORDER</span></div></div><div><b>四策略估值日</b><br><span class="mono">{{ASOF}}</span><br><small>owner 庫存快照 {{SNAPSHOT_ASOF}}；理論卡 {{THEORY_ASOF}}</small></div></section>
<section class="metrics">{{HEADER_CARDS}}</section>
<section class="grid">
<article class="panel full"><h2>四策略實際績效 · 累積曲線</h2><div class="sub">每個 sleeve 以 NT$50 萬現金起始，用實際成交、費稅、已實現損益與每日可變現價值重建；合計初始資金 NT$200 萬。</div>{{LINE_CHART}}</article>
<article class="panel full"><h2>四策略理論卡 · 來源顯示曲線</h2><div class="sub">這是 owner 策略卡的「當日持倉成分等權顯示報酬」，不是可投資 NAV，也不將每日百分比複利串接。資料只到 {{THEORY_ASOF}}。</div>{{THEORY_CHART}}</article>
<article class="panel full"><h2>實際 vs 理論 · 四策略差異</h2><div class="sub">「差異」只在共同截止日 {{THEORY_ASOF}} 計算：實際 50 萬 sleeve 可變現報酬 − 理論卡等權顯示報酬。這是描述性 implementation gap，權重與現金比率不同，不冒充 alpha。</div><div class="table-wrap"><table><thead><tr><th>策略</th><th class="num">實際累計<br>{{ASOF}}</th><th class="num">實際損益</th><th class="num">實際<br>{{THEORY_ASOF}}</th><th class="num">理論卡<br>{{THEORY_ASOF}}</th><th class="num">差異<br>pp</th><th class="num">實際/理論<br>持股數</th><th class="num">MDD</th><th class="num">Sharpe</th></tr></thead><tbody>{{STRATEGY_TABLE}}</tbody></table></div></article>
<article class="panel full"><h2>日／週／月／季／年／YTD／累計</h2><div class="sub">basis：{{ANALYSIS_BASIS}}。近一月、季、年若沒有足夠實際觀察就顯示 N/A，不用同一批股票倒推。</div><div class="period-grid">{{PERIOD_CARDS}}</div></article>
<article class="panel full" id="risk-metrics"><h2>Sharpe／MDD／Alpha／Beta · 完整績效風險衡量</h2><div class="sub">以四策略實際合計曲線計算。MDD 已可計算；Sharpe、Sortino、Alpha、Beta、IR 與 Tracking Error 因尚未滿 20 筆日報酬而顯示 N/A。</div><div class="status-grid">{{RISK_METRICS}}</div></article>
<article class="panel"><h2>歷史期間報酬</h2><div class="sub">資料成長後優先顯示月報酬，再依可用資料退回週／季／年。</div>{{PERIOD_BARS}}</article>
<article class="panel"><h2>水下回撤圖</h2><div class="sub">每天相對歷史淨值高點的跌幅；MDD 就是最深位置。</div>{{DRAWDOWN}}</article>
<article class="panel full"><h2>月度績效熱圖</h2><div class="sub">橫向為月份、縱向為年份，快速看 regime、季節性與連續虧損月份。</div>{{MONTHLY_HEATMAP}}</article>
<article class="panel"><h2>個股過去一年風險特徵</h2><div class="sub">這裡只是各股價格歷史的風險指紋，不是你的持有期報酬，不納入上方四策略績效。</div>{{RISK_SCATTER}}</article>
<article class="panel"><h2>個股過去一年與大盤相關性</h2><div class="sub">單純描述股價風險特徵；不把 8/10 以前報酬算進你的實際績效。</div>{{CORRELATION_CHART}}</article>
<article class="panel"><h2>個股累積未實現損益</h2><div class="sub">直接使用來源畫面的「損益試算」；綠色為正、紅色為負。</div>{{PNL_BARS}}</article>
<article class="panel"><h2>今日價格變動估算貢獻</h2><div class="sub">股數 × 畫面漲跌；未含今天費稅、盤中交易與現金，不是正式 daily P&amp;L。</div>{{DAY_BARS}}</article>
<article class="panel"><h2>庫存配置</h2><div class="sub">依來源「現值」重算；最大單一持股 {{MAX_WEIGHT}}。</div>{{ALLOCATION}}</article>
<article class="panel"><h2>今天先看懂三件事</h2><div class="sub">單點資料可以回答的問題，不越界解讀。</div><div class="callout"><b>帳面總體為正：</b>累積未實現損益 {{TOTAL_PNL}}，但 15 檔中仍有 {{LOSING}} 檔虧損。</div><p><b>累積最大正貢獻：</b>{{TOP_WINNER}}</p><p><b>累積最大負貢獻：</b>{{TOP_LOSER}}</p><p><b>今日估算最大推升：</b>{{TOP_DAY_WINNER}}</p><p><b>今日估算最大拖累：</b>{{TOP_DAY_LOSER}}</p></article>
<article class="panel full"><h2>持股明細</h2><div class="sub">現值與損益完全對上 owner 貼入小計；配置比例由現值重新計算。</div><div class="table-wrap"><table><thead><tr><th>股票</th><th class="num">股數</th><th class="num">成本均價</th><th class="num">現價</th><th class="num">今日漲跌幅</th><th class="num">現值</th><th class="num">未實現損益</th><th class="num">獲利率</th><th class="num">配置</th></tr></thead><tbody>{{HOLDINGS_TABLE}}</tbody></table></div></article>
<article class="panel full"><h2>資料品質與限制</h2><div class="sub">畫面能否拿來做決策，先看資料是否足夠。</div><div class="quality"><article><b class="positive">PASS · 庫存小計</b><p>15 檔股數、現值、成本與損益均對上 owner 快照。</p></article><article><b class="positive">PASS · 四策略成交歸屬</b><p>22 筆買賣重建後的活動股數與 8/24 庫存一致，但排除不在成交簿的 2886 1 股。</p></article><article><b class="positive">PASS · 重疊股拆分</b><p>1709：突破 3,644／融資 305 股；2301：YOY 261／投信 365 股；3702 賣出損益納入 YOY。</p></article><article><b class="negative">SHORT SAMPLE · 風險統計</b><p>目前僅 {{RISK_OBS}} 筆實際日報酬；MDD 可描述，Sharpe、Alpha、Beta 等尚不顯示數字。</p></article><article><b class="negative">STALE · 理論卡</b><p>理論來源只到 {{THEORY_ASOF}}；實際與理論不做錯日差異。</p></article><article><b class="positive">SAFE · 公開唯讀</b><p>HTML builder 無券商登入或下單；每日 updater 只讀 TWSE／TPEx 公開收盤行情。</p></article></div></article>
</section>
<footer class="footer"><span>口徑：252 trading days · rf=0 · CAGR 365.25 calendar days · Alpha=daily OLS intercept×252</span><span>生成時間：<span class="mono">{{GENERATED_AT}}</span></span></footer>
</main></body></html>"""

    top_winner = max(holdings, key=lambda row: row["unrealized_pnl_twd"])
    top_loser = min(holdings, key=lambda row: row["unrealized_pnl_twd"])
    top_day_winner = max(holdings, key=lambda row: row["estimated_daily_price_contribution_twd"])
    top_day_loser = min(holdings, key=lambda row: row["estimated_daily_price_contribution_twd"])
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    replacements = {
        "{{ASOF}}": actual_asof.isoformat(),
        "{{SNAPSHOT_ASOF}}": source_summary["asof_date"].isoformat(),
        "{{RISK_OBS}}": str(return_obs),
        "{{THEORY_ASOF}}": theory_asof.isoformat(),
        "{{HEADER_CARDS}}": header_cards,
        "{{LINE_CHART}}": line_chart(series),
        "{{THEORY_CHART}}": line_chart(theory_series),
        "{{STRATEGY_TABLE}}": strategy_comparison_table(
            actual_strategy_curves, card_curves, strategy_diagnostics
        ),
        "{{ANALYSIS_BASIS}}": analysis_basis,
        "{{PERIOD_CARDS}}": period_cards(analysis_curve),
        "{{PERIOD_BARS}}": historical_period_bars(analysis_curve),
        "{{DRAWDOWN}}": drawdown_visual(analysis_curve),
        "{{MONTHLY_HEATMAP}}": monthly_heatmap(analysis_curve),
        "{{RISK_SCATTER}}": risk_scatter,
        "{{CONTRIBUTION_WATERFALL}}": contribution_waterfall,
        "{{RETURN_DISTRIBUTION}}": return_distribution,
        "{{CORRELATION_CHART}}": correlation_chart,
        "{{PNL_BARS}}": diverging_bars(holdings, "unrealized_pnl_twd", " 元"),
        "{{DAY_BARS}}": diverging_bars(holdings, "estimated_daily_price_contribution_twd", " 元"),
        "{{ALLOCATION}}": allocation_bars(holdings),
        "{{MAX_WEIGHT}}": fmt_pct(snapshot["max_weight"]),
        "{{TOTAL_PNL}}": f"NT$ {fmt_ntd(snapshot['unrealized_pnl_twd'], sign=True)}",
        "{{LOSING}}": str(snapshot["losing_positions"]),
        "{{TOP_WINNER}}": f"{top_winner['stock_code']} {top_winner['stock_name']}，NT$ {fmt_ntd(top_winner['unrealized_pnl_twd'], sign=True)}",
        "{{TOP_LOSER}}": f"{top_loser['stock_code']} {top_loser['stock_name']}，NT$ {fmt_ntd(top_loser['unrealized_pnl_twd'], sign=True)}",
        "{{TOP_DAY_WINNER}}": f"{top_day_winner['stock_code']} {top_day_winner['stock_name']}，約 NT$ {fmt_ntd(top_day_winner['estimated_daily_price_contribution_twd'], sign=True)}",
        "{{TOP_DAY_LOSER}}": f"{top_day_loser['stock_code']} {top_day_loser['stock_name']}，約 NT$ {fmt_ntd(top_day_loser['estimated_daily_price_contribution_twd'], sign=True)}",
        "{{RISK_METRICS}}": risk_metrics,
        "{{HOLDINGS_TABLE}}": holdings_table(holdings),
        "{{GENERATED_AT}}": generated_at,
    }
    dashboard = template
    for marker, value in replacements.items():
        dashboard = dashboard.replace(marker, value)
    if any(marker in dashboard for marker in replacements):
        raise RuntimeError("unresolved dashboard template marker")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    index_path = ROOT / "index.html"
    output_path = OUTPUT / "performance_dashboard.html"
    index_path.write_text(dashboard, encoding="utf-8")
    output_path.write_text(dashboard, encoding="utf-8")
    period_snapshot = trailing_returns(analysis_curve)
    summary_markdown = "\n".join(
        [
            "# 公開績效累積圖 · 最新摘要",
            "",
            f"- 四策略估值日：`{actual_asof.isoformat()}`",
            f"- owner 庫存快照日：`{source_summary['asof_date'].isoformat()}`",
            f"- 庫存現值：`NT$ {fmt_ntd(snapshot['current_value_twd'])}`",
            f"- 累積未實現損益：`NT$ {fmt_ntd(snapshot['unrealized_pnl_twd'], sign=True)}`",
            f"- 累積未實現報酬：`{fmt_pct(snapshot['unrealized_return'], sign=True)}`",
            f"- 今日價格變動估算：`NT$ {fmt_ntd(snapshot['estimated_daily_price_contribution_twd'], sign=True)}`（約 `{fmt_pct(snapshot['estimated_gross_daily_return'], sign=True)}`）",
            f"- 四策略實際累計損益：`NT$ {fmt_ntd(actual_bundle_pnl, sign=True)}`（以 NT$200 萬起始資金）",
            f"- 理論卡最新日：`{theory_asof.isoformat()}`（非 8/24 同日值）",
            f"- 期間視圖 basis：`{analysis_basis}`",
            "",
            "| 期間 | TWR 績效 | 狀態 |",
            "|---|---:|---|",
            *[
                f"| {label} | {fmt_pct(value, sign=True)} | {'OK' if value is not None else 'BASELINE_ONLY'} |"
                for label, value in period_snapshot.items()
            ],
            "",
            "> 完整視覺：https://wegoliao.github.io/performance-accumulation-dashboard/",
        ]
    )
    (ROOT / "DASHBOARD_SUMMARY.md").write_text(summary_markdown + "\n", encoding="utf-8")
    receipt = {
        "status": "SUCCESS",
        "generated_at": generated_at,
        "source_data_asof": source_summary["asof_date"].isoformat(),
        "actual_valuation_asof": actual_asof.isoformat(),
        "source_capture_time": None,
        "source_status": "USER_PASTED_INTRADAY_SNAPSHOT",
        "snapshot_reconciliation": snapshot["reconciliation"],
        "inputs": {
            path.name: {"sha256": sha256(path), "rows": len(read_csv(path))}
            for path in (
                HOLDINGS_PATH,
                SUMMARY_PATH,
                ACCOUNT_NAV_PATH,
                STRATEGY_NAV_PATH,
                BENCHMARK_NAV_PATH,
                ACTUAL_FILLS_PATH,
                STRATEGY_CARD_PATH,
                STRATEGY_MARKS_PATH,
            )
        },
        "snapshot": snapshot,
        "history": {
            "account_nav_observations": len(account_rows),
            "account_return_observations": max(len(legacy_account_curve) - 1, 0),
            "analysis_return_observations": return_obs,
            "analysis_basis": analysis_basis,
            "mtm_diagnostics": mtm_diagnostics,
            "strategy_series": sorted(actual_strategy_curves),
            "strategy_card_asof": theory_asof.isoformat(),
            "strategy_diagnostics": strategy_diagnostics,
            "legacy_strategy_series": sorted(legacy_strategy_curves),
            "benchmark_series": sorted(benchmark_curves),
            "performance_status": history_status,
            "risk_metric_status": risk_status,
            "relative_metric_status": relative_status,
            "period_returns": period_snapshot,
        },
        "safety": {
            "network_access": False,
            "broker_import": False,
            "credential_access": False,
            "order_capability": False,
        },
    }
    (OUTPUT / "build_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return index_path, receipt


if __name__ == "__main__":
    path, receipt = build()
    snapshot = receipt["snapshot"]
    print(f"SUCCESS: {path}")
    print(
        "RECONCILED: "
        f"value={snapshot['current_value_twd']:.0f} "
        f"cost={snapshot['cost_basis_twd']:.0f} "
        f"pnl={snapshot['unrealized_pnl_twd']:.0f}"
    )
    print(f"HISTORY_STATUS: {receipt['history']['performance_status']}")
