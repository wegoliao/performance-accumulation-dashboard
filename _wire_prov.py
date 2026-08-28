"""Place the provisional banner and mark provisional lots wherever they surface."""
from pathlib import Path

p = Path("scripts/build_dashboard.py")
s = p.read_text(encoding="utf-8")

# 1. banner immediately under the hero cards
panel_anchor = '<article class="panel full"><h2>最新四策略卡 · {{SIGNAL_ASOF}} 收盤</h2>'
assert s.count(panel_anchor) == 1
s = s.replace(panel_anchor, "{{PROVISIONAL_BANNER}}\n" + panel_anchor, 1)

sub_anchor = '        "{{COST_GAP_ROWS}}": cost_gap_rows,'
assert s.count(sub_anchor) == 1
s = s.replace(
    sub_anchor,
    '        "{{PROVISIONAL_BANNER}}": provisional_banner(provisional_exits),\n' + sub_anchor,
    1,
)

# 2. closed-lot table must say which rows are not final
old_lot = '''        rows.append(
            "<tr>"
            f'<td>{html.escape(label)}</td>'
            f'<td>{lot["stock_code"]} {html.escape(lot["stock_name"])}</td>'''
assert s.count(old_lot) == 1
new_lot = '''        tag = (
            '<br><small style="color:var(--gold)">待確認 · 暫計</small>'
            if lot.get("provisional")
            else ""
        )
        rows.append(
            "<tr>"
            f'<td>{html.escape(label)}</td>'
            f'<td>{lot["stock_code"]} {html.escape(lot["stock_name"])}{tag}</td>'''
s = s.replace(old_lot, new_lot, 1)

# realized.closed_lots must carry the flag through
r = Path("scripts/realized.py")
rt = r.read_text(encoding="utf-8")
old_r = '''                    "buy_trade_id": lot["trade_id"],
                    "sell_trade_id": fill.get("trade_id", ""),'''
assert rt.count(old_r) == 1
rt = rt.replace(
    old_r,
    old_r
    + '''
                    # A provisional exit is priced at a published close, not at
                    # a real fill. Every surface that reports it must be able
                    # to say so, so the flag travels with the lot.
                    "provisional": bool(fill.get("provisional")),''',
    1,
)
r.write_text(rt, encoding="utf-8", newline="\n")

# 3. headline must not present an incomplete realized figure as final
old_card = '''                    f"{len(pnl_breakdown['_lots'])} 筆平倉的實收現金 − 實付成本；"
                    "賣掉的部位不在庫存表裡，但錢已經動了"'''
assert s.count(old_card) == 1
new_card = '''                    f"{len(pnl_breakdown['_lots'])} 筆平倉的實收現金 − 實付成本；"
                    "賣掉的部位不在庫存表裡，但錢已經動了"
                    + (
                        f"。其中 {provisional_lots} 筆仍以收盤價暫計，等回報"
                        if provisional_lots
                        else ""
                    )'''
s = s.replace(old_card, new_card, 1)

old_total = '''                "在庫未實現 + 已實現；這才是開戶至今的實際結果",'''
assert s.count(old_total) == 1
s = s.replace(
    old_total,
    '''                (
                    "在庫未實現 + 已實現；這才是開戶至今的實際結果"
                    + ("（含暫計，尚未定案）" if provisional_lots else "")
                ),''',
    1,
)

old_calc = '    realized_total = pnl_breakdown["_totals"]["realized_pnl_twd"]'
assert s.count(old_calc) == 1
s = s.replace(
    old_calc,
    old_calc
    + '\n    provisional_lots = sum(1 for lot in pnl_breakdown["_lots"] if lot.get("provisional"))',
    1,
)

p.write_text(s, encoding="utf-8", newline="\n")
print("wired provisional banner and lot flags")
