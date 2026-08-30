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
import realized  # noqa: E402
import strategy_gap  # noqa: E402


TRADING_DAYS = 252
MIN_RISK_RETURN_OBS = 20
ROLLING_WINDOW = analytics.ROLLING_WINDOW
ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
ACCOUNT_NAV_PATH = INPUTS / "account_nav.csv"
STRATEGY_NAV_PATH = INPUTS / "strategy_nav.csv"
BENCHMARK_NAV_PATH = INPUTS / "benchmark_nav.csv"
PRICE_HISTORY_PATH = INPUTS / "price_history.csv"
LEDGER_PATH = INPUTS / "positions_ledger.csv"
ACTUAL_FILLS_PATH = INPUTS / "actual_fills.csv"
SIGNAL_FILLS_PATH = INPUTS / "signal_fills.csv"
STRATEGY_CARD_PATH = INPUTS / "strategy_card_returns.csv"
STRATEGY_MARKS_PATH = INPUTS / "strategy_position_marks.csv"
LATEST_STRATEGY_SIGNALS_PATH = INPUTS / "latest_strategy_signals.csv"
SIGNAL_HISTORY_PATH = INPUTS / "signal_history.csv"
UNRECORDED_EVENTS_PATH = INPUTS / "unrecorded_events.csv"
BENCHMARK_LABELS = {"TAIEX": "加權指數", "0050": "0050 元大台灣50"}
STRATEGY_LABELS = {
    "TRUST": "投信",
    "YOY": "YOY",
    "MARGIN": "融資",
    "BREAKOUT": "突破",
}
STRATEGY_BUDGET_TWD = 500_000.0
EXPECTED_CARD_MEMBERS_LATEST = {
    "TRUST": 5,
    "YOY": 6,
    "MARGIN": 10,
    "BREAKOUT": 4,
}


class InputError(ValueError):
    """Input contract violation that must fail closed."""


def _latest_snapshot(prefix: str) -> Path:
    """Newest dated snapshot, so a new day only needs a new file.

    Pinning the filename meant every fresh paste from the owner required a
    code edit, and forgetting that edit would silently republish yesterday's
    holdings under today's date.
    """
    found = sorted(INPUTS.glob(f"{prefix}_????-??-??.csv"))
    if not found:
        raise InputError(f"no {prefix}_YYYY-MM-DD.csv in {INPUTS}")
    return found[-1]


HOLDINGS_PATH = _latest_snapshot("holdings_snapshot")
SUMMARY_PATH = _latest_snapshot("snapshot_summary")


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


def next_weekday(value: date) -> date:
    """Next Mon-Fri day after `value`. Taiwan exchange holidays are not
    modelled here; the source card remains the authority for the real day."""
    nxt = value + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


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


def load_unrecorded_exits(
    path: Path = UNRECORDED_EVENTS_PATH,
) -> list[dict[str, Any]]:
    """Exits the snapshot proves happened but the fill book cannot price.

    Each becomes a PROVISIONAL sell marked at that session's official close.
    The close is not a claim about the actual fill: it is a published, neutral
    stand-in, and the range it sits inside is carried alongside so the reader
    can see how wrong it could be.
    """
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in read_csv(path):
        if raw.get("status", "").strip() != "WAITING_FILL_CONFIRMATION":
            continue
        if raw.get("event", "").strip() != "POSITION_LEFT_SNAPSHOT":
            continue
        day = parse_date(raw["detected_date"])
        shares = required_float(raw["shares"], "unrecorded.shares")
        close = required_float(raw["session_close"], "unrecorded.session_close")
        low = optional_float(raw.get("session_low"), "unrecorded.session_low")
        high = optional_float(raw.get("session_high"), "unrecorded.session_high")
        consideration = shares * close
        fee = math.floor(consideration * 0.001425)
        tax = math.floor(consideration * 0.003)
        rows.append(
            {
                "trade_id": f"PROVISIONAL-{raw['stock_code'].strip()}-{day:%Y%m%d}",
                "strategy_id": raw["strategy_id"].strip(),
                "stock_code": raw["stock_code"].strip(),
                "stock_name": raw.get("stock_name", "").strip(),
                "side": "SELL",
                "date": day,
                "fill_price": close,
                "shares": shares,
                "consideration": consideration,
                "fee": float(fee),
                "tax": float(tax),
                "cash_out": 0.0,
                "cash_in": consideration - fee - tax,
                "source": "PROVISIONAL_MARK_AT_OFFICIAL_CLOSE",
                "provisional": True,
                "range_low": low,
                "range_high": high,
                "last_known_cost_twd": optional_float(
                    raw.get("last_known_cost_twd"), "unrecorded.last_known_cost_twd"
                ),
                "note": raw.get("note", "").strip(),
            }
        )
    return rows


def provisional_banner(exits: list[dict[str, Any]]) -> str:
    if not exits:
        return ""
    rows: list[str] = []
    for row in exits:
        low, high = row["range_low"], row["range_high"]
        if low is not None and high is not None:
            span = (high - low) * row["shares"]
            band = (
                f'{low:g} – {high:g}，200 股區間可能相差 NT$ {span:,.0f}'
                if row["shares"] == 200
                else f'{low:g} – {high:g}，{row["shares"]:,.0f} 股區間可能相差 NT$ {span:,.0f}'
            )
        else:
            band = "當日區間未知"
        rows.append(
            "<tr>"
            f'<td>{row["date"].isoformat()}</td>'
            f'<td>{row["stock_code"]} {html.escape(row["stock_name"])}</td>'
            f'<td class="num">{row["shares"]:,.0f}</td>'
            f'<td class="num">{fmt_ntd(row["last_known_cost_twd"] or 0)}</td>'
            f'<td class="num">{row["fill_price"]:g}</td>'
            f"<td>{html.escape(band)}</td>"
            "</tr>"
        )
    return (
        '<article class="panel full" style="border-color:var(--gold)">'
        '<h2 style="color:var(--gold)">⚠ 待確認成交 · 這些數字還不是最終值</h2>'
        '<div class="sub">庫存快照顯示部位已經離開，但成交回報還沒進到成交簿，'
        "所以<b>實際成交價目前不知道</b>。賣出的現金流無法從庫存表反推 —— 快照只記還在手上的東西。"
        "在收到回報之前，這些部位<b>暫時以當日官方收盤價計價</b>，並標成 PROVISIONAL。"
        "收盤價不是對成交價的猜測，它是一個公開、中性的替代值，右欄同時列出當日高低區間，"
        "讓你看得到它可能差多少。這些部位<b>不列入履約落差與成交落點統計</b>，"
        "因為那兩張表衡量的是執行品質，而這裡沒有執行紀錄可以衡量。"
        "回報一到，把它寫進 <code>actual_fills.csv</code> 並從 "
        "<code>unrecorded_events.csv</code> 移除，數字就會自動變成最終值。</div>"
        '<div class="table-wrap"><table><thead><tr><th>偵測日</th><th>股票</th>'
        '<th class="num">股數</th><th class="num">最後已知成本</th>'
        '<th class="num">暫計價格</th><th>當日區間與不確定範圍</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></article>"
    )


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


def load_latest_strategy_signals(
    path: Path = LATEST_STRATEGY_SIGNALS_PATH,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[date, str, str]] = set()
    for raw in read_csv(path):
        strategy_id = raw["strategy_id"].strip()
        if strategy_id not in STRATEGY_LABELS:
            raise InputError(f"unknown latest strategy id: {strategy_id!r}")
        asof = parse_date(raw["asof_date"])
        code = raw["stock_code"].strip()
        key = (asof, strategy_id, code)
        if not code or key in seen:
            raise InputError(f"duplicate or empty latest strategy signal: {key}")
        seen.add(key)
        signal = raw["signal"].strip()
        if signal not in {"進", "抱", "出"}:
            raise InputError(f"invalid signal for {key}: {signal!r}")
        direction = raw["direction"].strip()
        if direction not in {"+", "-"}:
            raise InputError(f"invalid direction for {key}: {direction!r}")
        magnitude = required_float(raw["magnitude_pct"], f"{key}.magnitude_pct")
        signed = required_float(raw["signed_return_pct"], f"{key}.signed_return_pct")
        expected = magnitude if direction == "+" else -magnitude
        if not math.isclose(signed, expected, abs_tol=0.001):
            raise InputError(f"signed return mismatch for {key}: {signed} vs {expected}")
        effective_raw = raw.get("effective_date", "").strip()
        rows.append(
            {
                **raw,
                "asof_date": asof,
                "effective_date": parse_date(effective_raw, "effective_date") if effective_raw else None,
                "entry_price": optional_float(raw.get("entry_price"), f"{key}.entry_price"),
                "close": required_float(raw["close"], f"{key}.close"),
                "magnitude_pct": magnitude,
                "signed_return_pct": signed,
                "strategy_id": strategy_id,
                "stock_code": code,
                "signal": signal,
            }
        )
    if not rows or len({row["asof_date"] for row in rows}) != 1:
        raise InputError("latest strategy signals must contain exactly one source date")
    expected_effective = next_weekday(rows[0]["asof_date"])
    for row in rows:
        if row["signal"] in {"進", "出"} and row["effective_date"] != expected_effective:
            raise InputError(
                f"planned action {row['strategy_id']} {row['stock_code']} "
                f"must carry effective_date={expected_effective.isoformat()}, "
                f"got {row['effective_date']}"
            )
    return rows


def latest_signal_quality(
    rows: list[dict[str, Any]],
    card_curves: dict[str, list[tuple[date, float]]],
) -> dict[str, Any]:
    asof = rows[0]["asof_date"]
    result: dict[str, Any] = {"asof_date": asof.isoformat()}
    for strategy_id in STRATEGY_LABELS:
        members = [row for row in rows if row["strategy_id"] == strategy_id]
        held = [row for row in members if row["signal"] != "進"]
        if not held:
            raise InputError(f"no held members for latest {strategy_id}")
        visible_mean = sum(row["signed_return_pct"] for row in held) / len(held)
        header_return = dict(card_curves[strategy_id])[asof] - 100.0
        gap = visible_mean - header_return
        result[strategy_id] = {
            "header_return_pct": header_return,
            "visible_member_mean_pct": visible_mean,
            "gap_pp": gap,
            "held_or_exit_count": len(held),
            "new_entry_count": sum(row["signal"] == "進" for row in members),
            "exit_count": sum(row["signal"] == "出" for row in members),
            "status": "PASS" if abs(gap) <= 0.12 else "SOURCE_CHECKSUM_MISMATCH",
        }
    return result


def latest_signal_cards(
    rows: list[dict[str, Any]],
    card_curves: dict[str, list[tuple[date, float]]],
) -> str:
    asof = rows[0]["asof_date"]
    cards: list[str] = []
    style_names = {"TRUST": "trust", "YOY": "yoy", "MARGIN": "margin", "BREAKOUT": "breakout"}
    for strategy_id, label in STRATEGY_LABELS.items():
        header_return = dict(card_curves[strategy_id])[asof] - 100.0
        body: list[str] = []
        for row in [item for item in rows if item["strategy_id"] == strategy_id]:
            value_class = css_value_class(row["signed_return_pct"])
            signal_class = "enter" if row["signal"] == "進" else "exit" if row["signal"] == "出" else "hold"
            body.append(
                "<tr>"
                f'<td><b>{html.escape(row["stock_code"])} {html.escape(row["stock_name"])}</b></td>'
                f'<td>{html.escape(row["industry"])}</td>'
                f'<td class="num">{html.escape(row["entry_display"])}</td>'
                f'<td class="num">{row["close"]:,.2f}</td>'
                f'<td class="num {value_class}">{row["signed_return_pct"]:+.1f}%</td>'
                f'<td><span class="signal {signal_class}">{row["signal"]}</span></td>'
                "</tr>"
            )
        cards.append(
            f'<section class="strategy-card {style_names[strategy_id]}">'
            f'<h3>{asof.strftime("%Y/%m/%d")} {html.escape(label)}策略：<span class="{css_value_class(header_return)}">{header_return:+.1f}%</span></h3>'
            '<div class="table-wrap"><table><thead><tr><th>股票</th><th>產業</th><th class="num">進場</th><th class="num">收盤</th><th class="num">%</th><th>訊</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div></section>'
        )
    return "".join(cards)


def planned_signal_table(rows: list[dict[str, Any]]) -> str:
    planned = [row for row in rows if row["signal"] in {"進", "出"}]
    asof_label = planned[0]["asof_date"].isoformat() if planned else "收盤"
    body = "".join(
        "<tr>"
        f'<td>{row["effective_date"].isoformat()}</td>'
        f'<td>{html.escape(STRATEGY_LABELS[row["strategy_id"]])}</td>'
        f'<td><b>{html.escape(row["stock_code"])} {html.escape(row["stock_name"])}</b></td>'
        f'<td><span class="signal {"enter" if row["signal"] == "進" else "exit"}">{row["signal"]}</span></td>'
        f'<td>{html.escape(row["entry_display"])}</td>'
        f'<td class="num">{row["close"]:,.2f}</td>'
        '<td><span class="badge warn">等待實際成交</span></td>'
        "</tr>"
        for row in planned
    )
    return (
        '<div class="table-wrap"><table><thead><tr><th>計畫日</th><th>策略</th><th>股票</th><th>動作</th><th>進場顯示</th>'
        f'<th class="num">{asof_label} 收盤</th><th>實際狀態</th></tr></thead>'
        f'<tbody>{body}</tbody></table></div>'
    )


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
) -> tuple[dict[str, list[tuple[date, float]]], list[tuple[date, float]], dict[str, Any]]:
    """Value every day -- including the latest -- on one consistent basis.

    Positions are always marked at the official close and carried at an
    estimated net liquidation value (fee + transaction tax). Owner snapshot
    gross values are never substituted into the curve, so consecutive daily
    returns stay comparable across snapshot days.
    """
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
    # The owner snapshot is a point in time. A fill executed after it cannot be
    # reconciled against it yet, so replay the book only up to the snapshot date
    # and check that; anything later is reported as awaiting its own snapshot
    # rather than silently loosening the guard.
    snapshot_dates = [row["asof_date"] for row in holdings if row.get("asof_date")]
    snapshot_asof = max(snapshot_dates) if snapshot_dates else asof
    settled_shares: dict[str, float] = defaultdict(float)
    for fill in fills:
        if fill["date"] > snapshot_asof:
            continue
        settled_shares[fill["stock_code"].strip()] += (
            fill["shares"] if fill["side"] == "BUY" else -fill["shares"]
        )
    settled = {code: shares for code, shares in settled_shares.items() if shares > 1e-9}
    if settled != expected:
        raise InputError(
            f"four-strategy active shares do not reconcile at {snapshot_asof}: "
            f"fills={settled} source={expected}"
        )
    diagnostics["reconciliation"] = "PASS_EXCLUDING_UNASSIGNED_2886"
    diagnostics["reconciled_asof"] = snapshot_asof.isoformat()
    diagnostics["post_snapshot_positions"] = {
        code: shares - expected.get(code, 0.0)
        for code, shares in sorted(reconstructed.items())
        if abs(shares - expected.get(code, 0.0)) > 1e-9
    }
    diagnostics["unassigned"] = {"2886": 1.0}
    diagnostics["last_fill_date"] = max(row["date"] for row in fills).isoformat()
    diagnostics["valuation_asof"] = asof.isoformat()
    diagnostics["valuation_basis"] = "OFFICIAL_CLOSE_ESTIMATED_LIQUIDATION_CARRY_FORWARD_POSITIONS"
    diagnostics["bundle_current_pnl_twd"] = (
        bundle[-1][1] / 100.0 * STRATEGY_BUDGET_TWD * len(STRATEGY_LABELS)
        - STRATEGY_BUDGET_TWD * len(STRATEGY_LABELS)
    )
    active_codes = set(reconstructed)
    # The snapshot cost is a point in time, so only fills up to that date may be
    # compared against it -- otherwise a post-snapshot purchase shows up as a
    # six-figure "cost basis gap" that is really just a newer trade.
    active_fill_cash_out = sum(
        row["cash_out"]
        for row in fills
        if row["side"] == "BUY"
        and row["stock_code"].strip() in active_codes
        and row["date"] <= snapshot_asof
    )
    source_active_cost = sum(
        row["cost_basis_twd"]
        for row in holdings
        if row["stock_code"] != "2886"
    )
    diagnostics["active_fill_cash_out_twd"] = active_fill_cash_out
    diagnostics["source_active_cost_ex_unassigned_twd"] = source_active_cost
    diagnostics["active_cost_basis_gap_twd"] = active_fill_cash_out - source_active_cost
    diagnostics["post_snapshot_fill_cash_out_twd"] = sum(
        row["cash_out"]
        for row in fills
        if row["side"] == "BUY" and row["date"] > snapshot_asof
    )
    return curves, bundle, diagnostics


def slice_and_normalize(
    curve: list[tuple[date, float]], start: date, end: date
) -> list[tuple[date, float]]:
    selected = [(day, value) for day, value in curve if start <= day <= end]
    return normalize_curve(selected)


def pnl_split(
    fills: list[dict[str, Any]],
    prices: dict[str, list[tuple[date, float]]],
    asof: date,
) -> dict[str, Any]:
    """Split every sleeve into settled cash and open marks.

    Realized comes only from the fill book -- cash that actually moved. Open
    cost is the FIFO-reduced book cost of what is still held, so realized and
    unrealized never double-count the same share.
    """
    lots = realized.as_of(realized.closed_lots(fills), asof)
    by_id = realized.by_strategy(lots)
    rows: dict[str, Any] = {}
    for strategy_id in STRATEGY_LABELS:
        held: dict[str, float] = defaultdict(float)
        book_cost: dict[str, float] = defaultdict(float)
        for fill in sorted(fills, key=lambda row: row["date"]):
            if fill["strategy_id"] != strategy_id or fill["date"] > asof:
                continue
            code = fill["stock_code"].strip()
            if fill["side"] == "BUY":
                held[code] += fill["shares"]
                book_cost[code] += fill["cash_out"]
            else:
                unit = book_cost[code] / held[code]
                book_cost[code] -= unit * fill["shares"]
                held[code] -= fill["shares"]
        market = 0.0
        open_cost = 0.0
        for code, shares in held.items():
            if shares <= 1e-9:
                continue
            history = [value for day, value in prices.get(code, []) if day <= asof]
            if not history:
                raise InputError(f"no close on or before {asof} for open {code}")
            market += estimated_liquidation_value(shares, history[-1])
            open_cost += book_cost[code]
        settled = by_id.get(strategy_id, {})
        realized_pnl = settled.get("realized_pnl_twd", 0.0)
        unrealized = market - open_cost
        rows[strategy_id] = {
            "realized_pnl_twd": realized_pnl,
            "unrealized_pnl_twd": unrealized,
            "total_pnl_twd": realized_pnl + unrealized,
            "open_cost_twd": open_cost,
            "market_twd": market,
            "closed_lots": settled.get("closed_lots", 0),
            "return_on_budget": (realized_pnl + unrealized) / STRATEGY_BUDGET_TWD,
        }
    strategies = [rows[key] for key in STRATEGY_LABELS]
    rows["_lots"] = lots
    rows["_totals"] = {
        "realized_pnl_twd": sum(row["realized_pnl_twd"] for row in strategies),
        "unrealized_pnl_twd": sum(row["unrealized_pnl_twd"] for row in strategies),
    }
    rows["_totals"]["total_pnl_twd"] = (
        rows["_totals"]["realized_pnl_twd"] + rows["_totals"]["unrealized_pnl_twd"]
    )
    return rows


def pnl_split_table(split: dict[str, Any]) -> str:
    rows: list[str] = []
    for strategy_id, label in STRATEGY_LABELS.items():
        row = split[strategy_id]
        realized_cell = (
            fmt_ntd(row["realized_pnl_twd"], sign=True) if row["closed_lots"] else "—"
        )
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(label)}</b><br><small>{strategy_id}</small></td>"
            f'<td class="num {css_value_class(row["realized_pnl_twd"])}">{realized_cell}</td>'
            f'<td class="num">{row["closed_lots"] or "—"}</td>'
            f'<td class="num {css_value_class(row["unrealized_pnl_twd"])}">'
            f'{fmt_ntd(row["unrealized_pnl_twd"], sign=True)}</td>'
            f'<td class="num">{fmt_ntd(row["open_cost_twd"])}</td>'
            f'<td class="num {css_value_class(row["total_pnl_twd"])}">'
            f'<b>{fmt_ntd(row["total_pnl_twd"], sign=True)}</b></td>'
            f'<td class="num {css_value_class(row["total_pnl_twd"])}">'
            f'{fmt_pct(row["return_on_budget"], sign=True)}</td>'
            "</tr>"
        )
    totals = split["_totals"]
    budget = STRATEGY_BUDGET_TWD * len(STRATEGY_LABELS)
    rows.append(
        '<tr style="border-top:2px solid var(--line)">'
        "<td><b>四策略合計</b><br><small>NT$200 萬</small></td>"
        f'<td class="num {css_value_class(totals["realized_pnl_twd"])}">'
        f'<b>{fmt_ntd(totals["realized_pnl_twd"], sign=True)}</b></td>'
        f'<td class="num">{len(split["_lots"])}</td>'
        f'<td class="num {css_value_class(totals["unrealized_pnl_twd"])}">'
        f'<b>{fmt_ntd(totals["unrealized_pnl_twd"], sign=True)}</b></td>'
        '<td class="num">—</td>'
        f'<td class="num {css_value_class(totals["total_pnl_twd"])}">'
        f'<b>{fmt_ntd(totals["total_pnl_twd"], sign=True)}</b></td>'
        f'<td class="num {css_value_class(totals["total_pnl_twd"])}">'
        f'<b>{fmt_pct(totals["total_pnl_twd"] / budget, sign=True)}</b></td>'
        "</tr>"
    )
    return "".join(rows)


def closed_lot_table(lots: list[dict[str, Any]]) -> str:
    if not lots:
        return (
            '<tr><td colspan="8" class="neutral">尚無平倉紀錄。'
            "任何賣出成交寫進 actual_fills.csv 後，這裡會逐筆列出實收現金與已實現損益。</td></tr>"
        )
    rows: list[str] = []
    for lot in sorted(lots, key=lambda row: row["sell_date"], reverse=True):
        cls = css_value_class(lot["realized_pnl_twd"])
        label = STRATEGY_LABELS.get(lot["strategy_id"], lot["strategy_id"])
        tag = (
            '<br><small style="color:var(--gold)">待確認 · 以收盤價暫計</small>'
            if lot.get("provisional")
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(label)}</td>"
            f'<td>{lot["stock_code"]} {html.escape(lot["stock_name"])}{tag}</td>'
            f'<td class="num">{lot["shares"]:,.0f}</td>'
            f'<td class="num">{lot["buy_date"].isoformat()}'
            f'<br><small>@{lot["buy_price"]:g}</small></td>'
            f'<td class="num">{lot["sell_date"].isoformat()}'
            f'<br><small>@{lot["sell_price"]:g}</small></td>'
            f'<td class="num">{lot["holding_days"]} 天</td>'
            f'<td class="num">{fmt_ntd(lot["cost_twd"])} → {fmt_ntd(lot["proceeds_twd"])}</td>'
            f'<td class="num {cls}"><b>{fmt_ntd(lot["realized_pnl_twd"], sign=True)}</b>'
            f'<br><small>{fmt_pct(lot["return_pct"], sign=True)}</small></td>'
            "</tr>"
        )
    return "".join(rows)


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
        expected_members = EXPECTED_CARD_MEMBERS_LATEST[strategy_id]
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


def line_chart(series: dict[str, list[tuple[date, float]]], chart_id: str = "curve") -> str:
    """Rebased-to-100 curves with a hover readout.

    Every line starts at 100 on the first shared session, so what the eye
    compares is percentage move from that day, not absolute money -- a NT$500k
    sleeve and an index level are otherwise unplottable together. The readout
    on hover carries both the rebased level and the move from 100, because
    "102.4" and "+2.4%" are the same fact and the second one is the one people
    actually reason about.
    """
    usable = {name: normalize_curve(values) for name, values in series.items() if len(values) >= 2}
    if not usable:
        return (
            '<div class="empty-chart"><div class="empty-icon">↗</div>'
            '<b>WAITING_HISTORY</b><p>至少要有兩個交易日的資料才畫得出累積曲線。</p></div>'
        )
    all_points = [(day, value) for values in usable.values() for day, value in values]
    dates = sorted({point[0] for point in all_points})
    min_date, max_date = dates[0], dates[-1]
    date_span = max((max_date - min_date).days, 1)
    values = [point[1] for point in all_points]
    low, high = min(values), max(values)
    padding = max((high - low) * 0.14, 0.6)
    low -= padding
    high += padding

    width, height = 940, 330
    left, top, right, bottom = 56, 20, 108, 46
    plot_w, plot_h = width - left - right, height - top - bottom

    def x_of(day: date) -> float:
        return left + ((day - min_date).days / date_span) * plot_w

    def y_of(value: float) -> float:
        return top + (high - value) / (high - low) * plot_h

    # Seven hues far apart on the wheel; the combined line and the benchmarks
    # used to collide on green, which made the one number that matters the
    # hardest to find.
    palette = {
        "四策略實際合計": "#ffffff",
        "實際·投信": "#f5bd58",
        "實際·YOY": "#4d9dff",
        "實際·融資": "#c77dff",
        "實際·突破": "#ff6b6b",
        "Benchmark·加權指數": "#2fbf9b",
        "Benchmark·0050 元大台灣50": "#8d99ae",
    }
    fallback = ["#57d3a2", "#f5bd58", "#72a7ff", "#d689ff", "#ff7f7f", "#2fbf9b", "#8d99ae"]

    grid: list[str] = []
    for index in range(5):
        y = top + plot_h * index / 4
        value = high - (high - low) * index / 4
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="grid-line"/>'
            f'<text x="{left-8}" y="{y+4:.1f}" class="axis-text" text-anchor="end">{value:.1f}</text>'
        )
    # the 100 baseline: every line starts here, so it is the only level that
    # separates "made money" from "lost money" at a glance
    if low < 100.0 < high:
        y100 = y_of(100.0)
        grid.append(
            f'<line x1="{left}" y1="{y100:.1f}" x2="{width-right}" y2="{y100:.1f}" class="base-line"/>'
            f'<text x="{width-right+6}" y="{y100+4:.1f}" class="axis-text base-tag">100</text>'
        )

    paths: list[str] = []
    legend: list[str] = []
    payload: list[str] = []
    for index, (name, points) in enumerate(usable.items()):
        color = palette.get(name, fallback[index % len(fallback)])
        emphasis = 3.4 if name == "四策略實際合計" else 2.1
        coords = " ".join(f"{x_of(day):.1f},{y_of(value):.1f}" for day, value in points)
        paths.append(
            f'<polyline points="{coords}" fill="none" stroke="{color}" '
            f'stroke-width="{emphasis}" stroke-linejoin="round" stroke-linecap="round" '
            f'data-line="{index}"/>'
        )
        last = points[-1][1]
        legend.append(
            f'<span class="lg" data-line="{index}"><i style="background:{color}"></i>'
            f'{html.escape(name)}<b class="lg-v">{last - 100:+.2f}%</b></span>'
        )
        series_points = ",".join(
            f'["{day.isoformat()}",{value:.4f}]' for day, value in points
        )
        payload.append(
            f'{{"name":{json.dumps(name, ensure_ascii=False)},"color":"{color}",'
            f'"points":[{series_points}]}}'
        )

    data = "[" + ",".join(payload) + "]"
    geom = (
        f'{{"left":{left},"top":{top},"plotW":{plot_w},"plotH":{plot_h},'
        f'"low":{low:.6f},"high":{high:.6f},"minDate":"{min_date.isoformat()}",'
        f'"span":{date_span},"width":{width},"height":{height}}}'
    )
    return (
        f'<div class="chart-box" id="{chart_id}">'
        '<div class="chart-legend">' + "".join(legend) + "</div>"
        f'<div class="chart-frame">'
        f'<svg class="line-chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="績效累積曲線，全部以起始日 100 為基準">'
        + "".join(grid)
        + "".join(paths)
        + f'<line class="crosshair" x1="0" y1="{top}" x2="0" y2="{top+plot_h}" style="opacity:0"/>'
        + f'<g class="hover-dots"></g>'
        + f'<text x="{left}" y="{height-14}" class="axis-text">{min_date.isoformat()}</text>'
        + f'<text x="{width-right}" y="{height-14}" class="axis-text" text-anchor="end">'
        f'{max_date.isoformat()}</text>'
        + f'<rect class="hit" x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" '
        'fill="transparent"/>'
        "</svg>"
        '<div class="tip" hidden></div>'
        "</div>"
        f'<script type="application/json" class="chart-data">'
        f'{{"series":{data},"geom":{geom}}}</script>'
        "</div>"
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


def slippage_table(rows: list[dict[str, Any]]) -> str:
    """Signal price vs the price actually paid -- the implementation gap."""
    if not rows:
        return (
            '<div class="mini-empty">WAITING_FIRST_FILL · '
            "計畫訊號成交後，這裡會逐筆記錄訊號價與實際成交價的落差</div>"
        )

    def price_cell(value: float | None, note: str = "") -> str:
        if value is None:
            return '<td class="num">—</td>'
        suffix = f"<br><small>{html.escape(note)}</small>" if note else ""
        return f'<td class="num">{value:,.2f}{suffix}</td>'

    def bp_cell(value: float | None) -> str:
        if value is None:
            return '<td class="num">—</td>'
        # Adverse slippage is a cost, so positive basis points render red.
        klass = "negative" if value > 0 else "positive"
        return f'<td class="num {klass}">{value:+,.0f} bp</td>'

    def pct_cell(value: float | None, klass: str | None = None) -> str:
        if value is None:
            return '<td class="num">—</td>'
        return f'<td class="num {klass or css_value_class(value)}">{value * 100:+.2f}%</td>'

    body: list[str] = []
    for row in rows:
        position = row["fill_range_position"]
        if position is None:
            range_cell = "<td><small>—</small></td>"
        else:
            limit_note = ""
            if row["hit_limit_up"] is not None:
                limit_note = (
                    f'<br><small>當日最高 {row["day_high"]:,.2f}；漲停價 '
                    f'{row["limit_up_price"]:,.2f}'
                    f'{"，曾觸及漲停" if row["hit_limit_up"] else "，未觸及漲停"}</small>'
                )
            range_cell = (
                '<td><div class="range-track">'
                f'<span class="range-fill" style="left:{position * 100:.1f}%"></span></div>'
                f'<small>{row["day_low"]:,.2f} — {row["day_high"]:,.2f}，'
                f'成交落在區間 {position * 100:.0f}%</small>{limit_note}</td>'
            )
        body.append(
            "<tr>"
            f'<td><b>{html.escape(row["stock_code"])}</b> {html.escape(row["stock_name"])}'
            f'<br><small>{html.escape(row["strategy_id"])} · {html.escape(row["action"])}'
            f' · T+{row["delay_days"]}</small></td>'
            + price_cell(row["signal_ref_price"], row["signal_basis"])
            + price_cell(row["day_open"])
            + price_cell(row["fill_price"], row["fill_time"])
            + bp_cell(row["slippage_vs_open_bp"])
            + bp_cell(row["slippage_vs_signal_bp"])
            + range_cell
            + pct_cell(row["mfe_pct"], "positive")
            + pct_cell(row["mae_pct"], "negative")
            + pct_cell(row["close_vs_fill_pct"])
            + "</tr>"
        )
    return (
        '<div class="table-wrap"><table><thead><tr><th>訊號</th><th class="num">訊號參考價</th>'
        '<th class="num">當日開盤</th><th class="num">實際成交</th><th class="num">vs 開盤</th>'
        '<th class="num">vs 訊號價</th><th>成交價在當日區間位置</th><th class="num">最大有利</th>'
        '<th class="num">最大不利</th><th class="num">收盤 vs 成交</th></tr></thead><tbody>'
        + "".join(body)
        + "</tbody></table></div>"
    )


def update_timeline(
    fills: list[dict[str, Any]],
    prices: dict[str, list[tuple[date, float]]],
    days_shown: int = 18,
) -> tuple[str, str]:
    """One row per feed, one column per session. Returns (grid, summary line).

    The owner updates by hand every day, so the question that actually matters
    is not "what is today's number" but "which day is each number from". A gap
    in a row here is a day the dashboard is quietly carrying forward.
    """
    feeds: list[tuple[str, str, set[date]]] = []

    signal_days: set[date] = set()
    if SIGNAL_HISTORY_PATH.exists():
        signal_days = {parse_date(row["asof_date"]) for row in read_csv(SIGNAL_HISTORY_PATH)}
    signal_days |= {row["asof_date"] for row in load_latest_strategy_signals()}
    feeds.append(("策略卡", "owner 每日四張卡", signal_days))

    snapshot_days = set()
    for path in sorted(INPUTS.glob("holdings_snapshot_????-??-??.csv")):
        snapshot_days.add(parse_date(path.stem.rsplit("_", 1)[1]))
    feeds.append(("庫存快照", "owner 貼入的券商庫存", snapshot_days))

    feeds.append(("實際成交", "actual_fills.csv", {row["date"] for row in fills}))

    price_days = {day for points in prices.values() for day, _ in points}
    feeds.append(("官方收盤", "TWSE／TPEx", price_days))

    every_day = sorted(set().union(*(days for _, _, days in feeds)))
    window = every_day[-days_shown:]
    if not window:
        return '<p class="neutral">尚無任何更新紀錄。</p>', "—"

    head = "".join(
        f'<th class="tl-d"><span>{day.strftime("%m/%d")}</span></th>' for day in window
    )
    body: list[str] = []
    for label, note, days in feeds:
        latest = max(days) if days else None
        cells = "".join(
            f'<td class="tl-c{" on" if day in days else ""}"></td>' for day in window
        )
        stale = latest is not None and latest < window[-1]
        body.append(
            "<tr>"
            f'<td class="tl-n"><b>{html.escape(label)}</b><br><small>{html.escape(note)}</small></td>'
            f"{cells}"
            f'<td class="tl-l{" warn" if stale else ""}">'
            f'{latest.isoformat() if latest else "—"}</td>'
            "</tr>"
        )

    grid = (
        '<div class="table-wrap"><table class="timeline"><thead><tr>'
        '<th class="tl-n">資料來源</th>' + head + '<th class="tl-l">最新</th>'
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
    )
    freshest = {label: (max(days) if days else None) for label, _, days in feeds}
    newest = max(day for day in freshest.values() if day)
    behind = [label for label, day in freshest.items() if day and day < newest]
    summary = (
        f"最新一次更新 {newest.isoformat()}"
        + (f"；落後的來源：{'、'.join(behind)}" if behind else "；四個來源同日到齊")
    )
    return grid, summary


def cost_basis_gap_rows(
    fills: list[dict[str, Any]], holdings: list[dict[str, Any]]
) -> tuple[str, int]:
    """Per-stock disagreement between settled cash and the broker's cost column.

    Book cost is FIFO-reduced so a partial exit does not leave a phantom
    balance. A non-zero row is not an error to fix: it is an event the fill
    book has not been told about -- a dividend, a basis reset, a fee the
    broker booked differently -- and it should stay visible until someone
    explains it.
    """
    positions: dict[str, float] = defaultdict(float)
    book: dict[str, float] = defaultdict(float)
    for fill in sorted(fills, key=lambda row: row["date"]):
        code = fill["stock_code"].strip()
        if fill["side"] == "BUY":
            positions[code] += fill["shares"]
            book[code] += fill["cash_out"]
        else:
            unit = book[code] / positions[code]
            book[code] -= unit * fill["shares"]
            positions[code] -= fill["shares"]

    snapshot = {
        row["stock_code"].strip(): (row["cost_basis_twd"], row["shares"], row["stock_name"])
        for row in holdings
    }
    rows: list[str] = []
    total = 0.0
    for code in sorted(set(positions) | set(snapshot)):
        held = positions.get(code, 0.0)
        if held <= 1e-9 and code not in snapshot:
            continue
        fill_cost = book.get(code, 0.0)
        source_cost, shares, name = snapshot.get(code, (0.0, 0.0, ""))
        gap = fill_cost - source_cost
        if abs(gap) < 0.5:
            continue
        total += gap
        per_share = gap / shares if shares else None
        rows.append(
            "<tr>"
            f"<td>{code} {html.escape(name)}</td>"
            f'<td class="num">{fmt_ntd(fill_cost)}</td>'
            f'<td class="num">{fmt_ntd(source_cost)}</td>'
            f'<td class="num {css_value_class(gap)}"><b>{fmt_ntd(gap, sign=True)}</b></td>'
            f'<td class="num">{shares:,.0f}</td>'
            f'<td class="num">{f"{per_share:+,.4f}" if per_share is not None else "—"}</td>'
            "</tr>"
        )
    if not rows:
        return (
            '<tr><td colspan="6" class="neutral">成交簿與券商成本欄完全一致。</td></tr>',
            0,
        )
    rows.append(
        '<tr style="border-top:2px solid var(--line)"><td><b>合計</b></td>'
        '<td class="num">—</td><td class="num">—</td>'
        f'<td class="num {css_value_class(total)}"><b>{fmt_ntd(total, sign=True)}</b></td>'
        '<td class="num">—</td><td class="num">—</td></tr>'
    )
    return "".join(rows), len(rows) - 1


def implementation_bridge(
    fills: list[dict[str, Any]],
    prices: dict[str, list[tuple[date, float]]],
    card_curves: dict[str, list[tuple[date, float]]],
    latest_signals: list[dict[str, Any]],
    asof: date,
) -> dict[str, Any]:
    """Decompose each sleeve's gap against its own card into three exact terms."""
    lots = realized.as_of(realized.closed_lots(fills), asof)
    realized_by = realized.by_strategy(lots)
    card_entry = {
        (row["strategy_id"], row["stock_code"].strip()): row.get("entry_price")
        for row in latest_signals
    }
    card_members: dict[str, set[str]] = defaultdict(set)
    for row in latest_signals:
        card_members[row["strategy_id"]].add(row["stock_code"].strip())

    out: dict[str, Any] = {}
    for strategy_id in STRATEGY_LABELS:
        held: dict[str, float] = defaultdict(float)
        book: dict[str, float] = defaultdict(float)
        for fill in sorted(fills, key=lambda row: row["date"]):
            if fill["strategy_id"] != strategy_id or fill["date"] > asof:
                continue
            code = fill["stock_code"].strip()
            if fill["side"] == "BUY":
                held[code] += fill["shares"]
                book[code] += fill["cash_out"]
            else:
                unit = book[code] / held[code]
                book[code] -= unit * fill["shares"]
                held[code] -= fill["shares"]

        names: list[dict[str, Any]] = []
        position_cost = 0.0
        position_value = 0.0
        for code, shares in sorted(held.items()):
            if shares <= 1e-9:
                continue
            history = [value for day, value in prices.get(code, []) if day <= asof]
            if not history:
                continue
            close = history[-1]
            value = estimated_liquidation_value(shares, close)
            cost = book[code]
            position_cost += cost
            position_value += value
            entry = card_entry.get((strategy_id, code))
            paid = cost / shares
            names.append(
                {
                    "stock_code": code,
                    "shares": shares,
                    "paid": paid,
                    "card_entry": entry,
                    "entry_gap": (paid / entry - 1.0) if entry else None,
                    "close": close,
                    "cost_twd": cost,
                    "value_twd": value,
                    "return_pct": value / cost - 1.0 if cost else None,
                    "on_card": code in card_members.get(strategy_id, set()),
                }
            )

        budget = STRATEGY_BUDGET_TWD
        weight = position_cost / budget if budget else 0.0
        r_positions = (position_value / position_cost - 1.0) if position_cost else 0.0
        realized_pnl = realized_by.get(strategy_id, {}).get("realized_pnl_twd", 0.0)
        card_curve = card_curves.get(strategy_id) or []
        r_card = (card_curve[-1][1] / 100.0 - 1.0) if card_curve else None

        sleeve_return = weight * r_positions + realized_pnl / budget
        if r_card is None:
            terms = None
        else:
            terms = {
                "selection_entry": weight * (r_positions - r_card),
                "cash_drag": (weight - 1.0) * r_card,
                "realized": realized_pnl / budget,
            }

        held_on_card = sum(1 for row in names if row["on_card"])
        out[strategy_id] = {
            "weight": weight,
            "cash_twd": budget - position_cost + realized_pnl,
            "position_cost_twd": position_cost,
            "position_value_twd": position_value,
            "r_positions": r_positions,
            "r_card": r_card,
            "sleeve_return": sleeve_return,
            "gap": (sleeve_return - r_card) if r_card is not None else None,
            "terms": terms,
            "names": names,
            "card_members": len(card_members.get(strategy_id, set())),
            "held_on_card": held_on_card,
            "realized_pnl_twd": realized_pnl,
        }
    return out


def bridge_table(bridge: dict[str, Any]) -> str:
    rows: list[str] = []
    for strategy_id, label in STRATEGY_LABELS.items():
        row = bridge[strategy_id]
        terms = row["terms"]
        if terms is None:
            rows.append(
                f"<tr><td><b>{html.escape(label)}</b></td>"
                '<td colspan="8" class="neutral">理論卡尚無曲線，無法比較</td></tr>'
            )
            continue
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(label)}</b><br>"
            f'<small>卡上 {row["card_members"]} 檔，持有其中 {row["held_on_card"]} 檔</small></td>'
            f'<td class="num">{fmt_pct(row["r_card"], sign=True)}</td>'
            f'<td class="num {css_value_class(row["sleeve_return"])}">'
            f'<b>{fmt_pct(row["sleeve_return"], sign=True)}</b></td>'
            f'<td class="num {css_value_class(row["gap"])}"><b>'
            f'{row["gap"] * 100:+.2f}pp</b></td>'
            f'<td class="num {css_value_class(terms["selection_entry"])}">'
            f'{terms["selection_entry"] * 100:+.2f}pp</td>'
            f'<td class="num {css_value_class(terms["cash_drag"])}">'
            f'{terms["cash_drag"] * 100:+.2f}pp</td>'
            f'<td class="num {css_value_class(terms["realized"])}">'
            f'{terms["realized"] * 100:+.2f}pp</td>'
            f'<td class="num">{fmt_pct(row["weight"])}</td>'
            f'<td class="num">{fmt_ntd(row["cash_twd"])}</td>'
            "</tr>"
        )
    return "".join(rows)


def entry_gap_table(bridge: dict[str, Any]) -> str:
    rows: list[str] = []
    for strategy_id, label in STRATEGY_LABELS.items():
        for name in bridge[strategy_id]["names"]:
            if name["card_entry"] is None:
                continue
            gap = name["entry_gap"]
            rows.append(
                "<tr>"
                f"<td>{html.escape(label)}</td>"
                f'<td>{name["stock_code"]}</td>'
                f'<td class="num">{name["shares"]:,.0f}</td>'
                f'<td class="num">{name["card_entry"]:,.2f}</td>'
                f'<td class="num">{name["paid"]:,.2f}</td>'
                f'<td class="num {css_value_class(-gap)}"><b>{gap * 100:+.2f}%</b></td>'
                f'<td class="num">{name["close"]:,.2f}</td>'
                f'<td class="num {css_value_class(name["return_pct"])}">'
                f'{fmt_pct(name["return_pct"], sign=True)}</td>'
                "</tr>"
            )
    if not rows:
        return '<tr><td colspan="8" class="neutral">卡片尚未提供可比對的進場價。</td></tr>'
    return "".join(rows)


def gap_lens_cards(report: dict[str, Any]) -> str:
    """Owner-facing summary of the descriptive strategy/actual gap report."""
    summary = report["summary"]
    vs_bench = summary["combined_vs_benchmarks"]
    execution_ok = summary["execution_status"] == "OK_MIN_30"
    return "".join(
        [
            metric_card(
                "四策略實際",
                fmt_pct(summary["combined_actual_return"], sign=True),
                "成交現金流＋每日可變現價值",
                css_value_class(summary["combined_actual_return"]),
            ),
            metric_card(
                "四卡平均顯示",
                fmt_pct(summary["combined_card_return"], sign=True),
                "來源卡表頭平均；不是可投資 NAV",
                css_value_class(summary["combined_card_return"]),
            ),
            metric_card(
                "實施落差",
                f'{summary["combined_gap"] * 100:+.2f}pp',
                "實際 − 四卡平均；描述性，不是 alpha",
                css_value_class(summary["combined_gap"]),
            ),
            metric_card(
                "策略成員覆蓋",
                f'{summary["covered_members"]}/{summary["card_members"]}',
                f'同策略持有覆蓋 {fmt_pct(summary["coverage_fraction"])}',
            ),
            metric_card(
                "資金投入",
                fmt_pct(summary["deployed_fraction"]),
                "四個 sleeve 在庫成本 ÷ NT$200 萬",
            ),
            metric_card(
                "相對加權指數",
                fmt_pct(vs_bench.get("TAIEX"), sign=True),
                "同起訖日實際合計 − TAIEX",
                css_value_class(vs_bench.get("TAIEX") or 0.0),
            ),
            metric_card(
                "相對 0050",
                fmt_pct(vs_bench.get("0050"), sign=True),
                "同起訖日實際合計 − 0050",
                css_value_class(vs_bench.get("0050") or 0.0),
            ),
            metric_card(
                "訊號成交樣本",
                f'{summary["execution_observations"]}/30',
                "可歸因到訊號的確定成交；未滿 30 不下執行結論",
                "positive" if execution_ok else "neutral",
            ),
        ]
    )


def _code_chips(codes: list[str], kind: str = "") -> str:
    if not codes:
        return '<span class="neutral">—</span>'
    return "".join(
        f'<span class="code-chip {kind}">{html.escape(code)}</span>' for code in codes
    )


def gap_driver_table(report: dict[str, Any]) -> str:
    rows: list[str] = []
    for strategy_id, label in STRATEGY_LABELS.items():
        row = report["strategies"][strategy_id]
        drivers = row["drivers"]
        dominant = row["dominant_driver"] or "—"
        execution = row["execution"]
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(label)}</b><br><small>{strategy_id}</small></td>"
            f'<td class="num {css_value_class(row["actual_return"])}">'
            f'{fmt_pct(row["actual_return"], sign=True)}</td>'
            f'<td class="num">{fmt_pct(row["card_return"], sign=True)}</td>'
            f'<td class="num {css_value_class(row["gap"])}"><b>'
            f'{row["gap"] * 100:+.2f}pp</b></td>'
            f'<td><b>{html.escape(dominant)}</b><br><small>'
            f'{drivers.get(dominant, 0.0) * 100:+.2f}pp</small></td>'
            f'<td class="num">{fmt_pct(row["deployed_fraction"])}</td>'
            f'<td class="num">{row["covered_members"]}/{row["card_members"]}</td>'
            f'<td class="num {css_value_class(row["active_vs_benchmarks"].get("TAIEX") or 0.0)}">'
            f'{fmt_pct(row["active_vs_benchmarks"].get("TAIEX"), sign=True)}</td>'
            f'<td class="num {css_value_class(row["active_vs_benchmarks"].get("0050") or 0.0)}">'
            f'{fmt_pct(row["active_vs_benchmarks"].get("0050"), sign=True)}</td>'
            f'<td class="num">{execution["observations"]}<br><small>'
            f'{fmt_num(execution["average_signal_slippage_bp"], 0)} bp</small></td>'
            "</tr>"
        )
    return "".join(rows)


def coverage_lens_table(report: dict[str, Any]) -> str:
    rows: list[str] = []
    for strategy_id, label in STRATEGY_LABELS.items():
        row = report["strategies"][strategy_id]
        missing_note = (
            fmt_pct(row["missing_card_average_return"], sign=True)
            if row["missing_card_average_return"] is not None
            else "—"
        )
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(label)}</b></td>"
            f'<td>{_code_chips(row["covered_codes"], "covered")}</td>'
            f'<td>{_code_chips(row["missing_codes"], "missing")}'
            f'<br><small>卡上顯示平均 {missing_note}</small></td>'
            f'<td>{_code_chips(row["stale_codes"], "stale")}</td>'
            f'<td>{_code_chips(row["planned_entry_codes"], "planned")}</td>'
            f'<td>{_code_chips(row["planned_exit_codes"], "planned")}</td>'
            f'<td class="num">{fmt_pct(row["weighted_entry_gap"], sign=True)}</td>'
            f'<td class="num">{fmt_ntd(row["cash_twd"])}</td>'
            "</tr>"
        )
    return "".join(rows)


def gap_history_chart(report: dict[str, Any]) -> str:
    """Small multi-line chart of actual minus card on source-card dates."""
    histories = {
        strategy_id: row["gap_history"]
        for strategy_id, row in report["strategies"].items()
        if row["gap_history"]
    }
    if not histories:
        return '<div class="mini-empty">沒有共同日期，無法畫 gap 歷史。</div>'
    dates = sorted({point["date"] for rows in histories.values() for point in rows})
    values = [point["gap_pp"] for rows in histories.values() for point in rows] + [0.0]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.12, 0.5)
    low, high = low - pad, high + pad
    width, height = 940.0, 300.0
    left, right, top, bottom = 64.0, 916.0, 22.0, 262.0

    def x_of(day: str) -> float:
        return left if len(dates) == 1 else left + dates.index(day) / (len(dates) - 1) * (right - left)

    def y_of(value: float) -> float:
        return bottom - (value - low) / (high - low) * (bottom - top)

    colors = {"TRUST": "#f5bd58", "YOY": "#4d9dff", "MARGIN": "#c77dff", "BREAKOUT": "#ff6b6b"}
    paths: list[str] = []
    legend: list[str] = []
    for strategy_id, rows in histories.items():
        points = " ".join(f'{x_of(row["date"]):.1f},{y_of(row["gap_pp"]):.1f}' for row in rows)
        color = colors[strategy_id]
        latest = rows[-1]["gap_pp"]
        paths.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" '
            'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>'
        )
        legend.append(
            f'<span><i style="background:{color}"></i>{html.escape(STRATEGY_LABELS[strategy_id])}'
            f' <b class="{css_value_class(latest)}">{latest:+.2f}pp</b></span>'
        )
    zero_y = y_of(0.0)
    return (
        '<div class="chart-legend">' + "".join(legend) + "</div>"
        '<svg class="line-chart" viewBox="0 0 940 300" role="img" '
        'aria-label="各策略實際報酬減策略卡顯示報酬的歷史">'
        f'<line x1="{left}" y1="{zero_y:.1f}" x2="{right}" y2="{zero_y:.1f}" class="base-line"/>'
        f'<text x="{left - 8}" y="{y_of(high - pad):.1f}" class="axis-text" text-anchor="end">{high - pad:+.1f}pp</text>'
        f'<text x="{left - 8}" y="{zero_y + 4:.1f}" class="axis-text" text-anchor="end">0</text>'
        f'<text x="{left - 8}" y="{y_of(low + pad) + 4:.1f}" class="axis-text" text-anchor="end">{low + pad:+.1f}pp</text>'
        + "".join(paths)
        + f'<text x="{left}" y="286" class="axis-text">{dates[0]}</text>'
        + f'<text x="{right}" y="286" class="axis-text" text-anchor="end">{dates[-1]}</text>'
        + "</svg>"
    )


def accrual_panel(
    analysis_curve: list[tuple[date, float]],
    fills: list[dict[str, Any]],
    slippage_rows: list[dict[str, Any]],
    lots: list[dict[str, Any]],
    signal_days: int,
) -> str:
    """Progress toward every sample threshold this page enforces."""
    settled = [row for row in fills if not row.get("provisional")]
    returns = max(len(analysis_curve) - 1, 0)
    gauges = [
        (
            "風險統計",
            returns,
            MIN_RISK_RETURN_OBS,
            "Sharpe、Sortino、Alpha、Beta、Information Ratio、Tracking Error",
            "日報酬",
        ),
        (
            "執行品質結論",
            len(settled),
            30,
            "成交落點的平均值才有統計意義，才能談要不要改下單方式",
            "確定成交",
        ),
        (
            "履約折扣估計",
            len(slippage_rows),
            20,
            "訊號價到成交價的平均折損，用來把策略卡報酬打折",
            "訊號→成交配對",
        ),
        (
            "勝率與期望值",
            len(lots),
            20,
            "平倉勝率、平均獲利／虧損、賺賠比",
            "平倉",
        ),
        (
            "策略卡穩定度",
            signal_days,
            20,
            "卡片表頭與成分股平均的落差是不是系統性的",
            "策略卡日",
        ),
    ]
    cards: list[str] = []
    for title, have, need, unlocks, unit in gauges:
        ratio = min(have / need, 1.0) if need else 1.0
        done = have >= need
        remaining = max(need - have, 0)
        state = "解鎖" if done else f"還差 {remaining} 筆"
        cards.append(
            f'<div class="gauge{" on" if done else ""}">'
            f'<div class="g-top"><b>{html.escape(title)}</b>'
            f'<span class="g-state">{state}</span></div>'
            f'<div class="g-bar"><span style="width:{ratio * 100:.1f}%"></span></div>'
            f'<div class="g-num">{have} / {need} {html.escape(unit)}</div>'
            f'<div class="g-note">{html.escape(unlocks)}</div>'
            "</div>"
        )
    return '<div class="gauges">' + "".join(cards) + "</div>"


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
    provisional_exits = load_unrecorded_exits()
    # Downstream code sees one fill book. The provisional flag rides along on
    # the rows so every surface that reports execution quality can drop them.
    fills = sorted(fills + provisional_exits, key=lambda row: row["date"])
    card_curves = load_strategy_cards()
    latest_signals = load_latest_strategy_signals()
    signal_quality = latest_signal_quality(latest_signals, card_curves)
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
    )
    pnl_breakdown = pnl_split(fills, prices, actual_asof)
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
    # Both benchmarks, not just the preferred one. 0050 is what the owner could
    # actually have bought instead; TAIEX is the market. They answer different
    # questions and the gap between them is itself informative.
    for benchmark_id in ("TAIEX", "0050"):
        if benchmark_id not in benchmark_curves:
            continue
        aligned = slice_and_normalize(
            benchmark_curves[benchmark_id],
            analysis_curve[0][0],
            analysis_curve[-1][0],
        )
        if aligned:
            series[f"Benchmark·{BENCHMARK_LABELS.get(benchmark_id, benchmark_id)}"] = aligned
    theory_series = {
        f"理論卡·{STRATEGY_LABELS[strategy_id]}": curve
        for strategy_id, curve in card_curves.items()
    }
    theory_asof = min(curve[-1][0] for curve in card_curves.values())
    actual_bundle_pnl = strategy_diagnostics["bundle_current_pnl_twd"]
    timeline_grid, timeline_summary = update_timeline(fills, prices)
    cost_gap_rows, cost_gap_count = cost_basis_gap_rows(fills, holdings)
    signal_day_count = len({
        row["asof_date"]
        for row in (read_csv(SIGNAL_HISTORY_PATH) if SIGNAL_HISTORY_PATH.exists() else [])
    })
    slippage_rows = analytics.build_slippage_ledger(
        SIGNAL_FILLS_PATH, analytics.load_ohlc(PRICE_HISTORY_PATH)
    )
    bridge = implementation_bridge(
        fills, prices, card_curves, latest_signals, actual_asof
    )
    gap_report = strategy_gap.analyze(
        strategy_labels=STRATEGY_LABELS,
        bridge=bridge,
        latest_signals=latest_signals,
        actual_curves=actual_strategy_curves,
        card_curves=card_curves,
        benchmark_curves=benchmark_curves,
        slippage_rows=slippage_rows,
        asof=actual_asof,
    )
    realized_total = pnl_breakdown["_totals"]["realized_pnl_twd"]
    provisional_lots = sum(1 for l in pnl_breakdown["_lots"] if l.get("provisional"))
    combined_pnl = snapshot["unrealized_pnl_twd"] + realized_total

    header_cards = "".join(
        [
            metric_card("庫存現值", f"NT$ {fmt_ntd(snapshot['current_value_twd'])}", "來源畫面『現值』小計"),
            metric_card("付出成本", f"NT$ {fmt_ntd(snapshot['cost_basis_twd'])}", "來源畫面『付出成本』小計"),
            metric_card(
                "在庫未實現損益",
                f"NT$ {fmt_ntd(snapshot['unrealized_pnl_twd'], sign=True)}",
                "現值 − 付出成本；只算還在手上的部位",
                css_value_class(snapshot["unrealized_pnl_twd"]),
            ),
            metric_card(
                "已實現損益",
                f"NT$ {fmt_ntd(realized_total, sign=True)}",
                (
                    f"{len(pnl_breakdown['_lots'])} 筆平倉的實收現金 − 實付成本；"
                    "賣掉的部位不在庫存表裡，但錢已經動了"
                    + (f"。其中 {provisional_lots} 筆以收盤價暫計，等回報"
                       if provisional_lots else "")
                ),
                css_value_class(realized_total),
            ),
            metric_card(
                "累積總損益",
                f"NT$ {fmt_ntd(combined_pnl, sign=True)}",
                (
                    "在庫未實現 + 已實現；這才是開戶至今的實際結果"
                    + ("（含暫計，尚未定案）" if provisional_lots else "")
                ),
                css_value_class(combined_pnl),
            ),
            metric_card(
                "累積未實現報酬",
                fmt_pct(snapshot["unrealized_return"], sign=True),
                "在庫未實現損益 ÷ 付出成本；分母不含已平倉部位",
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

    # --- Basket lane -------------------------------------------------------
    # The four-strategy actual lane is only {RISK_OBS} days old, so Sharpe /
    # Alpha / Beta legitimately read N/A there. The basket lane marks TODAY's
    # 15 holdings back through 242 official closes: that is a valid risk
    # fingerprint of the book the owner is holding right now, and it is the
    # only lane on this page with enough observations to be stable. It is NOT
    # realised performance and every label on it says so.
    basket_window = (mtm_curve[0][0], mtm_curve[-1][0]) if len(mtm_curve) >= 2 else None
    basket_metrics = performance_metrics(mtm_curve)
    basket_benchmarks: dict[str, list[tuple[date, float]]] = {}
    if basket_window:
        for code in ("TAIEX", "0050"):
            if code in benchmark_curves:
                basket_benchmarks[code] = slice_and_normalize(
                    benchmark_curves[code], basket_window[0], basket_window[1]
                )
    basket_relative = relative_metrics(mtm_curve, basket_benchmarks.get("TAIEX", []))
    basket_capture = analytics.capture_ratios(mtm_curve, basket_benchmarks.get("TAIEX", []))
    basket_dd = analytics.drawdown_detail(mtm_curve)
    basket_obs = max(len(mtm_curve) - 1, 0)
    basket_status = "OK" if basket_obs >= MIN_RISK_RETURN_OBS else "WAITING_MIN_20_RETURNS"

    basket_bench_metrics = performance_metrics(basket_benchmarks.get("TAIEX", []))
    template = """<!doctype html>
<html lang="zh-Hant-TW">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>績效累積圖 · {{SIGNAL_ASOF}}</title>
<style>
:root{--ink:#ecf4ef;--muted:#9eaaa5;--panel:#14231f;--panel2:#192c27;--line:#2a4039;--green:#57d3a2;--red:#ff7f7f;--gold:#f5bd58;--blue:#72a7ff;--bg:#0b1512}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#18362d 0,transparent 34%),var(--bg);color:var(--ink);font-family:"Segoe UI","Noto Sans TC",sans-serif;line-height:1.55}.wrap{max-width:1280px;margin:auto;padding:34px 24px 70px}.eyebrow{color:var(--green);font-weight:700;letter-spacing:.16em;font-size:12px;text-transform:uppercase}.hero{display:flex;justify-content:space-between;gap:24px;align-items:flex-end;margin:8px 0 24px}.hero h1{font-size:clamp(34px,5vw,64px);line-height:1.02;margin:0;letter-spacing:-.04em}.hero p{max-width:560px;color:var(--muted);margin:8px 0 0}.badges{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.badge{border:1px solid var(--line);border-radius:999px;padding:6px 10px;font-size:12px;color:var(--muted)}.badge.good{border-color:#2c7259;color:var(--green)}.badge.warn{border-color:#745c2c;color:var(--gold)}.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.metric-card,.panel{background:linear-gradient(145deg,rgba(25,44,39,.94),rgba(17,31,27,.94));border:1px solid var(--line);border-radius:18px;box-shadow:0 20px 50px rgba(0,0,0,.18)}.metric-card{padding:18px;min-height:132px}.metric-label{font-size:13px;color:var(--muted)}.metric-value{font-size:25px;font-weight:750;margin:10px 0 4px;white-space:nowrap}.metric-note{font-size:12px;color:var(--muted)}.positive{color:var(--green)!important}.negative{color:var(--red)!important}.neutral{color:var(--muted)!important}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}.panel{padding:22px;overflow:hidden}.panel.full{grid-column:1/-1}.panel h2{font-size:20px;margin:0 0 4px}.panel .sub{color:var(--muted);font-size:13px;margin-bottom:18px}.callout{border-left:3px solid var(--gold);background:#2a2618;border-radius:8px;padding:12px 14px;color:#eadfbe;margin:16px 0}.bar-row{display:grid;grid-template-columns:150px 1fr 92px;gap:10px;align-items:center;margin:9px 0;font-size:12px}.bar-label{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.bar-track{height:12px;background:#0d1915;border-radius:999px;position:relative;overflow:hidden}.bar-axis{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#607169}.bar-fill{position:absolute;top:2px;bottom:2px;border-radius:999px}.bar-fill.positive{background:var(--green)}.bar-fill.negative{background:var(--red)}.bar-fill.neutral{background:#607169}.bar-value{text-align:right;font-variant-numeric:tabular-nums}.allocation-row{display:grid;grid-template-columns:150px 1fr 54px;gap:10px;align-items:center;font-size:12px;margin:8px 0}.allocation-track{height:8px;background:#0d1915;border-radius:99px;overflow:hidden}.allocation-track span{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--green));border-radius:99px}.allocation-row strong{text-align:right}.status-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.status-metric{background:#0f1d19;border:1px solid var(--line);border-radius:12px;padding:13px}.status-metric>div{font-size:12px}.status-metric strong{display:block;font-size:20px;margin:6px 0}.status-metric small{display:block;color:var(--muted);font-size:10px}.status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px}.status-dot.ok{background:var(--green);box-shadow:0 0 10px var(--green)}.status-dot.waiting{background:var(--gold);box-shadow:0 0 10px var(--gold)}.period-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:10px}.period-card{background:#0f1d19;border:1px solid var(--line);border-radius:14px;padding:15px}.period-card span,.period-card small{display:block;color:var(--muted);font-size:11px}.period-card b{display:block;font-size:21px;margin:7px 0}.period-bar-row{display:grid;grid-template-columns:82px 1fr 70px;gap:10px;align-items:center;margin:9px 0;font-size:12px}.period-bar-track{height:10px;background:#0d1915;border-radius:99px;overflow:hidden}.period-bar-track i{display:block;height:100%;border-radius:99px}.period-bar-track i.positive{background:var(--green)}.period-bar-track i.negative{background:var(--red)}.period-kind{font-size:12px;color:var(--muted);margin-bottom:10px}.range-track{height:8px;background:#0d1915;border-radius:99px;position:relative;margin:4px 0 5px;border:1px solid var(--line)}.range-fill{position:absolute;top:-3px;width:3px;height:12px;background:var(--gold);border-radius:2px;box-shadow:0 0 6px var(--gold)}.mini-empty{min-height:180px;border:1px dashed var(--line);border-radius:12px;display:flex;align-items:center;justify-content:center;color:var(--gold);text-align:center;padding:20px}.heat-wrap{overflow:auto}.heatmap{min-width:850px}.heatmap td{text-align:center;font-variant-numeric:tabular-nums;border:3px solid var(--panel);border-radius:7px}.heat-empty{background:#0f1d19;color:#5f6e68}.drawdown-head{display:flex;justify-content:space-between;margin-bottom:8px}.drawdown-chart{width:100%;height:auto;background:#0f1d19;border-radius:12px}.empty-chart{min-height:260px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:14px}.empty-chart b{color:var(--gold)}.empty-chart p{margin:4px;max-width:540px}.empty-icon{font-size:48px;color:var(--green)}.line-chart{width:100%;height:auto;background:#0f1d19;border-radius:12px}.grid-line{stroke:#2a4039;stroke-width:1}.axis-text{fill:#899791;font-size:11px}.chart-legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:10px;font-size:12px;color:var(--muted)}.chart-legend i{display:inline-block;width:18px;height:3px;margin-right:6px;vertical-align:middle}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;color:var(--muted);font-weight:600;border-bottom:1px solid var(--line);padding:10px 8px;white-space:nowrap}td{padding:10px 8px;border-bottom:1px solid rgba(42,64,57,.55)}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}small{color:var(--muted)}.quality{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.quality article{background:#0f1d19;border-radius:12px;padding:15px;border:1px solid var(--line)}.quality b{display:block;margin-bottom:5px}.quality p{font-size:12px;color:var(--muted);margin:0}.footer{margin-top:22px;color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:20px}.mono{font-family:Consolas,monospace}.section-gap{margin-top:16px}@media(max-width:1050px){.metrics{grid-template-columns:repeat(3,1fr)}.period-grid{grid-template-columns:repeat(4,1fr)}.status-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:760px){.wrap{padding:22px 14px 50px}.hero{display:block}.grid{grid-template-columns:1fr}.panel.full{grid-column:auto}.metrics{grid-template-columns:repeat(2,1fr)}.period-grid{grid-template-columns:repeat(2,1fr)}.status-grid,.quality{grid-template-columns:1fr}.bar-row{grid-template-columns:100px 1fr 78px}.allocation-row{grid-template-columns:100px 1fr 48px}.metric-value{font-size:20px}}@media print{body{background:#fff;color:#111}.metric-card,.panel{box-shadow:none;background:#fff;border-color:#ccc}.metric-note,.panel .sub,small,.footer{color:#555}.positive{color:#087f5b!important}.negative{color:#c92a2a!important}}

table.timeline{min-width:0}
table.timeline td,table.timeline th{padding:5px 3px;border-bottom:1px solid var(--line)}
.tl-n{min-width:120px;white-space:nowrap}
.tl-d{text-align:center;font-size:10px;padding:4px 2px !important;color:var(--muted)}
.tl-d span{writing-mode:vertical-rl;text-orientation:mixed}
.tl-c{width:16px;padding:5px 2px !important}
.tl-c::after{content:"";display:block;width:11px;height:11px;margin:0 auto;border-radius:3px;
  background:var(--line)}
.tl-c.on::after{background:var(--green)}
.tl-l{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;font-size:12px}
.tl-l.warn{color:var(--gold);font-weight:700}

.chart-box{position:relative}
.chart-frame{position:relative}
.chart-legend .lg{cursor:pointer;user-select:none;transition:opacity .12s}
.chart-legend .lg.off{opacity:.32}
.chart-legend .lg-v{margin-left:6px;font-variant-numeric:tabular-nums;font-weight:700}
.base-line{stroke:var(--muted);stroke-width:1.2;stroke-dasharray:5 4;opacity:.75}
.base-tag{fill:var(--muted);font-weight:700}
.crosshair{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;pointer-events:none}
.hover-dots circle{pointer-events:none}
polyline[data-line].off{opacity:.08}
.hit{cursor:crosshair}
.tip{position:absolute;pointer-events:none;z-index:5;min-width:186px;
  background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:9px 11px;font-size:12.5px;box-shadow:0 8px 26px rgba(0,0,0,.42)}
.tip[hidden]{display:none}
.tip .tip-d{font-weight:700;margin-bottom:6px;font-variant-numeric:tabular-nums;
  padding-bottom:5px;border-bottom:1px solid var(--line)}
.tip .tip-r{display:flex;align-items:center;gap:7px;line-height:1.75;white-space:nowrap}
.tip .tip-r i{width:9px;height:9px;border-radius:2px;flex:none}
.tip .tip-r .n{flex:1;overflow:hidden;text-overflow:ellipsis}
.tip .tip-r .v{font-variant-numeric:tabular-nums;font-weight:700}
.tip .tip-r .p{font-variant-numeric:tabular-nums;min-width:56px;text-align:right}
.tip .up{color:var(--green)} .tip .down{color:var(--red)}

.gauges{display:grid;grid-template-columns:repeat(auto-fit,minmax(228px,1fr));gap:12px}
.gauge{background:var(--raise);border:1px solid var(--line);border-radius:11px;padding:14px 15px}
.gauge.on{border-color:var(--green)}
.g-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font-size:14px}
.g-state{font-size:11.5px;color:var(--muted);white-space:nowrap}
.gauge.on .g-state{color:var(--green);font-weight:700}
.g-bar{height:6px;border-radius:3px;background:var(--line);margin:9px 0 7px;overflow:hidden}
.g-bar span{display:block;height:100%;background:var(--accent);border-radius:3px}
.gauge.on .g-bar span{background:var(--green)}
.g-num{font-size:12.5px;font-variant-numeric:tabular-nums;font-weight:700}
.g-note{font-size:11.5px;color:var(--muted);line-height:1.5;margin-top:5px}
.gap-lenses{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:16px}
.gap-lenses .metric-card{min-height:118px;background:#0f1d19}
.code-chip{display:inline-block;margin:2px 4px 2px 0;padding:2px 7px;border-radius:999px;
  border:1px solid var(--line);font-size:11px;font-family:Consolas,monospace}
.code-chip.covered{color:var(--green);border-color:#2c7259;background:#10291f}
.code-chip.missing{color:var(--red);border-color:#744141;background:#2b1717}
.code-chip.stale{color:var(--gold);border-color:#745c2c;background:#292313}
.code-chip.planned{color:var(--blue);border-color:#3e5d83;background:#142337}
@media(max-width:1050px){.gap-lenses{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.gap-lenses{grid-template-columns:1fr}}
</style>
<style>
.badges a{text-decoration:none}
.strategy-card-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.strategy-card{border:1px solid var(--line);border-radius:14px;padding:14px;background:#0f1d19}.strategy-card h3{margin:0 0 8px;font-size:17px}.strategy-card.trust{border-color:#835d4f}.strategy-card.yoy{border-color:#507948}.strategy-card.margin{border-color:#72675d}.strategy-card.breakout{border-color:#456d8d}.signal{display:inline-block;min-width:28px;text-align:center;border-radius:999px;padding:2px 7px;font-weight:700}.signal.hold{color:var(--blue);background:#132d45}.signal.enter{color:#ff9a9a;background:#401f24}.signal.exit{color:#7de6aa;background:#163b2a}@media(max-width:760px){.strategy-card-grid{grid-template-columns:1fr}}
@media(max-width:520px){.metrics{grid-template-columns:1fr}.metric-value{white-space:normal}.hero,.hero>div,.hero p{width:calc(100vw - 28px);max-width:calc(100vw - 28px);white-space:normal;overflow-wrap:anywhere;word-break:break-word}.badges{display:grid;grid-template-columns:1fr 1fr;width:calc(100vw - 28px)}.badge{text-align:center}.panel{padding:16px;min-width:0}.strategy-card{padding:10px;min-width:0}.table-wrap{max-width:100%;overflow-x:auto}}
</style>
</head>
<body><main class="wrap">
<div class="eyebrow">66 · PERFORMANCE ACCUMULATION</div>
<section class="hero"><div><h1>績效累積圖</h1><p>實際績效已改用 2026-08-10 起始、2026-08-11 起逐筆成交的四策略 equity curve。不再把今日持股倒推一年。理論卡與實際線分開標示截止日。</p><div class="badges"><span class="badge good">ACTUAL_FILLS_RECONCILED</span><span class="badge good">THEORY_ASOF_{{THEORY_ASOF_COMPACT}}</span><span class="badge warn">RISK_SAMPLE_{{RISK_OBS}}_RETURNS</span><span class="badge">NO_BROKER · NO_ORDER</span><a class="badge good" href="inputs/four_strategy_daily_signals.xlsx" download>下載 Excel 主檔</a></div></div><div><b>四策略估值日</b><br><span class="mono">{{ASOF}}</span><br><small>owner 庫存快照 {{SNAPSHOT_ASOF}}；理論卡 {{THEORY_ASOF}}</small></div></section>
<section class="metrics">{{HEADER_CARDS}}</section>
<section class="grid">
{{PROVISIONAL_BANNER}}
<article class="panel full"><h2>最新四策略卡 · {{SIGNAL_ASOF}} 收盤</h2><div class="sub">來源圖逐列保存。紅／綠方向已轉成帶正負號報酬；{{PLANNED_DATE}} 的「進／出」是計畫訊號，不是成交。</div><div class="strategy-card-grid">{{LATEST_SIGNAL_CARDS}}</div></article>
<article class="panel full"><h2>{{PLANNED_DATE}} 計畫進出 · 等待實際成交</h2><div class="sub">沒有成交時間、價格、股數與費稅前，不寫入 actual_fills.csv，也不改實際績效曲線。</div>{{PLANNED_SIGNALS}}</article>
<article class="panel full"><h2>訊號 → 成交 · 履約落差帳</h2><div class="sub">策略卡報的是訊號價，帳戶付的是成交價，中間的差就是「這個策略能不能被執行」的全部答案。正的 bp 代表對自己不利。累積夠多筆之後，才知道策略卡報酬要打幾折。</div>{{SLIPPAGE_TABLE}}</article>
<article class="panel full"><h2>四策略實際績效 · 累積曲線</h2><div class="sub">每個 sleeve 以 NT$50 萬現金起始，用實際成交、費稅、已實現損益與每日可變現價值重建；合計初始資金 NT$200 萬。</div>{{LINE_CHART}}</article>
<article class="panel full"><h2>四策略理論卡 · 來源顯示曲線</h2><div class="sub">這是 owner 策略卡的「當日持倉成分等權顯示報酬」，不是可投資 NAV，也不將每日百分比複利串接。資料只到 {{THEORY_ASOF}}。</div>{{THEORY_CHART}}</article>
<article class="panel full"><h2>成本口徑落差 · 逐檔拆解</h2><div class="sub">成交簿記的是實際付出的現金（價金＋手續費），券商『付出成本』欄記的是它自己的成本基礎。兩者不一致時，這裡列出是哪一檔、差多少、每股差多少。<b>差額不是要去抹平的誤差，是成交簿還不知道的事件</b> —— 配息、成本重算、券商用不同方式記費用。在有人解釋它之前，它應該一直看得見。四策略實績一律以逐筆成交現金流為準。</div><div class="table-wrap"><table><thead><tr><th>股票</th><th class="num">成交簿成本</th><th class="num">券商成本欄</th><th class="num">差額</th><th class="num">股數</th><th class="num">每股差</th></tr></thead><tbody>{{COST_GAP_ROWS}}</tbody></table></div></article>
<article class="panel full"><h2>資料累積 · 還差多少才說得出話</h2><div class="sub">每一個顯示 <code>N/A</code> 的統計，背後都有一個樣本門檻。在門檻之前它不是壞掉，是還不知道 —— 而「不知道」和「不好」是兩件事。這裡把每天堆疊的資料換算成進度：現在有幾筆、需要幾筆、到了會解鎖什麼。<b>暫計成交不計入</b>，因為那不是真的執行紀錄。</div>{{ACCRUAL}}</article>
<article class="panel full"><h2>每日更新時間軸</h2><div class="sub">四個來源，各自有自己的更新節奏。實心格代表那一天有這個來源的資料；空格代表沒有，而不是「和前一天一樣」。右欄的日期若比最後一欄舊，代表這個來源正在落後，畫面上與它有關的數字都還停在那一天。{{TIMELINE_SUMMARY}}。</div>{{UPDATE_TIMELINE}}</article>
<article class="panel full"><h2>已實現 vs 未實現 · 完整損益拆解</h2><div class="sub">畫面上其他地方的「損益」都是<b>未實現</b>，只算還在手上的部位。已平倉的成交不會出現在庫存表裡，但現金已經確定變動 —— 那筆錢的盈虧在這裡。sleeve 曲線一直都含這兩塊，這張表只是把它拆開讓你看得到。已實現＝實收現金 − 實付成本（含手續費與證交稅）；未實現＝目前可變現值 − 在庫帳面成本，兩者不重複計算。</div><div class="table-wrap"><table><thead><tr><th>策略</th><th class="num">已實現損益</th><th class="num">平倉筆數</th><th class="num">未實現損益</th><th class="num">在庫成本</th><th class="num">合計損益</th><th class="num">對 50 萬報酬</th></tr></thead><tbody>{{PNL_SPLIT_TABLE}}</tbody></table></div><div class="section-gap"></div><div class="period-kind">逐筆平倉明細 · FIFO 對沖，一次賣出跨多筆買進會拆成多列</div><div class="table-wrap"><table><thead><tr><th>策略</th><th>股票</th><th class="num">股數</th><th class="num">買進</th><th class="num">賣出</th><th class="num">持有</th><th class="num">成本 → 實收</th><th class="num">已實現損益</th></tr></thead><tbody>{{CLOSED_LOTS}}</tbody></table></div></article>
<article class="panel full"><h2>策略 vs 實際 · 八個角度的診斷</h2><div class="sub">策略卡是當日成員的等權顯示報酬；實際 sleeve 是成交現金流、真實權重、閒置現金、費稅與可變現估值。兩者不是同一種 NAV。這一區回答「差在哪裡」，但不把描述性 bridge 冒充因果歸因或 alpha。</div><div class="gap-lenses">{{GAP_LENS_CARDS}}</div><div class="period-kind">策略層診斷 · 同一起訖日</div><div class="table-wrap"><table><thead><tr><th>策略</th><th class="num">實際</th><th class="num">卡片</th><th class="num">Gap</th><th>最大描述項</th><th class="num">投入</th><th class="num">覆蓋</th><th class="num">vs TAIEX</th><th class="num">vs 0050</th><th class="num">訊號成交樣本</th></tr></thead><tbody>{{GAP_DRIVER_TABLE}}</tbody></table></div><div class="section-gap"></div><div class="period-kind">Gap 走勢 · 實際報酬 − 卡片顯示報酬（pp）</div>{{GAP_HISTORY_CHART}}<div class="section-gap"></div><div class="period-kind">成員與狀態 · 缺席不等於損失，未買標的不得虛構 counterfactual P&amp;L</div><div class="table-wrap"><table><thead><tr><th>策略</th><th>同策略已覆蓋</th><th>卡上未持有</th><th>仍持有但已離卡</th><th>計畫進</th><th>計畫出</th><th class="num">實付 vs 卡價</th><th class="num">現金</th></tr></thead><tbody>{{COVERAGE_LENS_TABLE}}</tbody></table></div></article>
<article class="panel full"><h2>實施落差橋 · 三項加總的描述性 bridge</h2><div class="sub">只說「差幾 pp」沒有用。這裡用一個<b>代數恆等式</b>把差距拆成三項：<br><code>差距 = 在庫組合與進場 ＋ 現金／未投入 ＋ 已實現</code><br>三項加總會精確回到「實際 − 卡片」，但分類不是因果實驗：第一項同時混合成員覆蓋、實際權重、進場時點、進場價與出場費稅；第二項假設用卡片表頭當作未投入資金的參考報酬；第三項來自平倉現金流。它適合找下一個要查的方向，不適合宣稱哪一項造成未來績效。</div><div class="table-wrap"><table><thead><tr><th>策略</th><th class="num">理論卡</th><th class="num">實際 sleeve</th><th class="num">差距</th><th class="num">在庫組合<br>與進場</th><th class="num">現金／<br>未投入</th><th class="num">已實現<br>貢獻</th><th class="num">投入<br>比重</th><th class="num">閒置現金</th></tr></thead><tbody>{{BRIDGE_TABLE}}</tbody></table></div><div class="section-gap"></div><div class="period-kind">進場價差 · 卡片假設你付的 vs 你實際付的</div><div class="sub" style="margin-bottom:12px">「實付均價」是成交簿的在庫帳面成本 ÷ 股數，含手續費，所以它一定略高於成交價本身。綠色代表實付低於卡片進場價，紅色代表高於；它只描述成交，不代表那個價位是最佳進場。</div><div class="table-wrap"><table><thead><tr><th>策略</th><th>股票</th><th class="num">股數</th><th class="num">卡片進場</th><th class="num">實付均價</th><th class="num">進場價差</th><th class="num">現價</th><th class="num">在庫報酬<br>（扣出場費稅）</th></tr></thead><tbody>{{ENTRY_GAP_TABLE}}</tbody></table></div></article>
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
<article class="panel full"><h2>資料品質與限制</h2><div class="sub">畫面能否拿來做決策，先看資料是否足夠。</div><div class="quality"><article><b class="positive">PASS · 庫存小計</b><p>15 檔股數、現值、成本與損益均對上 owner 快照。</p></article><article><b class="positive">PASS · 四策略成交歸屬</b><p>22 筆買賣重建後的活動股數與 8/24 庫存一致，但排除不在成交簿的 2886 1 股。</p></article><article><b class="positive">PASS · 重疊股拆分</b><p>1709：突破 3,644／融資 305 股；2301：YOY 261／投信 365 股；3702 賣出損益納入 YOY。</p></article><article><b class="negative">CHECK · 成本口徑差</b><p>成交簿在庫實付 NT$1,178,519；快照在庫成本（排除 2886）NT$1,177,866，差 NT$653。四策略損益以逐筆成交現金流為準。</p></article><article><b class="negative">CHECK · YOY 來源矛盾</b><p>8/24 表頭 +3.7%，六檔可見數字平均 +4.33%，差 +0.63pp；兩者原樣保留，等待來源端說明。</p></article><article><b class="negative">SHORT SAMPLE · 風險統計</b><p>目前僅 {{RISK_OBS}} 筆實際日報酬；MDD 可描述，Sharpe、Alpha、Beta 等尚不顯示數字。</p></article><article><b class="positive">CURRENT · 理論卡</b><p>四策略來源均更新到 {{THEORY_ASOF}}，與目前實際估值同日。</p></article><article><b class="negative">GAP · 8/21 策略卡</b><p>未收到 8/21 來源圖，因此保留空缺，不用前值或行情補造策略卡。</p></article><article><b class="positive">SAFE · 公開唯讀</b><p>HTML builder 無券商登入或下單；每日 updater 只讀 TWSE／TPEx 公開收盤行情。</p></article></div></article>
</section>
<footer class="footer"><span><a href="mainline2/" style="color:var(--green);text-decoration:none">主線二 未持有訊號追蹤 →</a> · <a href="claude/" style="color:var(--green);text-decoration:none">Claude 版精進盤點 →</a> · 口徑：252 trading days · rf=0 · CAGR 365.25 calendar days · Alpha=daily OLS intercept×252</span><span>生成時間：<span class="mono">{{GENERATED_AT}}</span></span></footer>
</main><script>
(function () {
  // The chart is a static SVG; this only adds a readout. If it fails to run,
  // the curves are still fully readable -- nothing here is load-bearing.
  document.querySelectorAll(".chart-box").forEach(function (box) {
    var raw = box.querySelector(".chart-data");
    var svg = box.querySelector("svg");
    var tip = box.querySelector(".tip");
    var frame = box.querySelector(".chart-frame");
    if (!raw || !svg || !tip) return;
    var cfg;
    try { cfg = JSON.parse(raw.textContent); } catch (err) { return; }
    var g = cfg.geom;
    var series = cfg.series;
    var hidden = {};
    var dots = svg.querySelector(".hover-dots");
    var cross = svg.querySelector(".crosshair");

    var days = [];
    series.forEach(function (line) {
      line.points.forEach(function (pt) {
        if (days.indexOf(pt[0]) === -1) days.push(pt[0]);
      });
    });
    days.sort();
    var base = new Date(g.minDate + "T00:00:00Z").getTime();
    var DAY = 86400000;
    function xOf(iso) {
      var d = (new Date(iso + "T00:00:00Z").getTime() - base) / DAY;
      return g.left + (d / g.span) * g.plotW;
    }
    function yOf(v) {
      return g.top + (g.high - v) / (g.high - g.low) * g.plotH;
    }

    function move(evt) {
      var rect = svg.getBoundingClientRect();
      var px = (evt.clientX - rect.left) / rect.width * g.width;
      var best = null, bestDist = Infinity;
      days.forEach(function (iso) {
        var d = Math.abs(xOf(iso) - px);
        if (d < bestDist) { bestDist = d; best = iso; }
      });
      if (!best) return;
      var bx = xOf(best);
      cross.setAttribute("x1", bx); cross.setAttribute("x2", bx);
      cross.style.opacity = "1";

      var rows = "", marks = "";
      series.forEach(function (line, i) {
        if (hidden[i]) return;
        var hit = null;
        for (var k = 0; k < line.points.length; k++) {
          if (line.points[k][0] === best) { hit = line.points[k][1]; break; }
        }
        if (hit === null) return;
        var move = hit - 100;
        var cls = move > 0 ? "up" : (move < 0 ? "down" : "");
        rows += '<div class="tip-r"><i style="background:' + line.color + '"></i>'
          + '<span class="n">' + line.name + '</span>'
          + '<span class="v">' + hit.toFixed(2) + '</span>'
          + '<span class="p ' + cls + '">' + (move >= 0 ? "+" : "") + move.toFixed(2) + '%</span></div>';
        marks += '<circle cx="' + bx.toFixed(1) + '" cy="' + yOf(hit).toFixed(1)
          + '" r="3.6" fill="' + line.color + '" stroke="var(--panel)" stroke-width="1.4"/>';
      });
      dots.innerHTML = marks;
      tip.innerHTML = '<div class="tip-d">' + best + '　<span style="font-weight:400;opacity:.7">'
        + '相對 ' + g.minDate + ' 進場</span></div>' + rows;
      tip.hidden = false;

      var fw = frame.clientWidth;
      var leftPx = bx / g.width * fw + 16;
      if (leftPx + tip.offsetWidth > fw) leftPx = bx / g.width * fw - tip.offsetWidth - 16;
      tip.style.left = Math.max(0, leftPx) + "px";
      tip.style.top = Math.max(0, (evt.clientY - frame.getBoundingClientRect().top) - tip.offsetHeight / 2) + "px";
    }

    function leave() {
      tip.hidden = true;
      cross.style.opacity = "0";
      dots.innerHTML = "";
    }

    svg.addEventListener("mousemove", move);
    svg.addEventListener("mouseleave", leave);
    svg.addEventListener("touchmove", function (e) {
      if (e.touches.length) { move(e.touches[0]); e.preventDefault(); }
    }, { passive: false });
    svg.addEventListener("touchend", leave);

    box.querySelectorAll(".chart-legend .lg").forEach(function (tag) {
      tag.addEventListener("click", function () {
        var i = tag.getAttribute("data-line");
        hidden[i] = !hidden[i];
        tag.classList.toggle("off", !!hidden[i]);
        var line = svg.querySelector('polyline[data-line="' + i + '"]');
        if (line) line.classList.toggle("off", !!hidden[i]);
        leave();
      });
    });
  });
})();
</script>
</body></html>"""

    top_winner = max(holdings, key=lambda row: row["unrealized_pnl_twd"])
    top_loser = min(holdings, key=lambda row: row["unrealized_pnl_twd"])
    top_day_winner = max(holdings, key=lambda row: row["estimated_daily_price_contribution_twd"])
    top_day_loser = min(holdings, key=lambda row: row["estimated_daily_price_contribution_twd"])
    planned_rows = [row for row in latest_signals if row["signal"] in {"進", "出"}]
    planned_effective_label = (
        planned_rows[0]["effective_date"].isoformat() if planned_rows else None
    )
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    replacements = {
        "{{ASOF}}": actual_asof.isoformat(),
        "{{SIGNAL_ASOF}}": latest_signals[0]["asof_date"].isoformat(),
        "{{PLANNED_DATE}}": planned_effective_label or "待定",
        "{{SNAPSHOT_ASOF}}": source_summary["asof_date"].isoformat(),
        "{{RISK_OBS}}": str(return_obs),
        "{{THEORY_ASOF}}": theory_asof.isoformat(),
        "{{THEORY_ASOF_COMPACT}}": theory_asof.isoformat(),
        "{{HEADER_CARDS}}": header_cards,
        "{{LINE_CHART}}": line_chart(series),
        "{{THEORY_CHART}}": line_chart(theory_series, "theory"),
        "{{LATEST_SIGNAL_CARDS}}": latest_signal_cards(latest_signals, card_curves),
        "{{PLANNED_SIGNALS}}": planned_signal_table(latest_signals),
        "{{STRATEGY_TABLE}}": strategy_comparison_table(
            actual_strategy_curves, card_curves, strategy_diagnostics
        ),
        "{{ANALYSIS_BASIS}}": analysis_basis,
        "{{PERIOD_CARDS}}": period_cards(analysis_curve),
        "{{PERIOD_BARS}}": historical_period_bars(analysis_curve),
        "{{DRAWDOWN}}": drawdown_visual(analysis_curve),
        "{{MONTHLY_HEATMAP}}": monthly_heatmap(analysis_curve),
        "{{SLIPPAGE_TABLE}}": slippage_table(slippage_rows),
        "{{PROVISIONAL_BANNER}}": provisional_banner(provisional_exits),
        "{{ACCRUAL}}": accrual_panel(
            analysis_curve, fills, slippage_rows, pnl_breakdown["_lots"], signal_day_count
        ),
        "{{GAP_LENS_CARDS}}": gap_lens_cards(gap_report),
        "{{GAP_DRIVER_TABLE}}": gap_driver_table(gap_report),
        "{{GAP_HISTORY_CHART}}": gap_history_chart(gap_report),
        "{{COVERAGE_LENS_TABLE}}": coverage_lens_table(gap_report),
        "{{BRIDGE_TABLE}}": bridge_table(bridge),
        "{{ENTRY_GAP_TABLE}}": entry_gap_table(bridge),
        "{{COST_GAP_ROWS}}": cost_gap_rows,
        "{{UPDATE_TIMELINE}}": timeline_grid,
        "{{TIMELINE_SUMMARY}}": timeline_summary,
        "{{PNL_SPLIT_TABLE}}": pnl_split_table(pnl_breakdown),
        "{{CLOSED_LOTS}}": closed_lot_table(pnl_breakdown["_lots"]),
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
            f"- 理論卡最新日：`{theory_asof.isoformat()}`（與實際估值同日）",
            (
                f"- {planned_effective_label} 計畫訊號："
                + "、".join(
                    f"`{row['stock_code']} {row['stock_name']} {row['signal']}`"
                    for row in planned_rows
                )
                + "（等待實際成交）"
                if planned_rows
                else "- 無新的計畫進出訊號"
            ),
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
                LATEST_STRATEGY_SIGNALS_PATH,
            )
        },
        "snapshot": snapshot,
        "history": {
            "account_nav_observations": len(account_rows),
            "account_return_observations": max(len(legacy_account_curve) - 1, 0),
            "analysis_return_observations": return_obs,
            "analysis_basis": analysis_basis,
            "mtm_diagnostics": mtm_diagnostics,
            "signal_fill_slippage": [
                {
                    key: value.isoformat() if isinstance(value, date) else value
                    for key, value in row.items()
                }
                for row in slippage_rows
            ],
            "basket_lane": {
                "basis": "SIMULATED_CONSTANT_HOLDINGS",
                "observations": basket_obs,
                "status": basket_status,
                "metrics": basket_metrics,
                "relative_to_TAIEX": basket_relative,
                "capture": basket_capture,
                "drawdown": basket_dd,
                "benchmark_TAIEX": basket_bench_metrics,
            },
            "strategy_series": sorted(actual_strategy_curves),
            "strategy_card_asof": theory_asof.isoformat(),
            "latest_strategy_signals": {
                "asof_date": latest_signals[0]["asof_date"].isoformat(),
                "planned_effective_date": planned_effective_label,
                "planned_actions": [
                    {
                        "strategy_id": row["strategy_id"],
                        "stock_code": row["stock_code"],
                        "signal": row["signal"],
                        "actual_fill_status": "WAITING_ACTUAL_FILL",
                    }
                    for row in latest_signals
                    if row["signal"] in {"進", "出"}
                ],
                "quality": signal_quality,
            },
            "strategy_diagnostics": strategy_diagnostics,
            "strategy_actual_gap_report": gap_report,
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
