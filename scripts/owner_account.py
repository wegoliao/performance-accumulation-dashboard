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

def preference(root, strategy, card_day):
    path=root/'inputs/owner_preferences.csv'
    if not path.exists():return ''
    for r in read(path):
        if r['strategy_id']==strategy and r['card_asof']==str(card_day) and r['status']=='OWNER_DECLINED_FOLLOWING':
            return 'Owner 本次暫不跟進'
    return ''

def render_statement(r):
    """Three plainly separated metrics, then current positions and audit detail."""
    h=html.escape
    def amount(x):
        cls='negative' if x<0 else 'positive' if x>0 else 'neutral'
        return f'<span class="{cls}">{x:+,.2f}</span>'
    metrics=[]
    for title,key,note in [
        ('累積已實現損益','realized','已平倉成交的實際收付，已含券商費稅、借券費與利息。'),
        ('目前持股未實現損益','unrealized','9 檔持股收盤市值 − 實際買入支出；尚未賣出，未扣未來賣出費稅。'),
        ('已實現 ＋ 未實現','combined','上面兩項相加，方便看總交易損益；不是帳戶餘額。')]:
        metrics.append(f'<div class="metric-card"><div class="metric-label">{title}</div><div class="metric-value">NT$ {amount(r[key])}</div><p class="metric-note">{note}</p></div>')
    positions=[]
    for c,q in sorted(r['positions'].items()):
        cost=r['costs'][c];price=r['marks'][c]['close'];value=q*price;pnl=value-cost
        positions.append(f'<tr><td>{h(c)} {h(r["names"][c])}</td><td class="num">{q:,.0f}</td><td class="num">{cost/q:,.4f}</td><td class="num">{price:,.2f}</td><td class="num">{cost:,.2f}</td><td class="num">{value:,.2f}</td><td class="num">{amount(pnl)}</td><td class="num">{amount(pnl/cost*100)}%</td></tr>')
    positions.append(f'<tr><th colspan="4">9 檔合計</th><th class="num">{r["open_cost"]:,.2f}</th><th class="num">{r["market_value"]:,.2f}</th><th class="num">{amount(r["unrealized"])}</th><th></th></tr>')
    recent=[]
    for x in r['long_closes']:
        if x['date']==r['asof']:
            recent.append(f'<tr><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td>{x["date"]}</td><td>現賣・已平倉 {x["shares"]:,.0f} 股</td><td class="num">{x["entry_cash_out"]:,.2f}</td><td class="num">{x["proceeds"]:,.2f}</td><td class="num">{amount(x["pnl"])}</td></tr>')
    for x in r['short_closes']:
        recent.append(f'<tr><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td>{x["date"]}</td><td>券買・已回補 {x["shares"]:,.0f} 股</td><td class="num">保證金 {x["margin_paid"]:,.2f}</td><td class="num">返還 {x["released_cash"]:,.2f}</td><td class="num"><b>{amount(x["pnl"])}</b></td></tr>')
    short_detail=''.join(f'<p>{h(x["stock_name"])}：價差 {x["price_pnl"]:,.0f} − 手續費 {x["fees"]:,.0f} − 交易稅 {x["tax"]:,.0f} − 借券費 {x["borrow_fee"]:,.0f} ＋ 利息 {x["interest_credit"]:,.0f} ＝ <b>{amount(x["pnl"])}</b> 元。保證金收付是核帳依據，沒有當成獲利。</p>' for x in r['short_closes'])
    all_closed=[]
    for x in r['long_closes']:
        all_closed.append(f'<tr><td>{x["date"]}</td><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td class="num">{x["shares"]:,.0f}</td><td class="num">{x["entry_cash_out"]:,.2f}</td><td class="num">{x["proceeds"]:,.2f}</td><td class="num">{amount(x["pnl"])}</td></tr>')
    gap=''
    if r['unmatched']:
        gap='<p class="callout"><b>一筆成本缺口：</b>9/11 兆豐金（2886）賣出 1 股、淨收 50 元，對帳單沒有買入成本。該 50 元已列入收付核帳，但這筆損益暫不併入上述數字；所以這裡是「已配對成交」損益，不能宣稱整戶完整損益已全部核清。</p>'
    return f'''<style>#owner-account .account-metrics{{grid-template-columns:repeat(3,minmax(0,1fr));margin:18px 0}}#owner-account .metric-value{{font-size:clamp(20px,2.15vw,29px)}}#owner-account summary{{cursor:pointer;color:var(--gold);padding:10px 0}}@media(max-width:760px){{#owner-account .account-metrics{{grid-template-columns:1fr}}}}</style>
<article class="panel full" id="owner-account"><div class="eyebrow">券商成交對帳・已配對損益</div><h2>實際帳戶績效</h2><p class="sub">成交期間 {r['first_trade']} ～ {r['asof']} · 持股估值 {r['marks_asof']} 官方收盤 · 新版使用對帳單實際費用</p>
<div class="metrics account-metrics">{''.join(metrics)}</div>
<p>計算方式：多頭已實現 <b>{amount(r['long_realized'])}</b> ＋ 融券已實現 <b>{amount(r['short_realized'])}</b> ＝ <b>{amount(r['realized'])}</b> 元。持股成本 {r['open_cost']:,.2f} 元，收盤市值 {r['market_value']:,.2f} 元。</p>{gap}
<h3>最近平倉・金額都用實際收付</h3><div class="table-wrap"><table><thead><tr><th>股票</th><th>平倉日</th><th>成交狀態</th><th class="num">買入支出／保證金</th><th class="num">賣出淨收／返還款</th><th class="num">已實現損益</th></tr></thead><tbody>{''.join(recent)}</tbody></table></div>{short_detail}
<h3>目前持股・9 檔（融券剩餘 0 股）</h3><p class="sub">成本包含實際買入手續費。帳面損益＝股數 × 收盤價 − 買入支出；未扣未來賣出費稅，沒有加上未提供的股息或成本調整。</p>
<div class="table-wrap"><table><thead><tr><th>股票</th><th class="num">股數</th><th class="num">含費成本均價</th><th class="num">收盤價</th><th class="num">買入支出</th><th class="num">收盤市值</th><th class="num">未實現損益</th><th class="num">報酬率</th></tr></thead><tbody>{''.join(positions)}</tbody></table></div>
<p class="callout"><b>你的近期投信偏好：</b>本次投信進出暫不跟進。10/1 原卡的「6026 福邦證 出、3042 晶技 進」保留為來源訊號；對帳單沒有這兩筆成交，目前也沒有福邦證持股。不把訊號或這項偏好寫成成交。</p>
<details><summary>查看所有已配對多頭平倉與對帳方法</summary><div class="table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th class="num">股數</th><th class="num">FIFO 買入支出</th><th class="num">實際賣出淨收</th><th class="num">損益</th></tr></thead><tbody>{''.join(all_closed)}</tbody></table></div>
<p>59 筆成交逐筆核對，總手續費 {r['totals']['fee']:,.0f} 元、交易稅 {r['totals']['tax']:,.0f} 元、借券費 {r['totals']['borrow_fee']:,.0f} 元、利息 {r['totals']['interest']:,.0f} 元。費用總額含未平倉買入與缺成本那一筆，不能全部再扣一次。</p>
<p>應收 {r['totals']['receivable']:,.0f} − 應付 {r['totals']['payable']:,.0f} ＝ 收付變動 {r['cash_change']:,.0f} 元；加回未平倉成本 {r['open_cost']:,.0f}，扣未配對收款 {r['unmatched_receipts']:,.0f}，得到已實現 {r['realized']:,.0f} 元。保證金、擔保品會在融券開倉／回補重複列示，不當成收入加總。</p>
<p><a href="intake/">59 筆去識別對帳單、CSV 與驗算資料</a>。原始姓名與帳號未公開。下方四策略曲線使用其歷史分配，排除融券及兆豐金測試單，並含假設賣出費稅，因此與本區口徑不同。</p></details></article>'''

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
    if (root / 'inputs/broker_statement_fills.csv').exists():
        import statement_account
        return statement_account.load_account(root)
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
    if 'statement_rows' in r:
        return render_statement(r)
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
