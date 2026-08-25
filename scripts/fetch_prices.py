"""Fetch Taiwan daily closing prices for the 66 performance lane.

This is the ONLY script in this lane that touches the network, and it talks to
public market-data endpoints only:

  * TWSE  https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY
  * TWSE  https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST
  * TPEx  https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock

No credential, no broker SDK, no login, no order capability. The dashboard
builder stays fully offline and only reads the CSV files written here.

Usage
-----
    python fetch_prices.py                      # incremental, last 45 days
    python fetch_prices.py --start 2025-08-25   # explicit backfill window
    python fetch_prices.py --full               # backfill the ledger window
"""

from __future__ import annotations

import argparse
import csv
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
LEDGER_PATH = INPUTS / "positions_ledger.csv"
PRICE_PATH = INPUTS / "price_history.csv"
BENCHMARK_PATH = INPUTS / "benchmark_nav.csv"

TWSE_STOCK_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TWSE_TAIEX = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST"
TPEX_STOCK_DAY = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
TWSE_OPENAPI_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) quant-grill-lab/66-readonly"
PRICE_FIELDS = ["asof_date", "stock_code", "close", "open", "high", "low", "volume", "source"]
BENCHMARK_FIELDS = ["asof_date", "benchmark_id", "level", "data_asof", "note"]

# TWSE throttles aggressively; this delay keeps a full backfill inside its budget.
DEFAULT_DELAY = 2.6
BENCHMARKS = {"0050": "TWSE_ETF", "TAIEX": "TWSE_INDEX"}


class FetchError(RuntimeError):
    """A market-data endpoint did not return usable rows."""


# --------------------------------------------------------------------------- io


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle) if any(row.values())]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_universe() -> list[dict[str, str]]:
    rows = read_csv(LEDGER_PATH)
    if not rows:
        raise FetchError(f"{LEDGER_PATH.name} is empty; nothing to fetch")
    seen: dict[str, dict[str, str]] = {}
    for row in rows:
        code = (row.get("stock_code") or "").strip()
        if not code:
            continue
        seen.setdefault(
            code,
            {
                "stock_code": code,
                "stock_name": (row.get("stock_name") or "").strip(),
                "market": (row.get("market") or "TWSE").strip().upper() or "TWSE",
            },
        )
    return [seen[code] for code in sorted(seen)]


def ledger_start() -> date:
    starts = [
        datetime.strptime(row["effective_from"].strip(), "%Y-%m-%d").date()
        for row in read_csv(LEDGER_PATH)
        if (row.get("effective_from") or "").strip()
    ]
    if not starts:
        raise FetchError("positions_ledger.csv has no effective_from dates")
    return min(starts)


# ---------------------------------------------------------------------- http


class _KeepGetOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow 307/308 as a GET.

    TWSE answers datacenter IPs (GitHub Actions runners live on Azure) with a
    307 rather than the JSON payload. urllib will not re-issue the request
    unless the redirect is handled explicitly, which is why the scheduled job
    saw "HTTP Error 307" while the same call worked from a desktop.
    """

    def http_error_307(self, req, fp, code, msg, headers):  # noqa: D102
        return self.http_error_302(req, fp, code, msg, headers)

    http_error_308 = http_error_307


_OPENER = urllib.request.build_opener(_KeepGetOnRedirect)


def get_json(url: str, params: dict[str, str], attempts: int = 3) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.twse.com.tw/zh/trading/historical/stock-day.html",
            "Connection": "close",
        },
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with _OPENER.open(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
            return json.loads(payload)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = exc
            time.sleep(DEFAULT_DELAY * (attempt + 2))
    raise FetchError(f"{url} failed after {attempts} attempts: {last_error}")


# ------------------------------------------------------------------- parsing


def roc_to_date(value: str) -> date:
    """Convert a Taiwan ROC date string such as '115/08/03' to a date."""
    parts = value.strip().replace("年", "/").replace("月", "/").replace("日", "").split("/")
    if len(parts) != 3:
        raise FetchError(f"unparseable ROC date: {value!r}")
    year, month, day = (part.strip() for part in parts)
    return date(int(year) + 1911, int(month), int(day))



def roc_compact_to_date(value: str) -> date:
    """Parse an unpunctuated ROC stamp such as '1150824' -> 2026-08-24.

    The OpenAPI feed uses this form while STOCK_DAY uses '115/08/24'. Reading
    it as AD YYYYMMDD silently yields year 1150 and drops every row.
    """
    text = str(value).strip()
    if len(text) < 6 or not text.isdigit():
        raise FetchError(f"unparseable compact ROC date: {value!r}")
    return date(int(text[:-4]) + 1911, int(text[-4:-2]), int(text[-2:]))


def to_number(value: str) -> float | None:
    text = str(value).replace(",", "").replace("+", "").strip()
    if text in {"", "--", "---", "X", "N/A"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if number > 0 else None


def month_starts(start: date, end: date) -> list[date]:
    months: list[date] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        months.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return months


# ------------------------------------------------------------------ sources


def fetch_twse_month(code: str, month: date) -> list[dict[str, Any]]:
    payload = get_json(
        TWSE_STOCK_DAY,
        {"date": month.strftime("%Y%m%d"), "stockNo": code, "response": "json"},
    )
    if payload.get("stat") != "OK":
        return []
    rows: list[dict[str, Any]] = []
    for entry in payload.get("data") or []:
        close = to_number(entry[6])
        if close is None:
            continue
        rows.append(
            {
                "asof_date": roc_to_date(entry[0]).isoformat(),
                "stock_code": code,
                "close": f"{close:.4f}",
                "open": _fmt(to_number(entry[3])),
                "high": _fmt(to_number(entry[4])),
                "low": _fmt(to_number(entry[5])),
                "volume": str(int(to_number(entry[1]) or 0)),
                "source": "TWSE_STOCK_DAY",
            }
        )
    return rows


def fetch_tpex_month(code: str, month: date) -> list[dict[str, Any]]:
    payload = get_json(
        TPEX_STOCK_DAY,
        {"code": code, "date": month.strftime("%Y/%m/%d"), "response": "json"},
    )
    tables = payload.get("tables") or []
    if not tables:
        return []
    rows: list[dict[str, Any]] = []
    for entry in tables[0].get("data") or []:
        close = to_number(entry[6])
        if close is None:
            continue
        rows.append(
            {
                "asof_date": roc_to_date(entry[0]).isoformat(),
                "stock_code": code,
                "close": f"{close:.4f}",
                "open": _fmt(to_number(entry[3])),
                "high": _fmt(to_number(entry[4])),
                "low": _fmt(to_number(entry[5])),
                "volume": str(int((to_number(entry[1]) or 0) * 1000)),
                "source": "TPEX_STOCK_DAY",
            }
        )
    return rows


def fetch_taiex_month(month: date) -> list[dict[str, Any]]:
    payload = get_json(TWSE_TAIEX, {"date": month.strftime("%Y%m%d"), "response": "json"})
    if payload.get("stat") != "OK":
        return []
    rows: list[dict[str, Any]] = []
    for entry in payload.get("data") or []:
        close = to_number(entry[4])
        if close is None:
            continue
        rows.append(
            {
                "asof_date": roc_to_date(entry[0]).isoformat(),
                "stock_code": "TAIEX",
                "close": f"{close:.4f}",
                "open": _fmt(to_number(entry[1])),
                "high": _fmt(to_number(entry[2])),
                "low": _fmt(to_number(entry[3])),
                "volume": "0",
                "source": "TWSE_TAIEX_INDEX",
            }
        )
    return rows


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def fetch_symbol_month(code: str, market: str, month: date) -> tuple[list[dict[str, Any]], str]:
    """Return (rows, resolved_market). TWSE is tried first, TPEx is the fallback."""
    if code == "TAIEX":
        return fetch_taiex_month(month), "TWSE_INDEX"
    order = ["TPEX", "TWSE"] if market == "TPEX" else ["TWSE", "TPEX"]
    for candidate in order:
        rows = fetch_twse_month(code, month) if candidate == "TWSE" else fetch_tpex_month(code, month)
        if rows:
            return rows, candidate
        time.sleep(DEFAULT_DELAY)
    return [], market


def fetch_openapi_latest(codes: set[str]) -> list[dict[str, Any]]:
    """One-shot fallback for the most recent close.

    The per-symbol STOCK_DAY endpoint is the only way to reach history, but it
    is also the one TWSE 307s from datacenter IPs. This OpenAPI feed returns
    every listed stock for the latest session in a single call and carries its
    own Date field, so the daily job can still land today's close when the
    month-by-month path is being turned away. It cannot backfill.
    """
    request = urllib.request.Request(
        TWSE_OPENAPI_DAY_ALL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise FetchError(f"{TWSE_OPENAPI_DAY_ALL} failed: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise FetchError("OpenAPI STOCK_DAY_ALL returned no rows")

    rows: list[dict[str, Any]] = []
    for raw in payload:
        code = str(raw.get("Code", "")).strip()
        if code not in codes:
            continue
        close = to_number(str(raw.get("ClosingPrice", "")))
        raw_date = str(raw.get("Date", "")).strip()
        if close is None or not raw_date:
            continue
        try:
            day = roc_compact_to_date(raw_date)
        except (FetchError, ValueError):
            continue
        rows.append(
            {
                "asof_date": day.isoformat(),
                "stock_code": code,
                "close": f"{close:.4f}",
                "open": _fmt(to_number(str(raw.get("OpeningPrice", "")))),
                "high": _fmt(to_number(str(raw.get("HighestPrice", "")))),
                "low": _fmt(to_number(str(raw.get("LowestPrice", "")))),
                "volume": str(int(to_number(str(raw.get("TradeVolume", ""))) or 0)),
                "source": "TWSE_OPENAPI_STOCK_DAY_ALL",
            }
        )
    return rows


# -------------------------------------------------------------------- merge


def merge_prices(existing: list[dict[str, str]], incoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {
        (row["asof_date"], row["stock_code"]): row for row in existing
    }
    for row in incoming:
        merged[(row["asof_date"], row["stock_code"])] = row
    return [merged[key] for key in sorted(merged)]


def rebuild_benchmark_nav(prices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in prices:
        code = row["stock_code"]
        if code not in BENCHMARKS:
            continue
        rows.append(
            {
                "asof_date": row["asof_date"],
                "benchmark_id": code,
                "level": row["close"],
                "data_asof": row["asof_date"],
                "note": BENCHMARKS[code],
            }
        )
    return sorted(rows, key=lambda row: (row["benchmark_id"], row["asof_date"]))



def _latest_session(prices: list[dict[str, Any]], start: date, end: date) -> str | None:
    in_window = sorted(
        {
            row["asof_date"]
            for row in prices
            if start.isoformat() <= row["asof_date"] <= end.isoformat()
        }
    )
    return in_window[-1] if in_window else None


def _missing_at_latest(
    prices: list[dict[str, Any]], targets: list[dict[str, str]], start: date, end: date
) -> set[str]:
    """Target symbols with no row on the newest session inside the window."""
    latest = _latest_session(prices, start, end)
    if latest is None:
        return {item["stock_code"] for item in targets}
    covered = {row["stock_code"] for row in prices if row["asof_date"] == latest}
    return {item["stock_code"] for item in targets} - covered


# --------------------------------------------------------------------- main


def run(start: date, end: date, delay: float, only: set[str] | None = None) -> dict[str, Any]:
    universe = load_universe()
    targets = [dict(item) for item in universe]
    if only:
        known = {item['stock_code'] for item in targets} | set(BENCHMARKS)
        unknown = only - known
        if unknown:
            raise FetchError(f"--only contains codes outside the ledger: {sorted(unknown)}")
        targets = [item for item in targets if item['stock_code'] in only]
    for code in sorted(BENCHMARKS):
        if only and code not in only:
            continue
        if all(item["stock_code"] != code for item in targets):
            targets.append({"stock_code": code, "stock_name": code, "market": "TWSE"})

    months = month_starts(start, end)
    existing = read_csv(PRICE_PATH)
    collected: list[dict[str, Any]] = []
    per_symbol: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    total = len(targets) * len(months)
    step = 0
    for target in targets:
        code = target["stock_code"]
        kept = 0
        resolved = target["market"]
        for month in months:
            step += 1
            try:
                rows, resolved = fetch_symbol_month(code, target["market"], month)
            except FetchError as exc:
                failures.append(f"{code}@{month:%Y-%m}: {exc}")
                rows = []
            in_window = [row for row in rows if start.isoformat() <= row["asof_date"] <= end.isoformat()]
            collected.extend(in_window)
            kept += len(in_window)
            print(
                f"[{step}/{total}] {code} {month:%Y-%m} -> {len(in_window)} rows ({resolved})",
                flush=True,
            )
            time.sleep(delay)
        per_symbol[code] = {"rows": kept, "market": resolved, "name": target["stock_name"]}
        if kept == 0:
            failures.append(f"{code}: no rows in {start}..{end}")

    # TWSE rate-limits datacenter IPs unevenly: some symbols answer, others 307
    # in the same run. So decide the fallback on WHICH symbols are missing at
    # the newest session, not on whether any symbol happened to succeed.
    prices = merge_prices(existing, collected)
    stock_codes = {item["stock_code"] for item in targets if item["stock_code"] != "TAIEX"}
    missing = _missing_at_latest(prices, targets, start, end)
    if missing & stock_codes:
        try:
            recovered = [
                row
                for row in fetch_openapi_latest(missing & stock_codes)
                if start.isoformat() <= row["asof_date"] <= end.isoformat()
            ]
        except FetchError as exc:
            failures.append(f"openapi fallback: {exc}")
            recovered = []
        if recovered:
            collected.extend(recovered)
            print(
                f"[fallback] OpenAPI recovered {len(recovered)} rows for "
                f"{max(row['asof_date'] for row in recovered)}: "
                f"{sorted({row['stock_code'] for row in recovered})}",
                flush=True,
            )
            for code in {row["stock_code"] for row in recovered}:
                per_symbol.setdefault(code, {"rows": 0, "market": "TWSE", "name": code})
                per_symbol[code]["rows"] += 1
            prices = merge_prices(existing, collected)


    write_csv(PRICE_PATH, PRICE_FIELDS, prices)
    benchmark_rows = rebuild_benchmark_nav(prices)
    write_csv(BENCHMARK_PATH, BENCHMARK_FIELDS, benchmark_rows)

    dates = sorted({row["asof_date"] for row in prices})
    # The daily job only needs the newest session. A backfill month that 307s
    # is a warning, not a build failure -- previously ANY failure exited 1 and
    # took the whole scheduled workflow down with it.
    latest_day = _latest_session(prices, start, end)
    missing_at_latest = sorted(_missing_at_latest(prices, targets, start, end))
    receipt = {
        "status": (
            "SUCCESS"
            if latest_day and not missing_at_latest
            else "PARTIAL"
            if latest_day
            else "FAILED"
        ),
        "latest_session": latest_day,
        "missing_at_latest_session": missing_at_latest,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "requested_symbols": len(targets),
        "price_rows_total": len(prices),
        "price_rows_added_or_refreshed": len(collected),
        "trading_days": len(dates),
        "coverage": {"first": dates[0] if dates else None, "last": dates[-1] if dates else None},
        "per_symbol": per_symbol,
        "benchmark_rows": len(benchmark_rows),
        "failures": failures,
        "endpoints": [TWSE_STOCK_DAY, TWSE_TAIEX, TPEX_STOCK_DAY],
        "safety": {
            "broker_import": False,
            "credential_access": False,
            "order_capability": False,
            "public_market_data_only": True,
        },
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "fetch_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return receipt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch TWSE/TPEx daily closes for the 66 lane.")
    parser.add_argument("--start", help="inclusive start date, YYYY-MM-DD")
    parser.add_argument("--end", help="inclusive end date, YYYY-MM-DD (default: today)")
    parser.add_argument("--full", action="store_true", help="backfill from the ledger start date")
    parser.add_argument("--lookback", type=int, default=45, help="incremental lookback in days")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="seconds between requests")
    parser.add_argument(
        "--only",
        help="comma-separated stock codes to fetch instead of the whole ledger universe",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    end = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()
    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
    elif args.full:
        start = ledger_start()
    else:
        start = end - timedelta(days=args.lookback)
    if start > end:
        print(f"FAIL: start {start} is after end {end}", file=sys.stderr)
        return 2

    only = {code.strip() for code in args.only.split(",") if code.strip()} if args.only else None
    receipt = run(start, end, max(args.delay, 0.0), only)
    print(f"{receipt['status']}: {receipt['price_rows_total']} price rows, {receipt['trading_days']} trading days")
    print(f"COVERAGE: {receipt['coverage']['first']} .. {receipt['coverage']['last']}")
    for failure in receipt["failures"]:
        print(f"WARN: {failure}", file=sys.stderr)
    return 0 if receipt["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
