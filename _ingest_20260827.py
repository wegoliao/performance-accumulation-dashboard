"""Ingest 2026-08-27: holdings, the 2606 exit fill, and the four cards."""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ASOF = "2026-08-27"
PREV = "2026-08-26"
ROOT = Path(__file__).resolve().parent
INPUTS = ROOT / "inputs"
GAP_FLAG_PP = 0.5

FEE_RATE = 0.001425
TAX_RATE = 0.003

# 代碼, 名稱, 股數, 付出成本, 成本均價, 現價, 現值, 損益, 獲利率
PASTE = [
    ("1590", "亞德客-KY", 63, 96842, 1537.17, 1480.00, 92829, -4013, -4.13),
    ("1709", "和益", 3949, 103758, 26.27, 34.15, 134266, 30508, 29.40),
    ("2059", "川湖", 5, 59730, 11946.00, 14340.00, 71383, 11653, 19.51),
    ("2103", "台橡", 2584, 71719, 27.76, 27.75, 71390, -329, -0.46),
    ("2301", "光寶科", 626, 168449, 269.09, 301.00, 187595, 19146, 11.37),
    ("2354", "鴻準", 788, 49556, 62.89, 64.40, 50523, 967, 1.95),
    ("2395", "研華", 106, 70802, 667.94, 675.00, 71235, 433, 0.61),
    ("2397", "友通", 1045, 68544, 65.59, 63.80, 66378, -2166, -3.16),
    ("2408", "南亞科", 198, 94580, 477.68, 541.00, 106645, 12065, 12.76),
    ("2609", "陽明", 1000, 54777, 54.78, 57.90, 57645, 2868, 5.24),
    ("2637", "慧洋-KY", 1000, 97939, 97.94, 94.60, 94183, -3756, -3.84),
    ("2886", "兆豐金", 1, 40, 40.00, 46.15, 45, 5, 12.50),
    ("3006", "晶豪科", 257, 68974, 268.38, 274.00, 70107, 1133, 1.64),
    ("3044", "健鼎", 200, 99141, 495.71, 481.00, 95775, -3366, -3.40),
    ("6672", "騰輝電子-KY", 347, 97993, 282.40, 281.50, 97248, -745, -0.76),
]
SUBTOTAL = dict(shares=12169, cost=1202844, value=1267247, pnl=64403, ret=5.35)

# 現賣 2606 裕民, acting on the 2026-08-26 TRUST card's 出 signal
SELL = dict(
    trade_id="X-06D7", strategy_id="TRUST", code="2606", name="裕民",
    fill_date=ASOF, fill_time="12:24:20", price=73.80, shares=1000,
    consideration=73800, signal_ref=72.00, signal_date="2026-08-26",
)

CARDS = {
    "TRUST": (3.0, [
        ("2301", "光寶科", "電腦及週邊", "268.0", 301.0, 12.2, "+", "抱"),
        ("2609", "陽明", "航運業", "55.1", 57.9, 4.9, "+", "抱"),
        ("3044", "健鼎", "電子零組件", "492.0", 481.0, 2.4, "-", "出"),
        ("6672", "騰輝電子-KY", "電子零組件", "280.0", 281.5, 0.4, "+", "抱"),
        ("6727", "亞泰金屬", "電子零組件", "明開", 520.0, 0.0, "+", "進"),
    ]),
    "YOY": (4.3, [
        ("2059", "川湖", "電子零組件", "11700", 14340.0, 22.8, "+", "抱"),
        ("2103", "台橡", "橡膠工業", "27.80", 27.75, 0.3, "-", "抱"),
        ("2301", "光寶科", "電腦及週邊", "274.0", 301.0, 10.7, "+", "抱"),
        ("2395", "研華", "電腦及週邊", "660", 675.0, 2.1, "+", "抱"),
        ("2397", "友通", "電腦及週邊", "67.1", 63.8, 5.1, "-", "抱"),
        ("3006", "晶豪科", "半導體業", "274.0", 274.0, 0.1, "+", "抱"),
    ]),
    "MARGIN": (10.5, [
        ("1709", "和益", "化學工業", "32.55", 34.15, 4.8, "+", "抱"),
        ("1714", "和桐", "化學工業", "12.10", 16.55, 38.2, "+", "抱"),
        ("2030", "彰源", "鋼鐵工業", "18.00", 23.00, 27.6, "+", "抱"),
        ("2354", "鴻準", "其他電子業", "63.1", 64.4, 1.9, "+", "抱"),
        ("2637", "慧洋-KY", "航運業", "97.8", 94.6, 3.4, "-", "抱"),
        ("3046", "建碁", "電腦及週邊", "57.7", 57.7, 0.1, "-", "抱"),
        ("3605", "宏致", "電子零組件", "108.0", 119.5, 12.0, "+", "抱"),
        ("6213", "聯茂", "電子零組件", "424.0", 563.0, 32.6, "+", "抱"),
        ("6570", "維田", "電腦及週邊", "56.9", 52.8, 7.3, "-", "抱"),
        ("6603", "富強鑫", "電機機械", "27.00", 26.75, 1.1, "-", "抱"),
    ]),
    "BREAKOUT": (8.0, [
        ("1590", "亞德客-KY", "電機機械", "1560", 1480.0, 5.3, "-", "抱"),
        ("1709", "和益", "化學工業", "27.40", 34.15, 24.5, "+", "抱"),
        ("2395", "研華", "電腦及週邊", "661", 675.0, 2.1, "+", "抱"),
        ("2408", "南亞科", "半導體業", "489.0", 541.0, 10.5, "+", "抱"),
    ]),
}


def read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [r for r in reader if any(r.values())]


def closes() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    _, rows = read(INPUTS / "price_history.csv")
    for row in rows:
        if row["asof_date"] in {ASOF, PREV} and row.get("close"):
            out.setdefault(row["stock_code"], {})[row["asof_date"]] = float(row["close"])
    return out


def main() -> int:
    official = closes()

    # ---------------------------------------------------------------- holdings
    print(f"=== 畫面現價 vs 交易所 {ASOF} 收盤 ===")
    bad = 0
    for code, name, *_rest in ((r[0], r[1]) for r in PASTE):
        pass
    for code, name, shares, cost, avg, price, value, pnl, ret in PASTE:
        today = official.get(code, {}).get(ASOF)
        if today is None:
            print(f"  {code} {name}: NO_OFFICIAL_CLOSE"); bad += 1
        elif abs(today - price) > 1e-9:
            print(f"  {code} {name}: 畫面 {price:g} vs 交易所 {today:g}"); bad += 1
    print("  全部 15 檔與交易所收盤一致" if not bad else f"  {bad} 檔不一致")

    print("\n=== 成本基礎變動（對照昨日快照）===")
    _, prev_rows = read(INPUTS / f"holdings_snapshot_{PREV}.csv")
    prev_cost = {r["stock_code"]: float(r["cost_basis_twd"]) for r in prev_rows}
    prev_shares = {r["stock_code"]: float(r["shares"]) for r in prev_rows}
    moved = []
    for code, name, shares, cost, avg, *_ in PASTE:
        before = prev_cost.get(code)
        if before is None or abs(before - cost) < 0.5:
            continue
        if abs(prev_shares.get(code, 0) - shares) > 1e-9:
            continue  # share count changed too; not a pure basis adjustment
        delta = cost - before
        moved.append((code, name, before, cost, delta, delta / shares))
        print(
            f"  {code} {name}: 成本 {before:,.0f} -> {cost:,.0f} "
            f"（{delta:+,.0f}，每股 {delta / shares:+.4f}）股數未變"
        )
    if not moved:
        print("  無")

    total_value = sum(r[6] for r in PASTE)
    rows = []
    for code, name, shares, cost, avg, price, value, pnl, ret in PASTE:
        prev = official.get(code, {}).get(PREV)
        change = round(price - prev, 4) if prev is not None else None
        rows.append({
            "asof_date": ASOF, "captured_at": "", "source": "USER_PASTED_EOD",
            "category": "現股", "stock_code": code, "stock_name": name,
            "shares": shares, "yesterday_shares": shares,
            "current_value_twd": value, "buy_value_today_twd": 0,
            "sell_value_today_twd": 0, "cost_basis_twd": cost,
            "avg_cost": avg, "last_price": price,
            "price_change": "" if change is None else f"{change:g}",
            "price_change_pct": "" if not prev else f"{change / prev * 100:.2f}",
            "unrealized_pnl_twd": pnl,
            "source_allocation_pct": f"{value / total_value * 100:.2f}",
            "unrealized_return_pct": ret, "currency": "TWD", "action": "",
        })

    print("\n=== 小計對帳 ===")
    for label, got, want in (
        ("股數", sum(r["shares"] for r in rows), SUBTOTAL["shares"]),
        ("成本", sum(r["cost_basis_twd"] for r in rows), SUBTOTAL["cost"]),
        ("現值", sum(r["current_value_twd"] for r in rows), SUBTOTAL["value"]),
        ("損益", sum(r["unrealized_pnl_twd"] for r in rows), SUBTOTAL["pnl"]),
    ):
        print(f"  {label}: {got:,} vs {want:,} -> {'OK' if got == want else 'MISMATCH'}")

    target = INPUTS / f"holdings_snapshot_{ASOF}.csv"
    with target.open("w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    with (INPUTS / f"snapshot_summary_{ASOF}.csv").open("w", encoding="utf-8", newline="") as h:
        w = csv.writer(h)
        w.writerow(["asof_date", "shares", "yesterday_shares", "current_value_twd",
                    "buy_value_today_twd", "sell_value_today_twd", "cost_basis_twd",
                    "unrealized_pnl_twd", "unrealized_return_pct", "currency", "source"])
        w.writerow([ASOF, SUBTOTAL["shares"], 13169, SUBTOTAL["value"], 0,
                    SELL["consideration"], SUBTOTAL["cost"], SUBTOTAL["pnl"],
                    SUBTOTAL["ret"], "TWD", "USER_PASTED_SUBTOTAL"])

    # -------------------------------------------------------------------- fill
    fee = math.floor(SELL["consideration"] * FEE_RATE)
    tax = math.floor(SELL["consideration"] * TAX_RATE)
    cash_in = SELL["consideration"] - fee - tax
    print(f"\n=== {SELL['code']} {SELL['name']} 賣出費稅（推導，回報單未附）===")
    print(f"  價金 {SELL['consideration']:,} · 手續費 {fee} · 證交稅 {tax} · 實收 {cash_in:,}")
    print("  公式已用 3702 那筆實際回報驗證過（無條件捨去到整數元）")

    fields, fills = read(INPUTS / "actual_fills.csv")
    if not any(r["trade_id"] == SELL["trade_id"] for r in fills):
        fills.append({
            "trade_id": SELL["trade_id"], "strategy_id": SELL["strategy_id"],
            "stock_code": SELL["code"], "stock_name": SELL["name"], "side": "SELL",
            "fill_date": SELL["fill_date"], "fill_time": SELL["fill_time"],
            "fill_price": f"{SELL['price']:g}", "shares": str(SELL["shares"]),
            "consideration_twd": str(SELL["consideration"]), "fee_twd": str(fee),
            "tax_twd": str(tax), "cash_out_twd": "0", "cash_in_twd": str(cash_in),
            "currency": "TWD", "source": f"owner_pasted_fill_{ASOF}T{SELL['fill_time']}",
        })
        with (INPUTS / "actual_fills.csv").open("w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=fields); w.writeheader(); w.writerows(fills)
        print("  已寫入 actual_fills.csv")

    sf_fields, sf_rows = read(INPUTS / "signal_fills.csv")
    if not any(r["fill_date"] == ASOF and r["stock_code"] == SELL["code"] for r in sf_rows):
        sf_rows.append({
            "signal_date": SELL["signal_date"], "effective_date": ASOF,
            "strategy_id": SELL["strategy_id"], "stock_code": SELL["code"],
            "stock_name": SELL["name"], "action": "SELL",
            "signal_ref_price": f"{SELL['signal_ref']:g}", "signal_basis": "CARD_CLOSE",
            "fill_date": ASOF, "fill_time": SELL["fill_time"],
            "fill_price": f"{SELL['price']:g}", "shares": str(SELL["shares"]),
            "source": f"owner_pasted_fill_{ASOF}",
        })
        with (INPUTS / "signal_fills.csv").open("w", encoding="utf-8", newline="") as h:
            w = csv.DictWriter(h, fieldnames=sf_fields); w.writeheader(); w.writerows(sf_rows)
        edge = (SELL["price"] / SELL["signal_ref"] - 1.0) * 10000
        print(f"  訊號參考價 {SELL['signal_ref']:g} -> 成交 {SELL['price']:g}："
              f"賣方有利 {edge:.0f} bp")

    # ------------------------------------------------------------------- cards
    print("\n=== 策略卡表頭對帳 ===")
    signal_rows, card_rows = [], []
    for sid, (header, members) in CARDS.items():
        signed = []
        for code, name, ind, entry_display, close, printed, sign, sig in members:
            value = printed if sign == "+" else -printed
            signed.append(value)
            note = ""
            try:
                entry = float(entry_display)
                implied = (close / entry - 1.0) * 100.0
                if abs(implied - value) >= GAP_FLAG_PP:
                    note = (f"PRINTED_PCT_VS_ENTRY_CLOSE_GAP:printed={printed}{sign},"
                            f"implied={implied:+.1f}")
                entry_price = entry_display
            except ValueError:
                # "明開" -- the entry price does not exist until tomorrow's open
                entry_price = ""
                note = "ENTRY_PRICE_PENDING_NEXT_OPEN"
            signal_rows.append([
                ASOF, "2026-08-28" if sig in {"進", "出"} else "", sid, code, name, ind,
                entry_display, entry_price, f"{close:g}", f"{printed:g}", sign,
                f"{value:g}", sig, f"owner_strategy_card_{ASOF}", note,
            ])
        visible = sum(signed) / len(signed)
        gap = abs(visible - header)
        basis = ("SOURCE_HEADER_WITH_VISIBLE_MEMBER_MISMATCH" if gap >= GAP_FLAG_PP
                 else "EQUAL_WEIGHT_HELD_MEMBERS")
        card_rows.append([ASOF, sid, f"{header:g}", basis, f"owner_strategy_card_{ASOF}"])
        print(f"  {sid:9s} 表頭 {header:+.1f}% vs 成分平均 {visible:+.2f}%  差 {gap:.2f}pp  {basis}")

    flagged = [r for r in signal_rows if r[-1]]
    print(f"\n逐檔標註：{len(flagged)} 檔")
    for r in flagged:
        print(f"  {r[2]:9s} {r[3]} {r[4]}: {r[-1]}")

    ls_fields, _ = read(INPUTS / "latest_strategy_signals.csv")
    hist_fields, hist = read(INPUTS / "signal_history.csv")
    seen = {(r["asof_date"], r["strategy_id"], r["stock_code"]) for r in hist}
    for row in signal_rows:
        rec = dict(zip(ls_fields, [str(v) for v in row]))
        if (rec["asof_date"], rec["strategy_id"], rec["stock_code"]) not in seen:
            hist.append(rec)
    hist.sort(key=lambda r: (r["asof_date"], r["strategy_id"], r["stock_code"]))
    with (INPUTS / "signal_history.csv").open("w", encoding="utf-8", newline="") as h:
        w = csv.DictWriter(h, fieldnames=hist_fields); w.writeheader(); w.writerows(hist)
    with (INPUTS / "latest_strategy_signals.csv").open("w", encoding="utf-8", newline="") as h:
        w = csv.writer(h); w.writerow(ls_fields); w.writerows(signal_rows)
    with (INPUTS / "strategy_card_returns.csv").open("a", encoding="utf-8", newline="") as h:
        csv.writer(h).writerows(card_rows)

    days = sorted({r["asof_date"] for r in hist})
    print(f"\nsignal_history.csv: {len(hist)} 列，{len(days)} 天 ({days[0]} ~ {days[-1]})")
    print(f"WROTE: {target.name} 等 6 個檔案")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
