from __future__ import annotations

import importlib.util
import math
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_gap  # noqa: E402


SPEC = importlib.util.spec_from_file_location(
    "gap_dashboard", ROOT / "scripts" / "build_dashboard.py"
)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def test_gap_report_keeps_coverage_benchmark_and_execution_evidence_separate() -> None:
    d0, d1 = date(2026, 8, 10), date(2026, 8, 11)
    bridge = {
        "A": {
            "sleeve_return": 0.02,
            "r_card": 0.05,
            "weight": 0.50,
            "cash_twd": 250_000.0,
            "realized_pnl_twd": 0.0,
            "position_cost_twd": 250_000.0,
            "terms": {
                "selection_entry": -0.005,
                "cash_drag": -0.025,
                "realized": 0.0,
            },
            "names": [
                {
                    "stock_code": "A1",
                    "entry_gap": 0.01,
                    "cost_twd": 150_000.0,
                },
                {
                    "stock_code": "A3",
                    "entry_gap": -0.01,
                    "cost_twd": 100_000.0,
                },
            ],
        }
    }
    report = strategy_gap.analyze(
        strategy_labels={"A": "策略 A"},
        bridge=bridge,
        latest_signals=[
            {
                "strategy_id": "A",
                "stock_code": "A1",
                "signal": "抱",
                "signed_return_pct": 4.0,
            },
            {
                "strategy_id": "A",
                "stock_code": "A2",
                "signal": "進",
                "signed_return_pct": 6.0,
            },
        ],
        actual_curves={"A": [(d0, 100.0), (d1, 102.0)]},
        card_curves={"A": [(d0, 100.0), (d1, 105.0)]},
        benchmark_curves={"TAIEX": [(d0, 100.0), (d1, 101.0)]},
        slippage_rows=[{"strategy_id": "A", "slippage_vs_signal_bp": 12.0}],
        asof=d1,
    )

    row = report["strategies"]["A"]
    assert row["covered_codes"] == ["A1"]
    assert row["missing_codes"] == ["A2"]
    assert row["stale_codes"] == ["A3"]
    assert row["planned_entry_codes"] == ["A2"]
    assert math.isclose(row["coverage_fraction"], 0.5)
    assert math.isclose(row["missing_card_average_return"], 0.06)
    assert math.isclose(row["weighted_entry_gap"], 0.002)
    assert math.isclose(row["active_vs_benchmarks"]["TAIEX"], 0.01)
    assert row["execution"]["status"] == "WAITING_MIN_30_SIGNAL_FILLS"
    assert report["summary"]["execution_status"] == "WAITING_MIN_30_SIGNAL_FILLS"
    assert "counterfactual_pnl" not in row


def test_live_bridge_terms_are_an_exact_algebraic_identity() -> None:
    prices = dashboard.analytics.load_price_history(dashboard.PRICE_HISTORY_PATH)
    fills = dashboard.load_actual_fills() + dashboard.load_unrecorded_exits()
    cards = dashboard.load_strategy_cards()
    signals = dashboard.load_latest_strategy_signals()
    asof = max(day for day, _ in prices["TAIEX"])
    bridge = dashboard.implementation_bridge(fills, prices, cards, signals, asof)

    for strategy_id in dashboard.STRATEGY_LABELS:
        row = bridge[strategy_id]
        assert row["terms"] is not None
        assert math.isclose(
            sum(row["terms"].values()), row["gap"], rel_tol=0.0, abs_tol=1e-12
        )
        assert math.isclose(
            row["sleeve_return"],
            row["weight"] * row["r_positions"]
            + row["realized_pnl_twd"] / dashboard.STRATEGY_BUDGET_TWD,
            rel_tol=0.0,
            abs_tol=1e-12,
        )


def test_current_gap_report_surfaces_missing_and_stale_names_without_inventing_pnl() -> None:
    prices = dashboard.analytics.load_price_history(dashboard.PRICE_HISTORY_PATH)
    fills = dashboard.load_actual_fills() + dashboard.load_unrecorded_exits()
    cards = dashboard.load_strategy_cards()
    signals = dashboard.load_latest_strategy_signals()
    benchmarks = dashboard.load_grouped_levels(
        dashboard.BENCHMARK_NAV_PATH, "benchmark_id", "level"
    )
    holdings = dashboard.load_holdings()
    asof = max(day for day, _ in prices["TAIEX"])
    actual, _, _ = dashboard.build_four_strategy_actual(fills, prices, holdings, asof)
    bridge = dashboard.implementation_bridge(fills, prices, cards, signals, asof)
    slippage = dashboard.analytics.build_slippage_ledger(
        dashboard.SIGNAL_FILLS_PATH,
        dashboard.analytics.load_ohlc(dashboard.PRICE_HISTORY_PATH),
    )
    report = strategy_gap.analyze(
        strategy_labels=dashboard.STRATEGY_LABELS,
        bridge=bridge,
        latest_signals=signals,
        actual_curves=actual,
        card_curves=cards,
        benchmark_curves=benchmarks,
        slippage_rows=slippage,
        asof=asof,
    )

    for strategy_id, row in report["strategies"].items():
        assert row["covered_members"] <= row["card_members"]
        assert math.isclose(
            row["gap"], row["actual_return"] - row["card_return"], abs_tol=1e-12
        )
        assert "counterfactual_pnl" not in row
        assert set(row["covered_codes"]).isdisjoint(row["missing_codes"])

    # These states come from the latest files, not hard-coded expected counts.
    planned = {
        row["stock_code"]
        for row in signals
        if row["strategy_id"] == "TRUST" and row["signal"] == "進"
    }
    assert planned == set(report["strategies"]["TRUST"]["planned_entry_codes"])
