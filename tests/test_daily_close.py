from __future__ import annotations

import csv
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "daily_close_updater", ROOT / "scripts" / "update_daily_close.py"
)
assert SPEC and SPEC.loader
updater = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = updater
SPEC.loader.exec_module(updater)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_quote_maps_use_official_schema_and_roc_dates() -> None:
    quotes = updater.quote_maps(
        [
            {
                "Date": "1150825",
                "Code": "0050",
                "Name": "元大台灣50",
                "ClosingPrice": "105.50",
            }
        ],
        [
            {
                "Date": "1150825",
                "SecuritiesCompanyCode": "006201",
                "CompanyName": "元大富櫃50",
                "Close": "42.50",
            }
        ],
    )

    assert quotes["0050"].market == "TWSE"
    assert quotes["006201"].market == "TPEX"
    assert quotes["0050"].data_date == date(2026, 8, 25)


def test_update_appends_gross_mark_and_benchmark_without_inferred_trades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = tmp_path / "inputs"
    output = tmp_path / "output"
    prices = tmp_path / "data" / "prices"
    holdings_fields = ["stock_code", "stock_name", "shares"]
    write_csv(
        inputs / "holdings_snapshot_2026-08-24.csv",
        holdings_fields,
        [
            {"stock_code": "2301", "stock_name": "光寶科", "shares": "10"},
            {"stock_code": "2397", "stock_name": "友通", "shares": "20"},
        ],
    )
    account_fields = [
        "asof_date",
        "portfolio_value_twd",
        "external_cash_flow_twd",
        "scope",
        "valuation_basis",
        "source",
        "note",
    ]
    write_csv(
        inputs / "account_nav.csv",
        account_fields,
        [
            {
                "asof_date": "2026-08-24",
                "portfolio_value_twd": "1000",
                "external_cash_flow_twd": "0",
                "scope": "HOLDINGS_ONLY",
                "valuation_basis": "GROSS_MARK_TO_MARKET",
                "source": "USER_PASTED_INTRADAY",
                "note": "baseline",
            }
        ],
    )
    write_csv(
        inputs / "benchmark_nav.csv",
        ["asof_date", "benchmark_id", "level", "data_asof", "note"],
        [],
    )
    monkeypatch.setattr(updater, "INPUTS", inputs)
    monkeypatch.setattr(updater, "ROOT", tmp_path)
    monkeypatch.setattr(updater, "OUTPUT", output)
    monkeypatch.setattr(updater, "PRICE_DIR", prices)
    monkeypatch.setattr(updater, "ACCOUNT_NAV", inputs / "account_nav.csv")
    monkeypatch.setattr(updater, "BENCHMARK_NAV", inputs / "benchmark_nav.csv")
    quotes = {
        "2301": updater.Quote("2301", "光寶科", "TWSE", date(2026, 8, 25), 300.0),
        "2397": updater.Quote("2397", "友通", "TWSE", date(2026, 8, 25), 70.0),
        "0050": updater.Quote("0050", "元大台灣50", "TWSE", date(2026, 8, 25), 105.5),
    }

    receipt = updater.update_ledgers(quotes)
    nav = updater.read_csv(inputs / "account_nav.csv")
    benchmark = updater.read_csv(inputs / "benchmark_nav.csv")

    assert receipt["status"] == "UPDATED"
    assert receipt["portfolio_gross_mark_twd"] == 4400.0
    assert receipt["safety"]["inferred_position_changes"] is False
    assert nav[-1]["asof_date"] == "2026-08-25"
    assert nav[-1]["portfolio_value_twd"] == "4400.00"
    assert benchmark[-1]["benchmark_id"] == "0050"
    assert (prices / "official_close_2026-08-25.csv").exists()


def test_update_fails_closed_on_mixed_market_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = tmp_path / "inputs"
    write_csv(
        inputs / "holdings_snapshot_2026-08-24.csv",
        ["stock_code", "stock_name", "shares"],
        [{"stock_code": "2301", "stock_name": "光寶科", "shares": "10"}],
    )
    monkeypatch.setattr(updater, "INPUTS", inputs)
    monkeypatch.setattr(updater, "ROOT", tmp_path)
    quotes = {
        "2301": updater.Quote("2301", "光寶科", "TWSE", date(2026, 8, 25), 300.0),
        "0050": updater.Quote("0050", "元大台灣50", "TWSE", date(2026, 8, 24), 105.5),
    }

    with pytest.raises(updater.UpdateError, match="MARKET_DATE_MISMATCH"):
        updater.update_ledgers(quotes)
