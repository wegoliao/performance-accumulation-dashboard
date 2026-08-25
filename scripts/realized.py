"""Realized profit and loss from the actual fill book.

The sleeve curves in ``build_dashboard`` already carry realized results
implicitly: a sell adds ``cash_in`` back to the sleeve, so a loss shows up as a
smaller cash balance and the curve is correct. What was missing is the number
itself. A reader looking at "YOY +0.79%" cannot see that a position was closed
at a loss inside it, and a loss you cannot point at is a loss you will not
learn from.

This module matches sells against buys FIFO, per ``(strategy_id, stock_code)``,
and reports each closed lot with the cash that actually moved -- consideration
plus fee on the way in, consideration minus fee and tax on the way out. No
mark, no estimate: only settled cash.

Pure standard library. No network, no broker, no order path.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date
from typing import Any, Iterable, Sequence


class RealizedError(ValueError):
    """Fill book contradiction that must fail closed."""


def _unit(cash: float, shares: float) -> float:
    if shares <= 0:
        raise RealizedError("cannot derive a unit price from non-positive shares")
    return cash / shares


def closed_lots(fills: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """FIFO-match sells against buys and return one row per closed lot.

    Each row is a *portion* of a sell matched to a *portion* of a buy, so a sell
    that spans three buys produces three rows. That keeps holding period and
    entry price honest instead of averaging them into a single fictional lot.
    """
    books: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    lots: list[dict[str, Any]] = []

    for fill in sorted(fills, key=lambda row: (row["date"], row.get("trade_id", ""))):
        key = (fill["strategy_id"], fill["stock_code"].strip())
        shares = float(fill["shares"])
        if shares <= 0:
            raise RealizedError(f"{key} has a fill with non-positive shares")

        if fill["side"] == "BUY":
            books[key].append(
                {
                    "date": fill["date"],
                    "shares": shares,
                    "unit_cost": _unit(float(fill["cash_out"]), shares),
                    "price": float(fill["fill_price"]),
                    "trade_id": fill.get("trade_id", ""),
                }
            )
            continue

        unit_proceeds = _unit(float(fill["cash_in"]), shares)
        remaining = shares
        while remaining > 1e-9:
            if not books[key]:
                raise RealizedError(
                    f"{key[0]} sells {shares:g} of {key[1]} on {fill['date']} "
                    "with no matching buy in the fill book"
                )
            lot = books[key][0]
            matched = min(remaining, lot["shares"])
            lots.append(
                {
                    "strategy_id": key[0],
                    "stock_code": key[1],
                    "stock_name": fill.get("stock_name", ""),
                    "shares": matched,
                    "buy_date": lot["date"],
                    "buy_price": lot["price"],
                    "sell_date": fill["date"],
                    "sell_price": float(fill["fill_price"]),
                    "cost_twd": matched * lot["unit_cost"],
                    "proceeds_twd": matched * unit_proceeds,
                    "realized_pnl_twd": matched * (unit_proceeds - lot["unit_cost"]),
                    "return_pct": unit_proceeds / lot["unit_cost"] - 1.0,
                    "holding_days": (fill["date"] - lot["date"]).days,
                    "buy_trade_id": lot["trade_id"],
                    "sell_trade_id": fill.get("trade_id", ""),
                }
            )
            lot["shares"] -= matched
            remaining -= matched
            if lot["shares"] <= 1e-9:
                books[key].popleft()

    return lots


def open_lots(fills: Sequence[dict[str, Any]]) -> dict[tuple[str, str], float]:
    """Shares still open per ``(strategy_id, stock_code)`` after FIFO matching."""
    held: dict[tuple[str, str], float] = defaultdict(float)
    for fill in fills:
        key = (fill["strategy_id"], fill["stock_code"].strip())
        held[key] += float(fill["shares"]) * (1 if fill["side"] == "BUY" else -1)
    return {key: shares for key, shares in held.items() if shares > 1e-9}


def by_strategy(lots: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate closed lots into a per-strategy realized summary."""
    summary: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "realized_pnl_twd": 0.0,
            "cost_twd": 0.0,
            "proceeds_twd": 0.0,
            "closed_lots": 0,
            "winners": 0,
            "losers": 0,
        }
    )
    for lot in lots:
        row = summary[lot["strategy_id"]]
        row["realized_pnl_twd"] += lot["realized_pnl_twd"]
        row["cost_twd"] += lot["cost_twd"]
        row["proceeds_twd"] += lot["proceeds_twd"]
        row["closed_lots"] += 1
        if lot["realized_pnl_twd"] > 0:
            row["winners"] += 1
        elif lot["realized_pnl_twd"] < 0:
            row["losers"] += 1
    for row in summary.values():
        row["return_on_cost"] = (
            row["realized_pnl_twd"] / row["cost_twd"] if row["cost_twd"] else None
        )
    return dict(summary)


def as_of(lots: Sequence[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """Closed lots settled on or before ``day``."""
    return [lot for lot in lots if lot["sell_date"] <= day]
