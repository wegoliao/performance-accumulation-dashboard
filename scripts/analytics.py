"""Time-series analytics for the 66 performance accumulation lane.

Pure standard library, no network, no broker. Everything here consumes CSV
files that `fetch_prices.py` (or the owner) wrote, and returns plain data
structures that `build_dashboard.py` renders.

Two independent lanes are kept apart on purpose:

* ``ACCOUNT``   -- real money-weighted account NAV from ``account_nav.csv``.
                   This is the only lane that may be called realised performance.
* ``MTM``       -- the current holdings marked back through historical closes
                   (``SIMULATED_CONSTANT_HOLDINGS``). It answers "how does the
                   basket I hold today behave?", not "what did I actually earn".

Mixing the two would be the exact kind of flattering-but-false number this lane
exists to avoid, so the lane label travels with every series.
"""

from __future__ import annotations

import csv
import math
import statistics
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

TRADING_DAYS = 252
MIN_RISK_RETURN_OBS = 20
ROLLING_WINDOW = 60


class InputError(ValueError):
    """Input contract violation that must fail closed."""


# --------------------------------------------------------------------- loading


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle) if any(row.values())]


def _to_date(value: str, field: str) -> date:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise InputError(f"{field} must be YYYY-MM-DD: {value!r}") from exc


def _to_float(value: str, field: str) -> float:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise InputError(f"{field} must be numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise InputError(f"{field} must be finite")
    return number


def load_price_history(path: Path) -> dict[str, list[tuple[date, float]]]:
    """Return {stock_code: [(date, close), ...]} sorted ascending by date."""
    series: dict[str, dict[date, float]] = defaultdict(dict)
    for row in _read_csv(path):
        code = (row.get("stock_code") or "").strip()
        if not code:
            raise InputError("price_history.csv row is missing stock_code")
        close = _to_float(row["close"], f"price_history.close[{code}]")
        if close <= 0:
            raise InputError(f"price_history close must be positive for {code}")
        series[code][_to_date(row["asof_date"], "price_history.asof_date")] = close
    return {code: sorted(points.items()) for code, points in series.items()}


def load_ledger(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in _read_csv(path):
        code = (row.get("stock_code") or "").strip()
        if not code:
            raise InputError("positions_ledger.csv row is missing stock_code")
        shares = _to_float(row["shares"], f"positions_ledger.shares[{code}]")
        if shares < 0:
            raise InputError(f"positions_ledger shares must be >= 0 for {code}")
        rows.append(
            {
                "effective_from": _to_date(row["effective_from"], "effective_from"),
                "stock_code": code,
                "stock_name": (row.get("stock_name") or "").strip(),
                "shares": shares,
                "avg_cost": _to_float(row.get("avg_cost") or "0", "avg_cost"),
                "basis": (row.get("basis") or "").strip(),
            }
        )
    rows.sort(key=lambda item: (item["stock_code"], item["effective_from"]))
    return rows


def shares_on(ledger: Sequence[dict[str, Any]], day: date) -> dict[str, float]:
    """Latest ledger entry per stock effective on or before ``day``."""
    held: dict[str, float] = {}
    for row in ledger:
        if row["effective_from"] <= day:
            held[row["stock_code"]] = row["shares"]
    return {code: shares for code, shares in held.items() if shares > 0}


# ----------------------------------------------------------------- curve build


def _as_of(points: Sequence[tuple[date, float]], day: date) -> float | None:
    """Last close on or before ``day`` (forward fill across suspensions)."""
    index = bisect_right([point[0] for point in points], day)
    return points[index - 1][1] if index else None


def build_mtm_curve(
    ledger: Sequence[dict[str, Any]],
    prices: dict[str, list[tuple[date, float]]],
    calendar_code: str = "TAIEX",
) -> tuple[list[tuple[date, float]], dict[str, Any]]:
    """Mark today's book back through history.

    Returns ``(curve, diagnostics)``. A date is only emitted when every held
    stock already has a close on or before it, so the curve never silently
    changes constituent count mid-flight.
    """
    if not ledger or not prices:
        return [], {"status": "WAITING_PRICE_HISTORY", "covered_days": 0, "missing": []}

    calendar = [day for day, _ in prices.get(calendar_code, [])]
    if not calendar:
        calendar = sorted({day for points in prices.values() for day, _ in points})

    curve: list[tuple[date, float]] = []
    skipped: list[date] = []
    missing_codes: set[str] = set()
    for day in calendar:
        held = shares_on(ledger, day)
        if not held:
            continue
        value = 0.0
        complete = True
        for code, shares in held.items():
            close = _as_of(prices.get(code, []), day)
            if close is None:
                complete = False
                missing_codes.add(code)
                break
            value += shares * close
        if complete and value > 0:
            curve.append((day, value))
        else:
            skipped.append(day)

    diagnostics = {
        "status": "OK" if len(curve) >= 2 else "WAITING_PRICE_HISTORY",
        "covered_days": len(curve),
        "skipped_days": len(skipped),
        "first_covered": curve[0][0].isoformat() if curve else None,
        "last_covered": curve[-1][0].isoformat() if curve else None,
        "missing": sorted(missing_codes),
        "calendar_source": calendar_code,
        "basis": "SIMULATED_CONSTANT_HOLDINGS",
    }
    return curve, diagnostics


def normalize_curve(curve: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    if not curve:
        return []
    base = curve[0][1]
    return [(day, value / base * 100.0) for day, value in curve]


def returns_from_curve(curve: Sequence[tuple[date, float]]) -> list[float]:
    return [curve[i][1] / curve[i - 1][1] - 1.0 for i in range(1, len(curve))]


def aligned_returns(
    curve: Sequence[tuple[date, float]], benchmark: Sequence[tuple[date, float]]
) -> tuple[list[date], list[float], list[float]]:
    """Daily returns for both series on their shared trading days."""
    left = dict(curve)
    right = dict(benchmark)
    common = sorted(set(left) & set(right))
    if len(common) < 2:
        return [], [], []
    days = common[1:]
    left_returns = [left[common[i]] / left[common[i - 1]] - 1.0 for i in range(1, len(common))]
    right_returns = [right[common[i]] / right[common[i - 1]] - 1.0 for i in range(1, len(common))]
    return days, left_returns, right_returns


# --------------------------------------------------------------------- shapes


def drawdown_series(curve: Sequence[tuple[date, float]]) -> list[tuple[date, float]]:
    series: list[tuple[date, float]] = []
    peak = float("-inf")
    for day, value in curve:
        peak = max(peak, value)
        series.append((day, value / peak - 1.0))
    return series


def drawdown_detail(curve: Sequence[tuple[date, float]]) -> dict[str, Any]:
    """Depth, dates and recovery status of the worst drawdown."""
    if len(curve) < 2:
        return {"max_drawdown": None, "peak_date": None, "trough_date": None,
                "recovery_date": None, "underwater_days": None, "current_drawdown": None}
    peak_value = curve[0][1]
    peak_day = curve[0][0]
    worst = 0.0
    worst_peak_day = curve[0][0]
    worst_trough_day = curve[0][0]
    for day, value in curve:
        if value >= peak_value:
            peak_value = value
            peak_day = day
        drop = value / peak_value - 1.0
        if drop < worst:
            worst = drop
            worst_peak_day = peak_day
            worst_trough_day = day
    recovery_day = None
    trough_peak_value = dict(curve)[worst_peak_day]
    for day, value in curve:
        if day > worst_trough_day and value >= trough_peak_value:
            recovery_day = day
            break
    running_peak = max(value for _, value in curve)
    return {
        "max_drawdown": worst if worst < 0 else None,
        "peak_date": worst_peak_day.isoformat(),
        "trough_date": worst_trough_day.isoformat(),
        "recovery_date": recovery_day.isoformat() if recovery_day else None,
        "underwater_days": (worst_trough_day - worst_peak_day).days,
        "current_drawdown": curve[-1][1] / running_peak - 1.0,
    }


def monthly_returns(curve: Sequence[tuple[date, float]]) -> list[dict[str, Any]]:
    """Calendar-month returns from month-end levels (first point seeds the base)."""
    if len(curve) < 2:
        return []
    month_end: dict[tuple[int, int], tuple[date, float]] = {}
    for day, value in curve:
        month_end[(day.year, day.month)] = (day, value)
    keys = sorted(month_end)
    result: list[dict[str, Any]] = []
    previous = curve[0][1]
    for key in keys:
        day, value = month_end[key]
        if day == curve[0][0]:
            previous = value
            continue
        result.append(
            {
                "year": key[0],
                "month": key[1],
                "return": value / previous - 1.0,
                "asof": day.isoformat(),
            }
        )
        previous = value
    return result


def rolling_series(
    curve: Sequence[tuple[date, float]],
    benchmark: Sequence[tuple[date, float]],
    window: int = ROLLING_WINDOW,
) -> dict[str, list[tuple[date, float]]]:
    """Rolling annualised Sharpe (rf=0), rolling volatility and rolling beta."""
    days, portfolio, market = aligned_returns(curve, benchmark)
    if len(days) < window:
        return {"sharpe": [], "volatility": [], "beta": []}
    sharpe: list[tuple[date, float]] = []
    volatility: list[tuple[date, float]] = []
    beta: list[tuple[date, float]] = []
    for end in range(window, len(days) + 1):
        chunk = portfolio[end - window : end]
        market_chunk = market[end - window : end]
        day = days[end - 1]
        deviation = statistics.stdev(chunk)
        if deviation > 0:
            annual_vol = deviation * math.sqrt(TRADING_DAYS)
            volatility.append((day, annual_vol))
            sharpe.append((day, statistics.mean(chunk) * TRADING_DAYS / annual_vol))
        market_mean = statistics.mean(market_chunk)
        denominator = sum((value - market_mean) ** 2 for value in market_chunk)
        if denominator > 0:
            chunk_mean = statistics.mean(chunk)
            beta.append(
                (
                    day,
                    sum(
                        (x - market_mean) * (y - chunk_mean)
                        for x, y in zip(market_chunk, chunk)
                    )
                    / denominator,
                )
            )
    return {"sharpe": sharpe, "volatility": volatility, "beta": beta}


def return_histogram(returns: Sequence[float], bins: int = 21) -> list[dict[str, Any]]:
    if len(returns) < 2:
        return []
    low, high = min(returns), max(returns)
    if math.isclose(low, high):
        return []
    span = (high - low) / bins
    buckets = [0] * bins
    for value in returns:
        index = min(int((value - low) / span), bins - 1)
        buckets[index] += 1
    return [
        {"low": low + index * span, "high": low + (index + 1) * span, "count": count}
        for index, count in enumerate(buckets)
    ]


def capture_ratios(
    curve: Sequence[tuple[date, float]], benchmark: Sequence[tuple[date, float]]
) -> dict[str, float | None]:
    """Up/down capture: how much of the market's move the book participates in."""
    _, portfolio, market = aligned_returns(curve, benchmark)
    empty = {"up_capture": None, "down_capture": None, "up_days": None, "down_days": None,
             "hit_rate_vs_benchmark": None}
    if len(portfolio) < MIN_RISK_RETURN_OBS:
        return empty
    up = [(p, m) for p, m in zip(portfolio, market) if m > 0]
    down = [(p, m) for p, m in zip(portfolio, market) if m < 0]
    beat = sum(1 for p, m in zip(portfolio, market) if p > m)
    result = dict(empty)
    result["up_days"] = float(len(up))
    result["down_days"] = float(len(down))
    result["hit_rate_vs_benchmark"] = beat / len(portfolio)
    if up:
        market_up = statistics.mean(m for _, m in up)
        if market_up != 0:
            result["up_capture"] = statistics.mean(p for p, _ in up) / market_up
    if down:
        market_down = statistics.mean(m for _, m in down)
        if market_down != 0:
            result["down_capture"] = statistics.mean(p for p, _ in down) / market_down
    return result


def per_stock_stats(
    ledger: Sequence[dict[str, Any]],
    prices: dict[str, list[tuple[date, float]]],
    start: date,
    end: date,
    benchmark_code: str = "TAIEX",
) -> list[dict[str, Any]]:
    """Per-holding period return, risk, beta and NT$ contribution to the book."""
    held = shares_on(ledger, end)
    names = {row["stock_code"]: row["stock_name"] for row in ledger}
    costs = {row["stock_code"]: row["avg_cost"] for row in ledger}
    benchmark_points = [
        (day, value) for day, value in prices.get(benchmark_code, []) if start <= day <= end
    ]
    benchmark_map = dict(benchmark_points)

    stats: list[dict[str, Any]] = []
    for code, shares in sorted(held.items()):
        points = [(day, value) for day, value in prices.get(code, []) if start <= day <= end]
        if len(points) < 2:
            continue
        first_price, last_price = points[0][1], points[-1][1]
        period_return = last_price / first_price - 1.0
        returns = [points[i][1] / points[i - 1][1] - 1.0 for i in range(1, len(points))]
        volatility = (
            statistics.stdev(returns) * math.sqrt(TRADING_DAYS)
            if len(returns) >= MIN_RISK_RETURN_OBS
            else None
        )
        elapsed = (points[-1][0] - points[0][0]).days
        annualised = (
            (last_price / first_price) ** (365.25 / elapsed) - 1.0 if elapsed > 0 else None
        )
        common = sorted(set(dict(points)) & set(benchmark_map))
        beta = None
        correlation = None
        if len(common) > MIN_RISK_RETURN_OBS:
            stock_map = dict(points)
            stock_returns = [
                stock_map[common[i]] / stock_map[common[i - 1]] - 1.0 for i in range(1, len(common))
            ]
            market_returns = [
                benchmark_map[common[i]] / benchmark_map[common[i - 1]] - 1.0
                for i in range(1, len(common))
            ]
            market_mean = statistics.mean(market_returns)
            denominator = sum((value - market_mean) ** 2 for value in market_returns)
            if denominator > 0:
                stock_mean = statistics.mean(stock_returns)
                beta = (
                    sum(
                        (x - market_mean) * (y - stock_mean)
                        for x, y in zip(market_returns, stock_returns)
                    )
                    / denominator
                )
                stock_dev = statistics.stdev(stock_returns)
                market_dev = statistics.stdev(market_returns)
                if stock_dev > 0 and market_dev > 0:
                    correlation = beta * market_dev / stock_dev
        peak = points[0][1]
        stock_mdd = 0.0
        for _, value in points:
            peak = max(peak, value)
            stock_mdd = min(stock_mdd, value / peak - 1.0)
        stats.append(
            {
                "stock_code": code,
                "stock_name": names.get(code, code),
                "shares": shares,
                "first_price": first_price,
                "last_price": last_price,
                "period_return": period_return,
                "annualised_return": annualised,
                "volatility": volatility,
                "beta": beta,
                "correlation": correlation,
                "max_drawdown": stock_mdd,
                "start_value": shares * first_price,
                "end_value": shares * last_price,
                "value_change_twd": shares * (last_price - first_price),
                "avg_cost": costs.get(code, 0.0),
                "unrealized_pnl_twd": shares * (last_price - costs.get(code, 0.0)),
                "observations": len(returns),
            }
        )
    return stats


def contribution_shares(stats: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attribute the book's NT$ move to each holding, largest absolute first."""
    total = sum(row["value_change_twd"] for row in stats)
    ordered = sorted(stats, key=lambda row: row["value_change_twd"], reverse=True)
    return [
        {
            **row,
            "contribution_share": (row["value_change_twd"] / total) if total else None,
            "total_change_twd": total,
        }
        for row in ordered
    ]
