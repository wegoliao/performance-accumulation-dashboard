"""Broker PnL screens govern performance; settlement cash remains independent.

Screens are manually reviewed safe projections. Never publish raw account images
or infer dividends/corporate actions from a broker-versus-FIFO cost difference.
"""
import html
import json
from decimal import Decimal as D
from pathlib import Path
import statement_account as s

REALIZED_FIELDS={'trade_date','stock_code','stock_name','type','shares','price',
                 'buy_amount','sell_amount','pnl','order_id','included','exclusion_reason'}
POSITION_FIELDS={'stock_code','stock_name','shares','cost','quote','reported_value','pnl'}

def validate_snapshot(realized,positions,receipt,statement):
    if receipt['realized_period_end']<statement['asof']:
        raise ValueError('broker PnL screen predates latest statement fill')
    if receipt['realized_period_start']>statement['first_trade']:
        raise ValueError('broker PnL screen period omits statement trades')
    parsed=[];seen=set()
    for row in realized:
        if set(row)!=REALIZED_FIELDS:raise ValueError('unsafe realized columns')
        r={**row,**{k:D(row[k]) for k in ['shares','price','buy_amount','sell_amount','pnl']}}
        key=(r['trade_date'],r['order_id'],r['stock_code'],r['shares'],r['price'])
        if key in seen:raise ValueError('duplicate broker realized row')
        seen.add(key)
        if any(not r[k].is_finite() for k in ['shares','price','buy_amount','sell_amount','pnl']):
            raise ValueError('invalid broker number')
        if r['shares']<=0 or r['shares']!=int(r['shares']):raise ValueError('invalid broker quantity')
        if r['type'] not in ('現股','融券'):raise ValueError('unsupported broker position type')
        expected=r['sell_amount']-r['buy_amount'] if r['type']=='現股' else r['buy_amount']-r['sell_amount']
        if expected!=r['pnl']:raise ValueError('broker realized cash direction mismatch')
        if r['included'] not in ('true','false'):raise ValueError('invalid inclusion flag')
        if (r['included']=='false')!=(r['stock_code'] in receipt['excluded_stock_codes']):
            raise ValueError('owner exclusion policy mismatch')
        parsed.append(r)
    def totals(rows,expected,fields):
        if len(rows)!=expected['rows']:raise ValueError('broker row subtotal mismatch')
        if any(sum((r[k] for r in rows),D(0))!=D(expected[k]) for k in fields):
            raise ValueError('broker subtotal mismatch')
    totals(parsed,receipt['realized_totals'],['shares','buy_amount','sell_amount','pnl'])
    held={}
    for row in positions:
        if set(row)!=POSITION_FIELDS:raise ValueError('unsafe position columns')
        r={**row,**{k:D(row[k]) for k in ['shares','cost','quote','reported_value','pnl']}}
        c=r['stock_code']
        if c in held:raise ValueError('duplicate broker holding')
        if r['shares']!=statement['positions'].get(c):raise ValueError('broker position quantity mismatch')
        if r['cost']!=statement['costs'][c]:raise ValueError('broker open cost differs; reconciliation needed')
        if r['reported_value']-r['cost']!=r['pnl']:raise ValueError('broker position identity mismatch')
        if any(not r[k].is_finite() for k in ['shares','cost','quote','reported_value','pnl']) or r['quote']<=0:
            raise ValueError('invalid broker position number')
        held[c]=r
    if set(held)!=set(statement['positions']):raise ValueError('broker holdings coverage mismatch')
    totals(list(held.values()),receipt['unrealized_totals'],['shares','cost','reported_value','pnl'])
    # Screens print 2025 while the original HTML prints 2026. Preserve both.
    # Match exact unique order ID/code/shares/price, never silently change dates.
    expected_closes={(x['order_id'],x['stock_code'],x['shares'],x['price']) for x in statement['long_closes']}
    if len(expected_closes)!=len(statement['long_closes']):raise ValueError('ambiguous statement close identity')
    actual_closes=set();longs=[];shorts=[];differences=[];date_conflicts=[]
    for x in parsed:
        if x['included']=='false':continue
        if x['type']=='融券':
            candidates=[a for a in statement['short_closes'] if (a['order_id'],a['stock_code'],a['shares'],a['exit_price'])==
                        (x['order_id'],x['stock_code'],x['shares'],x['price'])]
            if len(candidates)!=1:raise ValueError('broker short coverage mismatch')
            a=candidates[0]
            if (x['buy_amount'],x['sell_amount'],x['pnl'])!=(a['released_cash'],a['margin_paid'],a['pnl']):
                raise ValueError('broker short cash mismatch')
            if x['trade_date']!=a['date']:date_conflicts.append({'order_id':x['order_id'],'stock_code':x['stock_code'],'broker_date':x['trade_date'],'statement_date':a['date']})
            shorts.append({**a,'statement_date':a['date'],'date':x['trade_date']});continue
        key=(x['order_id'],x['stock_code'],x['shares'],x['price'])
        if key not in expected_closes:raise ValueError('unmatched broker realized trade')
        actual_closes.add(key)
        old=next(a for a in statement['long_closes'] if (a['order_id'],a['stock_code'],a['shares'],a['price'])==key)
        if x['sell_amount']!=old['proceeds']:raise ValueError('broker sale net proceeds mismatch')
        if x['trade_date']!=old['date']:date_conflicts.append({'order_id':x['order_id'],'stock_code':x['stock_code'],'broker_date':x['trade_date'],'statement_date':old['date']})
        new={**old,'order_id':x['order_id'],'statement_date':old['date'],'date':x['trade_date'],'entry_cash_out':x['buy_amount'],'pnl':x['pnl'],
             'cost_source':'BROKER_REALIZED_REPORT'}
        longs.append(new)
        delta=(x['pnl']-old['pnl']).quantize(D('.01'))
        if abs(delta)>D('.005'):
            differences.append({'date':x['trade_date'],'stock_code':x['stock_code'],
                                'stock_name':x['stock_name'],'shares':x['shares'],
                                'statement_cost':old['entry_cash_out'],'broker_cost':x['buy_amount'],
                                'delta':delta,'reason':'SOURCE_COST_DIFFERENCE_CAUSE_NOT_PROVIDED'})
    if actual_closes!=expected_closes or len(shorts)!=len(statement['short_closes']):
        raise ValueError('broker closed trade coverage mismatch')
    if date_conflicts and receipt.get('date_match_policy')!='EXACT_ORDER_CODE_QUANTITY_PRICE_PRESERVE_SOURCE_DATES':
        raise ValueError('source date conflicts need explicit preservation policy')
    return {'receipt':receipt,'rows':parsed,'held':held,'longs':longs,'shorts':shorts,'date_conflicts':date_conflicts,
            'raw_realized':sum(x['pnl'] for x in parsed),
            'excluded_realized':sum(x['pnl'] for x in parsed if x['included']=='false'),
            'cost_differences':differences}

def apply(root,statement):
    path=root/'inputs/broker_pnl_snapshot.json'
    if not path.exists():return statement
    receipt=json.loads(path.read_text(encoding='utf-8'))
    for key in ('realized_file','positions_file'):
        if Path(receipt[key]).name!=receipt[key]:raise ValueError('unsafe snapshot source path')
    b=validate_snapshot(s.read(root/'inputs'/receipt['realized_file']),
                        s.read(root/'inputs'/receipt['positions_file']),receipt,statement)
    r=dict(statement);r['statement_reference']=statement;r['broker_snapshot']=b
    r['asof']=receipt['realized_period_end'];r['first_trade']=receipt['realized_period_start']
    r['long_closes']=b['longs'];r['short_closes']=b['shorts']
    r['long_realized']=sum(x['pnl'] for x in b['longs'])
    r['realized']=b['raw_realized']-b['excluded_realized']
    r['position_values']={c:x['reported_value'] for c,x in b['held'].items()}
    r['position_pnl']={c:x['pnl'] for c,x in b['held'].items()}
    r['market_value']=sum(r['position_values'].values());r['unrealized']=sum(r['position_pnl'].values())
    r['combined']=r['realized']+r['unrealized'];r['marks_asof']=receipt['data_asof']
    r['marks']={c:{'date':receipt['data_asof'],'close':x['quote'],'source':receipt['quote_basis']} for c,x in b['held'].items()}
    r['gross_market_value']=sum(r['positions'][c]*r['marks'][c]['close'] for c in r['positions'])
    r['market_value_basis']='BROKER_REPORTED_NET_ESTIMATE'
    r['performance_basis']='OWNER_SELECTED_BROKER_PNL_REPORT'
    r['cash_bridge_basis']='STATEMENT_REFERENCE_ONLY_NOT_PRIMARY_PNL'
    r['exact_net_status']='BROKER_REPORTED_REALIZED_AND_UNREALIZED_ESTIMATE'
    r['coverage_status']='BROKER_REPORT_RECONCILED_EXCLUDING_OWNER_TEST_TRADE'
    r['excluded_trades']=[x for x in b['rows'] if x['included']=='false'];r['unmatched']=[]
    return r

def amount(x):
    cls='negative' if x<0 else 'positive' if x>0 else 'neutral'
    return f'<span class="{cls}">{x:+,.2f}</span>'

LABELS={'TRUST':'投信','YOY':'YOY','MARGIN':'融資','BREAKOUT':'突破'}
COMMISSION=D('0.001425');SELL_TAX=D('0.003')

def strategy_status(root):
    """Owner strategy focus from owner_preferences: FOLLOW, STOPPED or absent."""
    path=root/'inputs/owner_preferences.csv'
    out={}
    if not path.exists():return out
    for p in sorted(s.read(path),key=lambda p:p['recorded_date']):
        if p['status']=='OWNER_FOCUS_STRATEGY':out[p['strategy_id']]='FOLLOW'
        elif p['status']=='OWNER_STOPPED_PARTICIPATING':out[p['strategy_id']]='STOPPED'
    return out

def sell_cost(gross):
    gross=D(int(gross))
    return max(D(int(gross*COMMISSION)),D(1))+D(int(gross*SELL_TAX))

def apply_trade_detail(root,r):
    """Roll the broker screen forward with later broker trade-detail orders.

    Fees/tax of these orders are the statement-verified formula estimate
    carried in actual_fills; quantities and prices are the broker's own.
    Remaining holdings are valued at the latest common official close with
    the same estimated sell fee/tax the broker screen deducts.
    """
    from collections import deque
    since=r['broker_snapshot']['receipt']['data_asof']
    fills=s.read(root/'inputs/actual_fills.csv')
    rows=[x for x in fills if x['fill_date']>since]
    if not rows:return r
    if any('broker_trade_detail' not in x['source'] for x in rows):
        raise ValueError('post-screen fill without broker trade-detail source')
    lots={c:deque([[x['shares'],x['cost']]]) for c,x in r['broker_snapshot']['held'].items()}
    names={c:x['stock_name'] for c,x in r['broker_snapshot']['held'].items()}
    trades=[];closes=[]
    for x in sorted(rows,key=lambda x:(x['fill_date'],x['trade_id'])):
        c=x['stock_code'];q=D(x['shares']);names[c]=x['stock_name']
        trades.append({**x,'q':q,'cash':D(x['cash_out_twd']) if x['side']=='BUY' else D(x['cash_in_twd'])})
        if x['side']=='BUY':
            lots.setdefault(c,deque()).append([q,D(x['cash_out_twd'])]);continue
        remaining=q;basis=D(0)
        while remaining:
            if not lots.get(c):raise ValueError(f'{c} post-screen sell exceeds holdings')
            lot=lots[c][0];take=min(remaining,lot[0])
            cost=lot[1] if take==lot[0] else lot[1]*take/lot[0]
            basis+=cost;lot[1]-=cost;lot[0]-=take;remaining-=take
            if not lot[0]:lots[c].popleft()
        proceeds=D(x['cash_in_twd'])
        closes.append({'date':x['fill_date'],'stock_code':c,'stock_name':x['stock_name'],'strategy_id':x['strategy_id'],
                       'shares':q,'price':D(x['fill_price']),'proceeds':proceeds,'basis':basis,'pnl':proceeds-basis})
    positions={c:sum((l[0] for l in v),D(0)) for c,v in lots.items()}
    positions={c:q for c,q in positions.items() if q}
    costs={c:sum((l[1] for l in lots[c]),D(0)) for c in positions}
    prices={}
    for p in s.read(root/'inputs/price_history.csv'):
        if p['stock_code'] in positions:prices.setdefault(p['stock_code'],{})[p['asof_date']]=p
    last_fill=max(x['fill_date'] for x in rows)
    common=set.intersection(*(set(prices.get(c,{})) for c in positions))
    eligible=[d for d in common if d>=last_fill]
    if not eligible:raise ValueError('no common official close on or after latest trade')
    day=max(eligible)
    sleeves={}
    for x in fills:
        k=(x['strategy_id'],x['stock_code'])
        sleeves[k]=sleeves.get(k,D(0))+(D(x['shares']) if x['side']=='BUY' else -D(x['shares']))
    held={}
    for c,q in positions.items():
        close=D(prices[c][day]['close']);gross=q*close;est=sell_cost(gross)
        owner=sorted(sid for (sid,code),n in sleeves.items() if code==c and n>0)
        held[c]={'stock_code':c,'stock_name':names[c],'shares':q,'cost':costs[c],'close':close,
                 'gross':gross,'est_sell_cost':est,'value':gross-est,'pnl':gross-est-costs[c],'strategies':owner}
    new_realized=sum((x['pnl'] for x in closes),D(0))
    cur={'since':since,'last_trade':last_fill,'marks_asof':day,'trades':trades,'closes':closes,'held':held,
         'snapshot_realized':r['realized'],'new_realized':new_realized,'realized':r['realized']+new_realized,
         'open_cost':sum(costs.values(),D(0)),'market_value':sum((x['value'] for x in held.values()),D(0)),
         'status':strategy_status(root)}
    cur['unrealized']=cur['market_value']-cur['open_cost'];cur['combined']=cur['realized']+cur['unrealized']
    r=dict(r);r['current']=cur
    ref=dict(r['statement_reference'])
    ref.update(positions=positions,costs=costs,names={**ref['names'],**names},marks_asof=day,
               marks={c:{'date':day,'close':held[c]['close'],'source':prices[c][day]['source']} for c in positions})
    r['statement_reference']=ref
    return r

def view(r):
    """Headline numbers: rolled forward when later trades exist, else the screen."""
    return r.get('current') or {'realized':r['realized'],'unrealized':r['unrealized'],
                                'combined':r['combined'],'marks_asof':r['marks_asof']}

def summary(r):
    if 'broker_snapshot' not in r:return ''
    v=view(r)
    labels=[('累積已實現損益','realized','券商 10/2 已實現報表，加上之後新平倉（費稅依公式估算）。'),
            ('目前持股未實現損益','unrealized','官方收盤 × 股數 − 預估賣出費稅 − 成本；尚未賣出。'),
            ('已實現 ＋ 未實現','combined','兩項相加；不是帳戶餘額，也不是已變現金額。')]
    return '<div class="metrics account-metrics">'+''.join(f'<div class="metric-card"><div class="metric-label">{title}</div><div class="metric-value">NT$ {amount(v[key])}</div><p class="metric-note">{note}</p></div>' for title,key,note in labels)+'</div>'

def notice(root):
    import owner_account
    r=owner_account.load_account(root)
    if not r or 'broker_snapshot' not in r:return ''
    v=view(r);n=len(v.get('held',r['broker_snapshot']['held']))
    return f'<div class="notice"><b>實際帳務 {v["marks_asof"]}</b>：累積已實現 {amount(v["realized"])} 元 · 目前未實現試算 {amount(v["unrealized"])} 元 · 合計 {amount(v["combined"])} 元。未實現已含預估賣出費稅；10/2 之後成交的費稅為公式估算，待對帳單確認。<a href="../#owner-account">查看 {n} 檔明細</a></div>'

def render(r):
    h=html.escape;b=r['broker_snapshot'];reference=r['statement_reference']
    v=r.get('current')
    if not v:raise ValueError('render expects a rolled-forward account')
    status=v['status']
    def tag(sids):
        out=[]
        for sid in sids:
            st=status.get(sid,'')
            out.append(LABELS.get(sid,sid)+('・10/13 起不跟・<b>待擇機清倉</b>' if st=='STOPPED' else '・追蹤中' if st=='FOLLOW' else ''))
        return '、'.join(out) or '—'
    held=[]
    for c,x in sorted(v['held'].items()):
        held.append(f'<tr><td>{h(c)} {h(x["stock_name"])}</td><td>{tag(x["strategies"])}</td><td class="num">{x["shares"]:,.0f}</td><td class="num">{x["close"]:,.2f}</td><td class="num">{x["cost"]:,.0f}</td><td class="num">{x["value"]:,.0f}</td><td class="num">{amount(x["pnl"])}</td><td class="num">{amount(x["pnl"]/x["cost"]*100)}%</td></tr>')
    held.append(f'<tr><th colspan="4">{len(v["held"])} 檔合計</th><th class="num">{v["open_cost"]:,.0f}</th><th class="num">{v["market_value"]:,.0f}</th><th class="num">{amount(v["unrealized"])}</th><th></th></tr>')
    trades=[]
    for x in v['trades']:
        kind='現買' if x['side']=='BUY' else '現賣'
        trades.append(f'<tr><td>{x["fill_date"]}</td><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td>{LABELS.get(x["strategy_id"],"")}</td><td>{kind}</td><td class="num">{x["q"]:,.0f}</td><td class="num">{D(x["fill_price"]):,.2f}</td><td class="num">{D(x["fee_twd"]):,.0f}</td><td class="num">{D(x["tax_twd"]):,.0f}</td><td class="num">{x["cash"]:,.0f}</td></tr>')
    closes=[]
    for x in v['closes']:
        closes.append(f'<tr><td>{x["date"]}</td><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td>{LABELS.get(x["strategy_id"],"")}</td><td class="num">{x["shares"]:,.0f}</td><td class="num">{x["basis"]:,.0f}</td><td class="num">{x["proceeds"]:,.0f}</td><td class="num">{amount(x["pnl"])}</td></tr>')
    closed=[]
    for x in sorted(b['rows'],key=lambda x:(x['trade_date'],x['stock_code'],x['shares'])):
        if x['included']=='false':continue
        kind='券買・已回補' if x['type']=='融券' else '現賣・已平倉'
        closed.append(f'<tr><td>{x["trade_date"]}</td><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td>{kind}</td><td class="num">{x["shares"]:,.0f}</td><td class="num">{x["buy_amount"]:,.0f}</td><td class="num">{x["sell_amount"]:,.0f}</td><td class="num">{amount(x["pnl"])}</td></tr>')
    diffs=[]
    for x in b['cost_differences']:
        diffs.append(f'<tr><td>{x["date"]}</td><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td class="num">{x["shares"]:,.0f}</td><td class="num">{x["statement_cost"]:,.2f}</td><td class="num">{x["broker_cost"]:,.2f}</td><td class="num">{amount(x["delta"])}</td></tr>')
    delta=r['realized']-reference['realized']
    follow='、'.join(LABELS[k] for k,st in status.items() if st=='FOLLOW') or '—'
    stopped='、'.join(LABELS[k] for k,st in status.items() if st=='STOPPED')
    stop_held=[f'{c} {x["stock_name"]}' for c,x in sorted(v['held'].items()) if x['strategies'] and all(status.get(t)=='STOPPED' for t in x['strategies'])]
    focus=''
    if stopped:
        rest=f'剩餘待擇機清倉：<b>{h("、".join(stop_held))}</b>。' if stop_held else f'{stopped} 目前已無持股。'
        focus=f'<p class="callout"><b>策略註記（Owner 10/12）：</b>只追強的策略 <b>{follow}</b>；{stopped} <b>從 10/13 開始不跟（測試）</b>，卡片照常放上並標明，但不再列入待進場。括號（反向）與 * 特殊標的目前也不跟。{rest}</p>'
    style=('<style>#owner-account .account-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:18px 0}'
           '#owner-account .metric-value{font-size:clamp(20px,2.15vw,29px)}#owner-account summary{cursor:pointer;color:var(--gold);padding:10px 0}'
           '@media(max-width:760px){#owner-account .account-metrics{grid-template-columns:1fr}}</style>')
    return (style+
        f'<article class="panel full" id="owner-account"><div class="eyebrow">券商 10/2 損益報表 ＋ 永豐成交明細至 {v["last_trade"]}</div><h2>實際帳戶績效</h2>'
        f'<p class="sub">已實現查詢 {r["first_trade"]} ～ {v["last_trade"]} · 持股估值 {v["marks_asof"]} 官方收盤。10/2 之後的成交依永豐成交明細逐筆對齊；手續費與交易稅依過去對帳單驗證過的公式估算，待下一份對帳單確認。</p>'
        f'{summary(r)}{focus}<p>10/2 券商已實現 {amount(v["snapshot_realized"])} ＋ 之後新平倉 {amount(v["new_realized"])} ＝ <b>{amount(v["realized"])}</b> 元。</p>'
        f'<p class="callout"><b>蔚華科已回補，實際虧損 {amount(r["short_realized"])} 元。</b>已含實際費稅、借券費與利息；融券剩餘 0 股。</p>'
        f'<h3>目前持股・{len(v["held"])} 檔</h3><p class="sub">成本＝券商成本（含買入手續費）；10/2 後新買入加計估算手續費，同股票先進先出。現值＝{v["marks_asof"]} 官方收盤 × 股數 − 預估賣出手續費與交易稅。</p>'
        '<div class="table-wrap"><table><thead><tr><th>股票</th><th>策略</th><th class="num">股數</th><th class="num">官方收盤</th><th class="num">成本</th><th class="num">現值（淨估）</th><th class="num">未實現損益</th><th class="num">報酬率</th></tr></thead>'
        f'<tbody>{"".join(held)}</tbody></table></div>'
        '<h3>10/2 之後新平倉</h3><div class="table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th>策略</th><th class="num">股數</th><th class="num">成本</th><th class="num">賣出淨收（估）</th><th class="num">已實現</th></tr></thead>'
        f'<tbody>{"".join(closes)}</tbody></table></div>'
        f'<details><summary>查看 10/2 之後 {len(v["trades"])} 筆成交（永豐成交明細）</summary><p>同委託書號、同價格的分筆成交已合併。成交金額取整後估算手續費 0.1425%、賣出交易稅 0.3%；此公式與過去對帳單每一筆現股收付一致，但當沖減稅、折讓等仍以對帳單為準。</p>'
        '<div class="table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th>策略</th><th>買賣</th><th class="num">股數</th><th class="num">成交價</th><th class="num">手續費（估）</th><th class="num">交易稅（估）</th><th class="num">收付（估）</th></tr></thead>'
        f'<tbody>{"".join(trades)}</tbody></table></div></details>'
        f'<details><summary>查看 10/2 券商報表 {len(closed)} 筆已實現明細</summary><p>券商買進／賣出金額保留原畫面。現股為賣出−買進；融券為返還款−原繳保證金，不把保證金當獲利。<b>來源年份待核對：</b>已實現畫面印 2025、HTML 對帳單印 2026，兩者日期都保存。</p>'
        '<div class="table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th>狀態</th><th class="num">股數</th><th class="num">券商買進欄</th><th class="num">券商賣出欄</th><th class="num">已實現損益</th></tr></thead>'
        f'<tbody>{"".join(closed)}</tbody></table></div></details>'
        f'<details><summary>為什麼券商已實現與成交單 FIFO 差 {delta:,.0f} 元？</summary><p>成交對帳單 FIFO 實付重建已實現 {amount(reference["realized"])} 元；券商已實現報表配對成本得 {amount(r["realized"])} 元，差 {amount(delta)} 元。賣出股數與淨收款一致，差額來自成本欄；<b>原因未提供，不推測配息或其他調整。</b></p>'
        '<div class="table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th class="num">股數</th><th class="num">成交單 FIFO 成本</th><th class="num">券商損益報表成本</th><th class="num">損益差額</th></tr></thead>'
        f'<tbody>{"".join(diffs)}</tbody></table></div></details>'
        '<p><a href="intake/">下載去識別損益明細、完整成交與來源收據</a>。原始截圖與成交明細含帳號姓名，只存本機，不公開。</p></article>')
