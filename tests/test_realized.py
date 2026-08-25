"""Realized profit and loss must come from settled cash, never from a mark."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_dashboard  # noqa: E402
import realized  # noqa: E402


def fill(**overrides):
    base = {
        "trade_id": "T1",
        "strategy_id": "YOY",
        "stock_code": "9999",
        "stock_name": "測試",
        "side": "BUY",
        "date": date(2026, 8, 11),
        "fill_price": 100.0,
        "shares": 1000.0,
        "cash_out": 100_142.0,
        "cash_in": 0.0,
    }
    base.update(overrides)
    return base


def test_realized_uses_settled_cash_not_price_difference() -> None:
    """A round trip must net fee and tax, not just (sell price - buy price)."""
    lots = realized.closed_lots(
        [
            fill(),
            fill(
                trade_id="T2",
                side="SELL",
                date=date(2026, 8, 19),
                fill_price=90.0,
                cash_out=0.0,
                cash_in=89_573.0,
            ),
        ]
    )
    assert len(lots) == 1
    lot = lots[0]
    assert lot["realized_pnl_twd"] == pytest.approx(89_573.0 - 100_142.0)
    # The naive price-only answer would be -10,000; costs make it worse.
    assert lot["realized_pnl_twd"] < -10_000
    assert lot["holding_days"] == 8


def test_the_real_3702_round_trip_reconciles_to_the_fill_book() -> None:
    """The one closed position in the live book must match its own cash flows."""
    fills = build_dashboard.load_actual_fills()
    lots = realized.closed_lots(fills)
    closed = [lot for lot in lots if lot["stock_code"] == "3702"]
    assert len(closed) == 1, "3702 is the only closed position in the fill book"
    lot = closed[0]

    paid = sum(
        row["cash_out"] for row in fills if row["stock_code"] == "3702" and row["side"] == "BUY"
    )
    received = sum(
        row["cash_in"] for row in fills if row["stock_code"] == "3702" and row["side"] == "SELL"
    )
    assert lot["realized_pnl_twd"] == pytest.approx(received - paid)
    assert lot["realized_pnl_twd"] < 0, "3702 closed at a loss; never round it away"
    assert lot["strategy_id"] == "YOY"


def test_a_sell_without_a_matching_buy_fails_closed() -> None:
    with pytest.raises(realized.RealizedError):
        realized.closed_lots(
            [
                fill(
                    side="SELL",
                    fill_price=90.0,
                    cash_out=0.0,
                    cash_in=89_573.0,
                )
            ]
        )


def test_one_sell_across_two_buys_splits_into_two_lots() -> None:
    """FIFO keeps each entry price and holding period honest."""
    lots = realized.closed_lots(
        [
            fill(trade_id="B1", shares=400.0, cash_out=40_000.0),
            fill(
                trade_id="B2",
                date=date(2026, 8, 13),
                fill_price=110.0,
                shares=600.0,
                cash_out=66_000.0,
            ),
            fill(
                trade_id="S1",
                side="SELL",
                date=date(2026, 8, 20),
                fill_price=120.0,
                shares=1000.0,
                cash_out=0.0,
                cash_in=119_000.0,
            ),
        ]
    )
    assert [lot["shares"] for lot in lots] == [400.0, 600.0]
    assert sum(lot["realized_pnl_twd"] for lot in lots) == pytest.approx(
        119_000.0 - 40_000.0 - 66_000.0
    )
    assert [lot["holding_days"] for lot in lots] == [9, 7]


def test_realized_and_unrealized_never_double_count_the_same_share() -> None:
    """Closed shares leave the open book; open cost is FIFO-reduced."""
    fills = build_dashboard.load_actual_fills()
    prices = build_dashboard.analytics.load_price_history(build_dashboard.PRICE_HISTORY_PATH)
    asof = max(day for points in prices.values() for day, _ in points)
    split = build_dashboard.pnl_split(fills, prices, asof)

    open_shares = realized.open_lots(fills)
    assert ("YOY", "3702") not in open_shares, "3702 is fully closed"

    yoy = split["YOY"]
    assert yoy["closed_lots"] == 1
    assert yoy["realized_pnl_twd"] < 0
    assert yoy["total_pnl_twd"] == pytest.approx(
        yoy["realized_pnl_twd"] + yoy["unrealized_pnl_twd"]
    )
    # 3702 contributes to realized only; its cost must be gone from the open book.
    assert yoy["open_cost_twd"] == pytest.approx(
        sum(
            row["cash_out"]
            for row in fills
            if row["strategy_id"] == "YOY" and row["side"] == "BUY" and row["stock_code"] != "3702"
        )
    )
