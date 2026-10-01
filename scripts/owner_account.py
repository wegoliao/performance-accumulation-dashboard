"""Replay confirmed quantities separately from missing settlement amounts.

Owner reports close positions even when net cash is not yet supplied. Exact
historic settled cash stays in actual_fills; estimated new fees never enter it.
All calculations use Decimal. No broker, credential or order capability.
"""
import csv
import html
from collections import defaultdict, deque
from decimal import Decimal as D
from pathlib import Path

COMMISSION = D('0.001425')
SELL_TAX = D('0.003')

def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def money(x):
    return f'{x:,.2f}'

def apply_close(positions, code, shares):
    if shares > D(str(positions.get(code, 0))):
        raise ValueError(f'{code} close exceeds confirmed position')
    positions[code] = D(str(positions[code])) - shares
    if positions[code] == 0:
        del positions[code]

def fee_estimate(gross, sell=False):
    # Existing dashboard display model: truncate each order's fee and tax.
    # These are estimates; actual discounts, minimums and borrowing are unknown.
    return D(int(gross * COMMISSION)) + (D(int(gross * SELL_TAX)) if sell else D(0))

def load_account(root: Path):
    inputs = root / 'inputs'
    raw = read(inputs / 'actual_fills.csv')
    books = defaultdict(deque)
    names = {}
    realized = D(0)
    for r in sorted(raw, key=lambda r:(r['fill_date'], r['trade_id'])):
        code, shares = r['stock_code'], D(r['shares'])
        names[code] = r['stock_name']
        key = (r['strategy_id'], code)
        if r['side'] == 'BUY':
            books[key].append([shares, D(r['cash_out_twd']) / shares])
        else:
            remaining = shares
            proceeds_unit = D(r['cash_in_twd']) / shares
            while remaining:
                if not books[key]:
                    raise ValueError(f'{code} historical sell exceeds settled buys')
                lot = books[key][0]
                matched = min(remaining, lot[0])
                realized += matched * (proceeds_unit - lot[1])
                remaining -= matched; lot[0] -= matched
                if not lot[0]: books[key].popleft()
    positions = defaultdict(lambda:D(0))
    costs = defaultdict(lambda:D(0))
    for (_,code), lots in books.items():
        for shares, unit in lots:
            positions[code] += shares
            costs[code] += shares * unit
    positions = {code:v for code,v in positions.items() if v}
    reports=[]
    seen=set()
    for p in sorted(inputs.glob('owner_reported_fills_*.csv')):
        for r in read(p):
            key=(r['source'],r['report_id'])
            if key in seen:
                raise ValueError('duplicate owner report row')
            seen.add(key); reports.append(r)
    if not reports:
        return None
    groups = {}
    for r in reports:
        if r['status']=='ALREADY_RECORDED': continue
        key=(r['trade_date'],r['order_id'],r['stock_code'],r['transaction_type'])
        group=groups.setdefault(key,{'date':r['trade_date'],'order_id':r['order_id'],'stock_code':r['stock_code'],'stock_name':r['stock_name'],'type':r['transaction_type'],'shares':D(0),'gross':D(0),'rows':0})
        group['shares']+=D(r['shares']);group['gross']+=D(r['consideration_twd']);group['rows']+=1
    new_closes=[]
    short_sells=defaultdict(list)
    short_remaining=defaultdict(lambda:D(0))
    short_pnl=short_cost=D(0)
    for key, group in sorted(groups.items()):
        code=group['stock_code']; shares=group['shares']
        names[code]=group['stock_name']
        if group['type']=='現賣':
            # If a later reconciled actual_fills import exists, do not replay it.
            settled=[r for r in raw if r['fill_date']==group['date'] and r['stock_code']==code and r['side']=='SELL' and r['trade_id'].split('-2026')[0]==group['order_id']]
            if settled:
                if sum(D(r['shares']) for r in settled)!=shares:
                    raise ValueError(f'{code} partial settled/report overlap requires reconciliation')
                continue
            original=positions.get(code,D(0))
            apply_close(positions,code,shares)
            entry=costs[code]*shares/original
            costs[code]-=entry
            new_closes.append({**group,'entry_cash_out':entry,'pnl_before_sell_cost':group['gross']-entry,'estimated_sell_cost':fee_estimate(group['gross'],True)})
        elif group['type']=='券賣':
            short_sells[code].append([shares, group['gross']/shares])
            short_remaining[code]+=shares
            short_cost+=fee_estimate(group['gross'],True)
        elif group['type']=='券買':
            remaining=shares
            while remaining:
                if not short_sells[code]: raise ValueError(f'{code} cover exceeds reported short opening')
                lot=short_sells[code][0];matched=min(remaining,lot[0])
                short_pnl+=matched*(lot[1]-group['gross']/shares)
                lot[0]-=matched;remaining-=matched
                if lot[0]==0:short_sells[code].pop(0)
            short_remaining[code]-=shares
            short_cost+=fee_estimate(group['gross'])
    price_rows=read(inputs/'price_history.csv')
    marks={}
    for r in sorted(price_rows,key=lambda r:r['asof_date']):
        if r['stock_code'] in positions:
            marks[r['stock_code']]={'date':r['asof_date'],'close':D(r['close']),'source':r['source']}
    if set(marks)!=set(positions):raise ValueError('missing official marks for open positions')
    known=realized + sum((r['pnl_before_sell_cost'] for r in new_closes),D(0)) + short_pnl
    estimated_cost=short_cost+sum((r['estimated_sell_cost'] for r in new_closes),D(0))
    return {'asof':max(r['trade_date'] for r in reports),'settled_book_asof':max(r['fill_date'] for r in raw),'positions':positions,'costs':{k:costs[k] for k in positions},'names':names,'marks':marks,'new_long_closes':new_closes,'settled_realized':realized.quantize(D('.01')),'short_price_pnl':short_pnl,'short_estimated_cost':short_cost,'short_net_estimate':short_pnl-short_cost,'short_remaining_shares':sum(short_remaining.values(),D(0)),'known_realized_before_missing_costs':known.quantize(D('.01')),'net_realized_estimate':(known-estimated_cost).quantize(D('.01')),'exact_net_status':'UNKNOWN_UNREPORTED_FEES','estimated_cost':estimated_cost}

def render(r):
    if not r: return ''
    h=html.escape
    rows=[]
    for x in r['new_long_closes']:
        net=x['pnl_before_sell_cost']-x['estimated_sell_cost']
        rows.append(f'<tr><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td>{x["date"]}</td><td>現賣・已平倉</td><td>{x["shares"]:g} 股</td><td>NT$ {money(x["gross"])}</td><td>NT$ {money(x["entry_cash_out"])}</td><td>NT$ {money(net)}</td><td>出場費稅估算 {money(x["estimated_sell_cost"])}</td></tr>')
    rows.append(f'<tr><td>3055 蔚華科</td><td>2026-09-23</td><td>券買・已回補</td><td>1,000 股</td><td>9/10 券賣 147 → 券買 217</td><td>價差損失 NT$ {money(r["short_price_pnl"])}</td><td><b>NT$ {money(r["short_net_estimate"])}</b></td><td>費稅估算 {money(r["short_estimated_cost"])}；另有借券費</td></tr>')
    positions=[]
    for code, shares in sorted(r['positions'].items()):
        mark=r['marks'][code]
        positions.append(f'<tr><td>{h(code)} {h(r["names"][code])}</td><td>{shares:g} 股</td><td>NT$ {money(r["costs"][code])}</td><td>{money(mark["close"])}</td><td>{mark["date"]}</td><td>NT$ {money(shares*mark["close"])}</td></tr>')
    return f'''<article class="panel full" id="owner-account" style="border-color:var(--red)"><h2>目前帳戶・{r['asof']} 已回報成交</h2><div class="metrics"><div class="metric-card"><b>蔚華科已回補・費後試算</b><h3 class="metric-value negative">NT$ {money(r['short_net_estimate'])}</h3><p>成交價差 −70,000；交易費稅估算 {money(r['short_estimated_cost'])}，另加未提供借券費。</p></div><div class="metric-card"><b>帳戶累積已實現・費後試算</b><h3 class="metric-value negative">NT$ {money(r['net_realized_estimate'])}</h3><p>原成交簿已實現 {money(r['settled_realized'])}，加兩檔新賣出、扣融券損失及估算費稅。精確淨損益待券商費稅／借券費，不含未入帳股息或券商成本調整。</p></div><div class="metric-card"><b>目前追蹤多頭部位</b><h3>{len(r['positions'])} 檔</h3><p>鴻準、緯創已平倉；蔚華科已回補，融券剩餘 {r['short_remaining_shares']:g} 股。</p></div></div><p>新成交費稅採原儀表板展示模型：手續費 0.1425%、現賣／券賣稅 0.3%，每張委託金額分別取整。僅為<b>費稅估算</b>，未確認折扣、最低收費與借券費；實際費用可能不同。原成交簿的費稅與淨收付保持原樣。</p><div class="table-wrap"><table><thead><tr><th>股票</th><th>平倉日</th><th>狀態</th><th>數量</th><th>成交回報</th><th>原始實付／價差</th><th>費後試算</th><th>費用口徑</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><h3>目前追蹤持股・{len(r['positions'])} 檔</h3><p>股數依原成交簿加 Owner 已貼的平倉回報重建；未回報的其他交易無法推測。行情是表列最近官方資料日，毛市值參考不是今日已核對庫存或可成交淨收款。</p><div class="table-wrap"><table><thead><tr><th>股票</th><th>追蹤股數</th><th>原買入實付總額</th><th>最近官方收盤參考</th><th>行情日</th><th>毛市值參考</th></tr></thead><tbody>{''.join(positions)}</tbody></table></div><p><a href="intake/">逐筆成交來源 CSV</a>・下方四策略曲線截至官方行情日，僅四策略；本區另外納入實際融券損益，不能把兩者當同一績效。</p></article>'''
