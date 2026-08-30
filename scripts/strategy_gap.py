"""Descriptive lenses for strategy-card versus actual-sleeve differences.

The public strategy card is not an investable NAV.  It is a source-displayed,
equal-weight return over the card's current members.  The actual sleeve is a
cash account reconstructed from fills and liquidation marks.  This module does
not pretend those are causally comparable; it turns their difference into a
small, explicit report whose assumptions stay visible to callers and tests.

Interface
---------
``analyze(...)`` is the only public interface.  It accepts normalized data that
the dashboard already owns and returns JSON-serializable state.  Rendering is
deliberately outside this module.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Iterable, Sequence


MIN_EXECUTION_OBSERVATIONS = 30


def _latest_return(curve: Sequence[tuple[date, float]]) -> float | None:
    if not curve:
        return None
    return curve[-1][1] / 100.0 - 1.0


def _benchmark_return(
    curve: Sequence[tuple[date, float]], start: date, end: date
) -> float | None:
    points = [(day, value) for day, value in curve if start <= day <= end]
    if len(points) < 2 or points[0][1] == 0:
        return None
    return points[-1][1] / points[0][1] - 1.0


def _gap_history(
    actual: Sequence[tuple[date, float]], card: Sequence[tuple[date, float]]
) -> list[dict[str, Any]]:
    actual_by_day = dict(actual)
    card_by_day = dict(card)
    days = sorted(set(actual_by_day) & set(card_by_day))
    return [
        {
            "date": day.isoformat(),
            # Both curves use 100 as their zero-return level.  Subtracting the
            # levels therefore returns percentage points directly.
            "gap_pp": actual_by_day[day] - card_by_day[day],
        }
        for day in days
    ]


def _execution_summary(
    rows: Iterable[dict[str, Any]], strategy_id: str
) -> dict[str, Any]:
    selected = [row for row in rows if row.get("strategy_id") == strategy_id]
    adverse = [
        float(row["slippage_vs_signal_bp"])
        for row in selected
        if row.get("slippage_vs_signal_bp") is not None
    ]
    return {
        "observations": len(selected),
        "average_signal_slippage_bp": (
            sum(adverse) / len(adverse) if adverse else None
        ),
        "status": (
            "OK_MIN_30"
            if len(selected) >= MIN_EXECUTION_OBSERVATIONS
            else "WAITING_MIN_30_SIGNAL_FILLS"
        ),
    }


def analyze(
    *,
    strategy_labels: dict[str, str],
    bridge: dict[str, Any],
    latest_signals: Sequence[dict[str, Any]],
    actual_curves: dict[str, Sequence[tuple[date, float]]],
    card_curves: dict[str, Sequence[tuple[date, float]]],
    benchmark_curves: dict[str, Sequence[tuple[date, float]]],
    slippage_rows: Sequence[dict[str, Any]],
    asof: date,
) -> dict[str, Any]:
    """Build one transparent report for every owner-facing gap lens.

    ``bridge`` is the exact algebraic bridge already produced by the fill-book
    module.  This function adds coverage, missing/stale names, benchmark
    context, gap history, and execution-evidence sufficiency.  It never creates
    a counterfactual P&L for a name that was not bought.
    """

    signals_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in latest_signals:
        signals_by_strategy[row["strategy_id"]].append(row)

    starts = [curve[0][0] for curve in actual_curves.values() if curve]
    start = min(starts) if starts else asof
    benchmark_returns = {
        benchmark_id: _benchmark_return(curve, start, asof)
        for benchmark_id, curve in benchmark_curves.items()
    }

    strategies: dict[str, Any] = {}
    total_card = 0
    total_covered = 0
    total_cost = 0.0
    total_actual = 0.0
    total_card_return = 0.0
    all_history_dates: set[str] = set()

    for strategy_id, label in strategy_labels.items():
        base = bridge[strategy_id]
        rows = signals_by_strategy.get(strategy_id, [])
        card_codes = {row["stock_code"].strip() for row in rows}
        held_codes = {row["stock_code"] for row in base["names"]}
        covered = sorted(card_codes & held_codes)
        missing = sorted(card_codes - held_codes)
        stale = sorted(held_codes - card_codes)
        planned_entry = sorted(
            row["stock_code"].strip() for row in rows if row.get("signal") == "進"
        )
        planned_exit = sorted(
            row["stock_code"].strip() for row in rows if row.get("signal") == "出"
        )

        return_by_code = {
            row["stock_code"].strip(): float(row.get("signed_return_pct") or 0.0)
            / 100.0
            for row in rows
        }
        missing_returns = [return_by_code[code] for code in missing if code in return_by_code]
        covered_returns = [return_by_code[code] for code in covered if code in return_by_code]
        missing_average = (
            sum(missing_returns) / len(missing_returns) if missing_returns else None
        )
        covered_average = (
            sum(covered_returns) / len(covered_returns) if covered_returns else None
        )

        entry_rows = [row for row in base["names"] if row.get("entry_gap") is not None]
        entry_cost = sum(float(row["cost_twd"]) for row in entry_rows)
        weighted_entry_gap = (
            sum(float(row["entry_gap"]) * float(row["cost_twd"]) for row in entry_rows)
            / entry_cost
            if entry_cost
            else None
        )

        history = _gap_history(
            actual_curves.get(strategy_id, []), card_curves.get(strategy_id, [])
        )
        all_history_dates.update(row["date"] for row in history)
        latest_gap = history[-1]["gap_pp"] if history else None
        prior_index = max(0, len(history) - 6)
        five_observation_change = (
            latest_gap - history[prior_index]["gap_pp"]
            if history and len(history) > 1
            else None
        )

        actual_return = float(base["sleeve_return"])
        card_return = float(base["r_card"]) if base.get("r_card") is not None else None
        active_vs_benchmarks = {
            benchmark_id: (
                actual_return - value if value is not None else None
            )
            for benchmark_id, value in benchmark_returns.items()
        }

        terms = base.get("terms") or {}
        drivers = {
            "在庫組合與進場": terms.get("selection_entry"),
            "現金／未投入": terms.get("cash_drag"),
            "已實現": terms.get("realized"),
        }
        available_drivers = {
            name: value for name, value in drivers.items() if value is not None
        }
        dominant_driver = (
            max(available_drivers, key=lambda name: abs(available_drivers[name]))
            if available_drivers
            else None
        )

        strategies[strategy_id] = {
            "label": label,
            "actual_return": actual_return,
            "card_return": card_return,
            "gap": actual_return - card_return if card_return is not None else None,
            "deployed_fraction": float(base["weight"]),
            "cash_twd": float(base["cash_twd"]),
            "realized_pnl_twd": float(base["realized_pnl_twd"]),
            "card_members": len(card_codes),
            "covered_members": len(covered),
            "coverage_fraction": len(covered) / len(card_codes) if card_codes else None,
            "covered_codes": covered,
            "missing_codes": missing,
            "stale_codes": stale,
            "planned_entry_codes": planned_entry,
            "planned_exit_codes": planned_exit,
            "covered_card_average_return": covered_average,
            "missing_card_average_return": missing_average,
            "weighted_entry_gap": weighted_entry_gap,
            "drivers": drivers,
            "dominant_driver": dominant_driver,
            "active_vs_benchmarks": active_vs_benchmarks,
            "execution": _execution_summary(slippage_rows, strategy_id),
            "gap_history": history,
            "gap_change_5_observations_pp": five_observation_change,
            "best_gap_pp": max((row["gap_pp"] for row in history), default=None),
            "worst_gap_pp": min((row["gap_pp"] for row in history), default=None),
        }

        total_card += len(card_codes)
        total_covered += len(covered)
        total_cost += float(base["position_cost_twd"])
        total_actual += actual_return
        total_card_return += card_return or 0.0

    count = len(strategy_labels) or 1
    combined_actual = total_actual / count
    combined_card = total_card_return / count
    combined_vs_benchmarks = {
        benchmark_id: (
            combined_actual - value if value is not None else None
        )
        for benchmark_id, value in benchmark_returns.items()
    }

    return {
        "asof": asof.isoformat(),
        "method": "DESCRIPTIVE_EXACT_ALGEBRA_NOT_CAUSAL_ATTRIBUTION",
        "strategies": strategies,
        "benchmarks": benchmark_returns,
        "summary": {
            "combined_actual_return": combined_actual,
            "combined_card_return": combined_card,
            "combined_gap": combined_actual - combined_card,
            "coverage_fraction": total_covered / total_card if total_card else None,
            "covered_members": total_covered,
            "card_members": total_card,
            "deployed_fraction": total_cost / (500_000.0 * count),
            "combined_vs_benchmarks": combined_vs_benchmarks,
            "execution_observations": sum(
                row["execution"]["observations"] for row in strategies.values()
            ),
            "execution_status": (
                "OK_MIN_30"
                if sum(row["execution"]["observations"] for row in strategies.values())
                >= MIN_EXECUTION_OBSERVATIONS
                else "WAITING_MIN_30_SIGNAL_FILLS"
            ),
            "gap_history_observations": len(all_history_dates),
        },
    }

