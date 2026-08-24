"""Append one official TWSE/TPEx close to the private performance ledger.

Only public exchange endpoints are contacted. No broker SDK, account, login,
credential, or order path exists here. Holdings remain owner-supplied; this
script never infers trades, dividends, deposits, or withdrawals.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
PRICE_DIR = ROOT / "data" / "prices"
ACCOUNT_NAV = INPUTS / "account_nav.csv"
BENCHMARK_NAV = INPUTS / "benchmark_nav.csv"
TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
BENCHMARK_CODE = "0050"


class UpdateError(RuntimeError):
    """Fail-closed official data or ledger error."""


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    market: str
    data_date: date
    close: float


def roc_date(value: str) -> date:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 7:
        raise UpdateError(f"invalid ROC date: {value!r}")
    return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))


def finite_price(value: Any, label: str) -> float:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise UpdateError(f"invalid close for {label}: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise UpdateError(f"non-positive close for {label}: {value!r}")
    return number


def fetch_json(url: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Quant-Grill-Lab-Private-Performance-Dashboard/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed official URLs
        payload = json.loads(response.read().decode("utf-8-sig"))
    if not isinstance(payload, list) or not payload:
        raise UpdateError(f"official endpoint returned no rows: {url}")
    return payload


def quote_maps(
    twse_payload: list[dict[str, Any]], tpex_payload: list[dict[str, Any]]
) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for raw in twse_payload:
        code = str(raw.get("Code", "")).strip()
        if not code:
            continue
        try:
            quote = Quote(
                code=code,
                name=str(raw.get("Name", "")).strip(),
                market="TWSE",
                data_date=roc_date(str(raw.get("Date", ""))),
                close=finite_price(raw.get("ClosingPrice"), f"TWSE:{code}"),
            )
        except UpdateError:
            continue
        quotes[code] = quote
    for raw in tpex_payload:
        code = str(raw.get("SecuritiesCompanyCode", "")).strip()
        if not code or code in quotes:
            continue
        try:
            quote = Quote(
                code=code,
                name=str(raw.get("CompanyName", "")).strip(),
                market="TPEX",
                data_date=roc_date(str(raw.get("Date", ""))),
                close=finite_price(raw.get("Close"), f"TPEX:{code}"),
            )
        except UpdateError:
            continue
        quotes[code] = quote
    return quotes


def latest_holdings_path() -> Path:
    paths = sorted(INPUTS.glob("holdings_snapshot_*.csv"))
    if not paths:
        raise UpdateError("no owner-supplied holdings snapshot")
    return paths[-1]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle) if any(row.values())]


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def update_ledgers(quotes: dict[str, Quote]) -> dict[str, Any]:
    holdings_path = latest_holdings_path()
    holdings = read_csv(holdings_path)
    if not holdings:
        raise UpdateError("holdings snapshot is empty")
    codes = [row["stock_code"].strip() for row in holdings]
    missing = sorted(code for code in codes + [BENCHMARK_CODE] if code not in quotes)
    if missing:
        raise UpdateError(f"official quotes missing: {','.join(missing)}")
    market_dates = {quotes[code].data_date for code in codes + [BENCHMARK_CODE]}
    if len(market_dates) != 1:
        detail = ",".join(
            f"{code}:{quotes[code].data_date.isoformat()}" for code in codes + [BENCHMARK_CODE]
        )
        raise UpdateError(f"MARKET_DATE_MISMATCH:{detail}")
    data_date = market_dates.pop()
    gross_value = sum(float(row["shares"]) * quotes[row["stock_code"]].close for row in holdings)

    account_fields = [
        "asof_date",
        "portfolio_value_twd",
        "external_cash_flow_twd",
        "scope",
        "valuation_basis",
        "source",
        "note",
    ]
    account_rows = read_csv(ACCOUNT_NAV)
    official_row = {
        "asof_date": data_date.isoformat(),
        "portfolio_value_twd": f"{gross_value:.2f}",
        "external_cash_flow_twd": "0",
        "scope": "HOLDINGS_ONLY",
        "valuation_basis": "GROSS_MARK_TO_MARKET",
        "source": "TWSE_TPEX_OFFICIAL_CLOSE",
        "note": f"positions_from={holdings_path.name}; no inferred trades/cash/dividends",
    }
    existing_dates = {row["asof_date"] for row in account_rows}
    account_changed = False
    if data_date.isoformat() in existing_dates:
        replaced: list[dict[str, Any]] = []
        for row in account_rows:
            if row["asof_date"] == data_date.isoformat() and row.get("source") != "TWSE_TPEX_OFFICIAL_CLOSE":
                replaced.append(official_row)
                account_changed = True
            else:
                replaced.append(row)
        account_rows = replaced
    else:
        latest_date = max((date.fromisoformat(row["asof_date"]) for row in account_rows), default=None)
        if latest_date is None or data_date > latest_date:
            account_rows.append(official_row)
            account_changed = True
    account_rows.sort(key=lambda row: row["asof_date"])
    if account_changed:
        write_csv(ACCOUNT_NAV, account_fields, account_rows)

    benchmark_fields = ["asof_date", "benchmark_id", "level", "data_asof", "note"]
    benchmark_rows = read_csv(BENCHMARK_NAV)
    benchmark_key = (data_date.isoformat(), BENCHMARK_CODE)
    benchmark_changed = False
    if benchmark_key not in {
        (row["asof_date"], row["benchmark_id"]) for row in benchmark_rows
    }:
        benchmark_rows.append(
            {
                "asof_date": data_date.isoformat(),
                "benchmark_id": BENCHMARK_CODE,
                "level": f"{quotes[BENCHMARK_CODE].close:.4f}",
                "data_asof": data_date.isoformat(),
                "note": "TWSE official close; price index without reinvested distributions",
            }
        )
        benchmark_rows.sort(key=lambda row: (row["benchmark_id"], row["asof_date"]))
        write_csv(BENCHMARK_NAV, benchmark_fields, benchmark_rows)
        benchmark_changed = True

    price_rows = [
        {
            "asof_date": data_date.isoformat(),
            "market": quotes[code].market,
            "stock_code": code,
            "stock_name": quotes[code].name,
            "close": f"{quotes[code].close:.4f}",
            "source": TWSE_URL if quotes[code].market == "TWSE" else TPEX_URL,
        }
        for code in codes + [BENCHMARK_CODE]
    ]
    price_path = PRICE_DIR / f"official_close_{data_date.isoformat()}.csv"
    price_changed = not price_path.exists()
    if account_changed or benchmark_changed or price_changed:
        write_csv(
            price_path,
            ["asof_date", "market", "stock_code", "stock_name", "close", "source"],
            price_rows,
        )
    return {
        "status": "UPDATED" if account_changed or benchmark_changed or price_changed else "NO_NEW_CLOSE",
        "data_asof": data_date.isoformat(),
        "holdings_source": holdings_path.name,
        "positions": len(holdings),
        "portfolio_gross_mark_twd": round(gross_value, 2),
        "benchmark": BENCHMARK_CODE,
        "benchmark_close": quotes[BENCHMARK_CODE].close,
        "account_nav_changed": account_changed,
        "benchmark_changed": benchmark_changed,
        "price_receipt_changed": price_changed,
        "price_receipt": str(price_path.relative_to(ROOT)),
        "safety": {
            "official_public_market_data_only": True,
            "broker_access": False,
            "credential_access": False,
            "order_capability": False,
            "inferred_position_changes": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        help="Read twse.json and tpex.json fixtures instead of the network.",
    )
    args = parser.parse_args()
    if args.fixture_dir:
        twse = json.loads((args.fixture_dir / "twse.json").read_text(encoding="utf-8"))
        tpex = json.loads((args.fixture_dir / "tpex.json").read_text(encoding="utf-8"))
    else:
        twse = fetch_json(TWSE_URL)
        tpex = fetch_json(TPEX_URL)
    receipt = update_ledgers(quote_maps(twse, tpex))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "daily_update_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
