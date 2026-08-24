from __future__ import annotations

import importlib.util
import math
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "performance_dashboard_builder", ROOT / "scripts" / "build_dashboard.py"
)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dashboard)


def test_pasted_snapshot_reconciles_exact_source_subtotal() -> None:
    holdings = dashboard.load_holdings()
    summary = dashboard.load_summary()
    result = dashboard.snapshot_analytics(holdings, summary)

    assert len(holdings) == 15
    assert result["shares"] == 12_169
    assert result["current_value_twd"] == 1_218_013
    assert result["cost_basis_twd"] == 1_177_906
    assert result["unrealized_pnl_twd"] == 40_107
    assert math.isclose(result["unrealized_return"], 40_107 / 1_177_906)
    assert result["reconciliation"] == "PASS"


def test_daily_price_contribution_is_labeled_estimate_and_reproducible() -> None:
    holdings = dashboard.load_holdings()
    result = dashboard.snapshot_analytics(holdings, dashboard.load_summary())

    assert math.isclose(
        result["estimated_daily_price_contribution_twd"], 24_217.55, abs_tol=0.001
    )
    assert math.isclose(
        result["estimated_gross_daily_return"], 0.02019484156390432
    )
    top = max(
        holdings, key=lambda row: row["estimated_daily_price_contribution_twd"]
    )
    bottom = min(
        holdings, key=lambda row: row["estimated_daily_price_contribution_twd"]
    )
    assert top["stock_code"] == "2301"
    assert bottom["stock_code"] == "2408"


def test_twr_adjusts_external_cash_flow() -> None:
    rows = [
        {"date": date(2026, 8, 20), "value": 100.0, "flow": 0.0},
        {"date": date(2026, 8, 21), "value": 111.0, "flow": 10.0},
        {"date": date(2026, 8, 24), "value": 113.22, "flow": 0.0},
    ]
    curve = dashboard.build_twr(rows)

    assert math.isclose(curve[1][1], 101.0)
    assert math.isclose(curve[2][1], 103.02)


def test_short_history_does_not_fabricate_risk_metrics() -> None:
    curve = [
        (date(2026, 8, 20), 100.0),
        (date(2026, 8, 21), 101.0),
        (date(2026, 8, 24), 99.0),
    ]
    metrics = dashboard.performance_metrics(curve)

    assert math.isclose(metrics["total_return"], -0.01)
    assert metrics["max_drawdown"] < 0
    assert metrics["sharpe"] is None
    assert metrics["sortino"] is None


def test_period_returns_cover_daily_weekly_monthly_quarterly_year_and_cumulative() -> None:
    curve = [
        (date(2025, 12, 31), 100.0),
        (date(2026, 1, 2), 101.0),
    ]
    for index in range(2, 260):
        curve.append((date(2026, 1, 2).fromordinal(date(2026, 1, 2).toordinal() + index), 101.0 + index * 0.1))
    values = dashboard.trailing_returns(curve)

    assert set(values) == {"今日", "近一週", "近一月", "近一季", "近一年", "YTD", "累計"}
    assert values["近一年"] is None
    assert all(value is not None for key, value in values.items() if key != "近一年")
    assert dashboard.period_end_returns(curve, "week")
    assert dashboard.period_end_returns(curve, "month")
    assert dashboard.period_end_returns(curve, "quarter")
    assert dashboard.period_end_returns(curve, "year")


def test_trailing_one_year_uses_calendar_coverage_not_fixed_252_rows() -> None:
    curve = [
        (date(2025, 8, 25), 100.0),
        (date(2026, 8, 24), 125.0),
    ]

    assert dashboard.trailing_returns(curve)["近一年"] == 0.25


def test_build_creates_offline_html_and_fail_closed_statuses() -> None:
    output, receipt = dashboard.build()
    content = output.read_text(encoding="utf-8")

    assert receipt["status"] == "SUCCESS"
    assert receipt["snapshot_reconciliation"] == "PASS"
    assert receipt["history"]["account_nav_observations"] == 1
    assert receipt["history"]["performance_status"] == "ACTUAL_FILLS_RECONCILED"
    assert receipt["history"]["analysis_basis"] == "ACTUAL_FOUR_STRATEGY_LIQUIDATION_NAV"
    assert receipt["history"]["analysis_return_observations"] == 10
    assert receipt["history"]["risk_metric_status"] == "WAITING_MIN_20_RETURNS"
    assert receipt["history"]["strategy_card_asof"] == "2026-08-24"
    latest = receipt["history"]["latest_strategy_signals"]
    assert latest["asof_date"] == "2026-08-24"
    assert latest["planned_effective_date"] == "2026-08-25"
    assert latest["planned_actions"] == [
        {
            "strategy_id": "MARGIN",
            "stock_code": "2646",
            "signal": "出",
            "actual_fill_status": "WAITING_ACTUAL_FILL",
        },
        {
            "strategy_id": "MARGIN",
            "stock_code": "2637",
            "signal": "進",
            "actual_fill_status": "WAITING_ACTUAL_FILL",
        },
    ]
    assert latest["quality"]["YOY"]["status"] == "SOURCE_CHECKSUM_MISMATCH"
    assert math.isclose(latest["quality"]["YOY"]["gap_pp"], 0.6333333333333329)
    assert latest["quality"]["MARGIN"]["status"] == "PASS"
    diagnostics = receipt["history"]["strategy_diagnostics"]
    assert diagnostics["reconciliation"] == "PASS_EXCLUDING_UNASSIGNED_2886"
    # Was 28_446 under the old dual valuation path (snapshot gross vs close
    # net); unifying on net liquidation removes the ~NT$1.45 rounding gap.
    assert math.isclose(diagnostics["bundle_current_pnl_twd"], 28_444.55, abs_tol=0.01)
    assert diagnostics["active_fill_cash_out_twd"] == 1_178_519
    assert diagnostics["source_active_cost_ex_unassigned_twd"] == 1_177_866
    assert diagnostics["active_cost_basis_gap_twd"] == 653
    assert diagnostics["TRUST"]["active_positions"]["2301"] == 365
    assert diagnostics["YOY"]["active_positions"]["2301"] == 261
    assert diagnostics["MARGIN"]["active_positions"]["1709"] == 305
    assert diagnostics["BREAKOUT"]["active_positions"]["1709"] == 3644
    assert receipt["safety"]["network_access"] is False
    assert receipt["safety"]["order_capability"] is False
    assert "NT$ +40,107" in content
    # 28_444.55 net-basis PnL renders as NT$ +28,445 (was 28,446 dual-path).
    assert "NT$ +28,445" in content
    assert "差 NT$653" in content
    assert "ACTUAL_FOUR_STRATEGY_LIQUIDATION_NAV" in content
    assert "THEORY_ASOF_2026-08-24" in content
    assert "最新四策略卡 · 2026-08-24 收盤" in content
    assert "2637 慧洋-KY" in content
    assert "2646 星宇航空" in content
    assert "等待實際成交" in content
    assert 'href="inputs/four_strategy_daily_signals.xlsx"' in content
    assert "SOURCE_CHECKSUM_MISMATCH" not in content
    assert "YOY 來源矛盾" in content
    assert "SIMULATED_CONSTANT_HOLDINGS" not in content
    assert "242 個交易日" not in content
    assert "日／週／月／季／年／YTD／累計" in content
    assert "Sharpe／MDD／Alpha／Beta · 完整績效風險衡量" in content
    assert content.index("Sharpe／MDD／Alpha／Beta") < content.index("歷史期間報酬")
    assert "RISK_SAMPLE_10_RETURNS" in content
    assert "實際 vs 理論 · 四策略差異" in content
    assert "月度績效熱圖" in content
    assert "NO_BROKER · NO_ORDER" in content
    assert "亞德客-KY" in content


def _synthetic_fill(trade_id: str, day: str, price: str, shares: str) -> dict[str, Any]:
    """One BUY row shaped like load_actual_fills() output (raw + parsed)."""
    consideration = float(price) * float(shares)
    fee = int(consideration * 0.001425)
    return {
        "trade_id": trade_id,
        "strategy_id": "BREAKOUT",
        "stock_code": "6000",
        "stock_name": "測試股",
        "side": "BUY",
        "fill_date": day,
        "fill_price": price,
        # load_actual_fills() overwrites this same key with a float.
        "shares": float(shares),
        "consideration_twd": f"{consideration:.0f}",
        "fee_twd": str(fee),
        "tax_twd": "0",
        "cash_out_twd": f"{consideration + fee:.0f}",
        "cash_in_twd": "0",
        "currency": "TWD",
        "source": "synthetic_test",
        # Parsed fields exactly as load_actual_fills() derives them.
        "date": date.fromisoformat(day),
        "price": float(price),
        "shares_f": float(shares),
        "cash_out": consideration + fee,
        "cash_in": 0.0,
    }


def test_snapshot_day_is_valued_at_net_liquidation_like_every_other_day() -> None:
    """Regression for audit item #01.

    The four-strategy curve must never consult owner-holdings data: before
    the fix, the snapshot day silently substituted broker-screen values into
    the curve, creating a second valuation path. Here a deliberately absurd
    snapshot value must have ZERO effect on the curve.
    """
    fills = [
        _synthetic_fill("T1", "2026-08-20", "100", "4000"),
    ]
    prices = {
        "6000": [
            (date(2026, 8, 20), 100.0),
            (date(2026, 8, 21), 101.0),
        ]
    }

    def build(holdings: list[dict[str, Any]]) -> tuple[dict[date, float], dict[str, Any]]:
        curves, _, diagnostics = dashboard.build_four_strategy_actual(
            fills, prices, holdings, date(2026, 8, 21)
        )
        return dict(curves["BREAKOUT"]), diagnostics

    def holding_with(value_twd: float) -> list[dict[str, Any]]:
        # Shares must reconcile with the fill; the VALUE is the poison under
        # test -- the old code leaked it into the snapshot-day curve.
        return [
            {
                "stock_code": "6000",
                "stock_name": "測試股",
                "shares": 4000.0,
                "current_value_twd": value_twd,
                "cost_basis_twd": 1.0,
                "last_price": 101.0,
            }
        ]

    curve_a, diag_a = build(holding_with(999_999_999.0))
    curve_b, diag_b = build(holding_with(1.0))

    # Snapshot values have ZERO effect: absurd vs near-zero produce identical
    # curves, valued purely at official close net liquidation.
    assert curve_a == curve_b
    # Net liquidation, not gross: 404000 -> fee 575 + tax 1212 -> 402213.
    assert math.isclose(
        curve_b[date(2026, 8, 21)],
        (dashboard.STRATEGY_BUDGET_TWD - 400570 + 404000 - 575 - 1212)
        / dashboard.STRATEGY_BUDGET_TWD
        * 100.0,
    )
    for diagnostics in (diag_a, diag_b):
        assert (
            diagnostics["valuation_basis"]
            == "OFFICIAL_CLOSE_ESTIMATED_LIQUIDATION_CARRY_FORWARD_POSITIONS"
        )


def test_real_data_bundle_stays_on_one_valuation_basis_through_snapshot_day() -> None:
    """The 8/24 owner snapshot day must not switch valuation bases mid-curve.

    Locks the corrected numbers AND the fact that unifying the basis left the
    published 8/24 level essentially unchanged -- the broker screen's 現值
    column already carries fee+tax, so the audited 'fake +0.27pp' claim does
    not reproduce (the real two-path gap was ~NT$17 of rounding).
    """
    fills = dashboard.load_actual_fills()
    prices = dashboard.analytics.load_price_history(dashboard.PRICE_HISTORY_PATH)
    holdings = dashboard.load_holdings()

    curves, bundle, diagnostics = dashboard.build_four_strategy_actual(
        fills, prices, holdings, date(2026, 8, 24)
    )

    assert diagnostics["reconciliation"] == "PASS_EXCLUDING_UNASSIGNED_2886"
    assert (
        diagnostics["valuation_basis"]
        == "OFFICIAL_CLOSE_ESTIMATED_LIQUIDATION_CARRY_FORWARD_POSITIONS"
    )
    assert math.isclose(bundle[-1][1], 101.4222, abs_tol=0.001)
    assert math.isclose(diagnostics["bundle_current_pnl_twd"], 28_444.55, abs_tol=0.5)
