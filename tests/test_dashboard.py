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

    # Pinning one day's totals here made the suite fail on every new paste.
    # The invariant is that the rows add up to the broker's own subtotal.
    assert holdings, "the newest snapshot must contain at least one position"
    assert result["shares"] == sum(row["shares"] for row in holdings)
    assert result["current_value_twd"] == sum(row["current_value_twd"] for row in holdings)
    assert result["cost_basis_twd"] == sum(row["cost_basis_twd"] for row in holdings)
    assert result["unrealized_pnl_twd"] == sum(row["unrealized_pnl_twd"] for row in holdings)
    assert math.isclose(
        result["unrealized_return"],
        result["unrealized_pnl_twd"] / result["cost_basis_twd"],
    )
    assert result["reconciliation"] == "PASS"
    # The build must read the newest paste, not a filename pinned in code.
    assert dashboard.HOLDINGS_PATH.stem.endswith(summary["asof_date"].isoformat())
    assert dashboard.SUMMARY_PATH.stem.endswith(summary["asof_date"].isoformat())


def test_daily_price_contribution_is_labeled_estimate_and_reproducible() -> None:
    holdings = dashboard.load_holdings()
    result = dashboard.snapshot_analytics(holdings, dashboard.load_summary())

    expected = sum(row["shares"] * row["price_change"] for row in holdings)
    assert math.isclose(
        result["estimated_daily_price_contribution_twd"], expected, abs_tol=0.001
    )
    gross_now = sum(row["shares"] * row["last_price"] for row in holdings)
    opening_value = gross_now - expected
    assert math.isclose(
        result["estimated_gross_daily_return"], expected / opening_value, abs_tol=1e-9
    )
    top = max(
        holdings, key=lambda row: row["estimated_daily_price_contribution_twd"]
    )
    bottom = min(
        holdings, key=lambda row: row["estimated_daily_price_contribution_twd"]
    )
    # Which name leads changes daily; that the extremes come from the same
    # shares x price-change arithmetic as the total does not.
    assert top["estimated_daily_price_contribution_twd"] == (
        top["shares"] * top["price_change"]
    )
    assert bottom["estimated_daily_price_contribution_twd"] == (
        bottom["shares"] * bottom["price_change"]
    )
    assert top["estimated_daily_price_contribution_twd"] >= (
        bottom["estimated_daily_price_contribution_twd"]
    )


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
    # A new strategy card arrives every trading day, so assert the fail-closed
    # RULE rather than one day's constants -- pinning the count made the daily
    # GitHub Action fail on every fresh card.
    observations = receipt["history"]["analysis_return_observations"]
    assert observations >= 10
    expected_gate = "OK" if observations >= dashboard.MIN_RISK_RETURN_OBS else "WAITING_MIN_20_RETURNS"
    assert receipt["history"]["risk_metric_status"] == expected_gate

    # The card asof must track the newest row actually present in the input.
    card_rows = dashboard.read_csv(dashboard.INPUTS / "strategy_card_returns.csv")
    assert receipt["history"]["strategy_card_asof"] == max(row["asof_date"] for row in card_rows)

    latest = receipt["history"]["latest_strategy_signals"]
    signal_rows = dashboard.read_csv(dashboard.INPUTS / "latest_strategy_signals.csv")
    assert latest["asof_date"] == max(row["asof_date"] for row in signal_rows)

    # Every planned entry/exit stays labelled as a plan until a real fill lands.
    for action in latest["planned_actions"]:
        assert action["actual_fill_status"] == "WAITING_ACTUAL_FILL"
        assert action["signal"] in {"進", "出"}

    # The YOY card header has never matched the mean of its own visible members;
    # that divergence must keep surfacing instead of being quietly averaged away.
    assert latest["quality"]["YOY"]["status"] == "SOURCE_CHECKSUM_MISMATCH"
    assert abs(latest["quality"]["YOY"]["gap_pp"]) > 0.1
    assert latest["quality"]["MARGIN"]["status"] == "PASS"
    diagnostics = receipt["history"]["strategy_diagnostics"]
    assert diagnostics["reconciliation"] == "PASS_EXCLUDING_UNASSIGNED_2886"
    # Was 28_446 under the old dual valuation path (snapshot gross vs close
    # net). The amount moves with the market, so assert the PATH: bundle P&L
    # must equal the four sleeves' net-liquidation value less their budgets,
    # never a snapshot-gross figure.
    sleeve_total = sum(
        diagnostics[strategy_id]["cash_twd"]
        + diagnostics[strategy_id]["liquidation_value_twd"]
        for strategy_id in dashboard.STRATEGY_LABELS
    )
    budget_total = dashboard.STRATEGY_BUDGET_TWD * len(dashboard.STRATEGY_LABELS)
    assert math.isclose(
        diagnostics["bundle_current_pnl_twd"], sleeve_total - budget_total, abs_tol=0.5
    )
    # The gap between the fill book and the broker's cost column is disclosed,
    # not smoothed. Its size moves with each new fill; that it stays derived
    # from those two sources is the part worth asserting.
    assert diagnostics["active_cost_basis_gap_twd"] == (
        diagnostics["active_fill_cash_out_twd"]
        - diagnostics["source_active_cost_ex_unassigned_twd"]
    )
    assert diagnostics["active_fill_cash_out_twd"] > 0
    assert diagnostics["TRUST"]["active_positions"]["2301"] == 365
    assert diagnostics["YOY"]["active_positions"]["2301"] == 261
    assert diagnostics["MARGIN"]["active_positions"]["1709"] == 305
    assert diagnostics["BREAKOUT"]["active_positions"]["1709"] == 3644
    assert receipt["safety"]["network_access"] is False
    assert receipt["safety"]["order_capability"] is False
    pasted = dashboard.load_summary()
    assert f"NT$ {pasted['unrealized_pnl_twd']:+,.0f}" in content
    rendered_pnl = f"NT$ {diagnostics['bundle_current_pnl_twd']:+,.0f}"
    assert rendered_pnl in content
    assert "差 NT$653" in content
    assert "ACTUAL_FOUR_STRATEGY_LIQUIDATION_NAV" in content
    assert "THEORY_ASOF_" in content
    assert f"最新四策略卡 · {latest['asof_date']} 收盤" in content
    assert "2637 慧洋-KY" in content
    # The first signal that completed the plan -> fill lifecycle.
    assert "訊號 → 成交 · 履約落差帳" in content
    assert 'href="inputs/four_strategy_daily_signals.xlsx"' in content
    assert "SOURCE_CHECKSUM_MISMATCH" not in content
    assert "YOY 來源矛盾" in content
    assert "SIMULATED_CONSTANT_HOLDINGS" not in content
    assert "242 個交易日" not in content
    assert "日／週／月／季／年／YTD／累計" in content
    assert "Sharpe／MDD／Alpha／Beta · 完整績效風險衡量" in content
    assert content.index("Sharpe／MDD／Alpha／Beta") < content.index("歷史期間報酬")
    assert f"RISK_SAMPLE_{observations}_RETURNS" in content
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
    # The fill book the build actually uses: settled fills plus any provisional
    # exit standing in for a confirmation that has not arrived. Reading only
    # the settled half here would test a book the page never renders.
    fills = sorted(
        dashboard.load_actual_fills() + dashboard.load_unrecorded_exits(),
        key=lambda row: row["date"],
    )
    prices = dashboard.analytics.load_price_history(dashboard.PRICE_HISTORY_PATH)
    holdings = dashboard.load_holdings()
    # The snapshot's own date, not a pinned one -- the build discovers the
    # newest paste, so pinning a day here would fail on every new one.
    asof = holdings[0]["asof_date"]

    curves, bundle, diagnostics = dashboard.build_four_strategy_actual(
        fills, prices, holdings, asof
    )

    assert diagnostics["reconciliation"] == "PASS_EXCLUDING_UNASSIGNED_2886"
    assert (
        diagnostics["valuation_basis"]
        == "OFFICIAL_CLOSE_ESTIMATED_LIQUIDATION_CARRY_FORWARD_POSITIONS"
    )
    # One basis for the whole curve: the bundle level and the P&L it implies
    # must be the same statement, on the snapshot day as on every other day.
    budget = dashboard.STRATEGY_BUDGET_TWD * len(dashboard.STRATEGY_LABELS)
    assert math.isclose(
        bundle[-1][1],
        (budget + diagnostics["bundle_current_pnl_twd"]) / budget * 100.0,
        abs_tol=0.001,
    )
    sleeve_total = sum(
        diagnostics[strategy_id]["cash_twd"]
        + diagnostics[strategy_id]["liquidation_value_twd"]
        for strategy_id in dashboard.STRATEGY_LABELS
    )
    assert math.isclose(
        diagnostics["bundle_current_pnl_twd"], sleeve_total - budget, abs_tol=0.5
    )


def test_slippage_ledger_measures_execution_against_the_right_reference() -> None:
    """A buy filled above its reference is a COST, and must read positive bp.

    The sign convention is the whole point: if adverse slippage rendered
    negative it would look like a gain on the dashboard.
    """
    import analytics

    ohlc = {
        "9999": {
            date(2026, 8, 25): {"open": 100.0, "high": 110.0, "low": 95.0, "close": 96.0}
        }
    }
    ledger_csv = ROOT / "tests" / "_tmp_signal_fills.csv"
    ledger_csv.write_text(
        "signal_date,effective_date,strategy_id,stock_code,stock_name,action,"
        "signal_ref_price,signal_basis,fill_date,fill_time,fill_price,shares,source\n"
        "2026-08-24,2026-08-25,MARGIN,9999,測試股,BUY,100.0,NEXT_OPEN,"
        "2026-08-25,10:00:00,101.0,1000,test\n",
        encoding="utf-8",
    )
    try:
        rows = analytics.build_slippage_ledger(ledger_csv, ohlc)
    finally:
        ledger_csv.unlink()

    assert len(rows) == 1
    row = rows[0]
    # Paid 101 against a 100 reference: 100 bp adverse, positive by convention.
    assert math.isclose(row["slippage_vs_signal_bp"], 100.0)
    assert math.isclose(row["slippage_vs_open_bp"], 100.0)
    assert row["delay_days"] == 1
    # Filled at 101 in a 95-110 range -> 40% of the way up the day's range.
    assert math.isclose(row["fill_range_position"], (101.0 - 95.0) / (110.0 - 95.0))
    assert math.isclose(row["mfe_pct"], 110.0 / 101.0 - 1.0)
    assert math.isclose(row["mae_pct"], 95.0 / 101.0 - 1.0)
    assert math.isclose(row["close_vs_fill_pct"], 96.0 / 101.0 - 1.0)
    # Limit up on a 100 reference is 110.00, and the high touched it.
    assert math.isclose(row["limit_up_price"], 110.0)
    assert row["hit_limit_up"] is True


def test_slippage_sign_flips_for_sells() -> None:
    """Selling BELOW the reference is the adverse direction for a sell."""
    import analytics

    ohlc = {"9999": {date(2026, 8, 25): {"open": 100.0, "high": 101.0, "low": 98.0, "close": 99.0}}}
    ledger_csv = ROOT / "tests" / "_tmp_signal_fills_sell.csv"
    ledger_csv.write_text(
        "signal_date,effective_date,strategy_id,stock_code,stock_name,action,"
        "signal_ref_price,signal_basis,fill_date,fill_time,fill_price,shares,source\n"
        "2026-08-24,2026-08-25,MARGIN,9999,測試股,SELL,100.0,NEXT_OPEN,"
        "2026-08-25,10:00:00,99.0,1000,test\n",
        encoding="utf-8",
    )
    try:
        row = analytics.build_slippage_ledger(ledger_csv, ohlc)[0]
    finally:
        ledger_csv.unlink()

    # Sold 1% below the reference -> 100 bp adverse, still positive.
    assert math.isclose(row["slippage_vs_signal_bp"], 100.0)


def test_real_2637_fill_landed_exactly_on_the_official_open() -> None:
    """Regression on the first real signal->fill sample."""
    import analytics

    rows = analytics.build_slippage_ledger(
        dashboard.SIGNAL_FILLS_PATH, analytics.load_ohlc(dashboard.PRICE_HISTORY_PATH)
    )
    row = next(item for item in rows if item["stock_code"] == "2637")

    assert math.isclose(row["fill_price"], 97.80)
    assert math.isclose(row["day_open"], 97.80)
    assert math.isclose(row["slippage_vs_open_bp"], 0.0, abs_tol=1e-6)
    # The 8/24 close of 97.1 was the signal reference, so vs-signal is adverse.
    assert row["slippage_vs_signal_bp"] > 0
    # It ran to 105.00 intraday but limit up was 106.81 -- it never locked up.
    assert row["hit_limit_up"] is False
    assert math.isclose(row["day_high"], 105.0)


def test_roc_date_parsers_handle_both_official_formats() -> None:
    """Regression: the OpenAPI feed stamps dates ROC-compact, not AD.

    Reading '1150824' as AD YYYYMMDD yields year 1150 and silently drops
    every row, which is exactly how the fallback failed the first time.
    """
    import fetch_prices

    assert fetch_prices.roc_compact_to_date("1150824") == date(2026, 8, 24)
    assert fetch_prices.roc_compact_to_date("1141231") == date(2025, 12, 31)
    # STOCK_DAY uses the punctuated form for the same day.
    assert fetch_prices.roc_to_date("115/08/24") == date(2026, 8, 24)
    assert fetch_prices.roc_to_date("115年08月24日") == date(2026, 8, 24)

    for bad in ("", "abc", "12"):
        try:
            fetch_prices.roc_compact_to_date(bad)
        except fetch_prices.FetchError:
            continue
        raise AssertionError(f"{bad!r} should have failed closed")
