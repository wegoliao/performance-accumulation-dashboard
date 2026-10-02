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

def summary(r):
    if 'broker_snapshot' not in r:return ''
    labels=[('累積已實現損益','realized','券商已實現報表，排除兆豐金測試買賣。'),
            ('目前持股未實現損益','unrealized','券商損益試算；含預估賣出費稅，尚未賣出。'),
            ('已實現 ＋ 未實現','combined','兩項相加；不是帳戶餘額，也不是已變現金額。')]
    return '<div class="metrics account-metrics">'+''.join(f'<div class="metric-card"><div class="metric-label">{title}</div><div class="metric-value">NT$ {amount(r[key])}</div><p class="metric-note">{note}</p></div>' for title,key,note in labels)+'</div>'

def notice(root):
    import owner_account
    r=owner_account.load_account(root)
    if not r or 'broker_snapshot' not in r:return ''
    return f'<div class="notice"><b>券商損益快照 {r["marks_asof"]}</b>：累積已實現 {amount(r["realized"])} 元 · 目前未實現試算 {amount(r["unrealized"])} 元 · 合計 {amount(r["combined"])} 元。兆豐金測試買賣不計；未實現已含預估賣出費稅。<br>券商盤中截圖的精確報價時間未提供；以下技術量測仍使用另標日期的官方收盤。<a href="../#owner-account">查看 9 檔明細及來源年份差異</a></div>'

def render(r):
    h=html.escape;b=r['broker_snapshot'];receipt=b['receipt'];reference=r['statement_reference']
    held=[]
    for c,x in sorted(b['held'].items()):
        held.append(f'<tr><td>{h(c)} {h(x["stock_name"])}</td><td class="num">{x["shares"]:,.0f}</td><td class="num">{x["quote"]:,.2f}</td><td class="num">{x["cost"]:,.0f}</td><td class="num">{x["reported_value"]:,.0f}</td><td class="num">{amount(x["pnl"])}</td><td class="num">{amount(x["pnl"]/x["cost"]*100)}%</td></tr>')
    held.append(f'<tr><th colspan="3">9 檔合計</th><th class="num">{r["open_cost"]:,.0f}</th><th class="num">{r["market_value"]:,.0f}</th><th class="num">{amount(r["unrealized"])}</th><th></th></tr>')
    closed=[]
    for x in sorted(b['rows'],key=lambda x:(x['trade_date'],x['stock_code'],x['shares'])):
        if x['included']=='false':continue
        kind='券買・已回補' if x['type']=='融券' else '現賣・已平倉'
        closed.append(f'<tr><td>{x["trade_date"]}</td><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td>{kind}</td><td class="num">{x["shares"]:,.0f}</td><td class="num">{x["buy_amount"]:,.0f}</td><td class="num">{x["sell_amount"]:,.0f}</td><td class="num">{amount(x["pnl"])}</td></tr>')
    diffs=[]
    for x in b['cost_differences']:
        diffs.append(f'<tr><td>{x["date"]}</td><td>{h(x["stock_code"])} {h(x["stock_name"])}</td><td class="num">{x["shares"]:,.0f}</td><td class="num">{x["statement_cost"]:,.2f}</td><td class="num">{x["broker_cost"]:,.2f}</td><td class="num">{amount(x["delta"])}</td></tr>')
    delta=r['realized']-reference['realized']
    return f'''<style>#owner-account .account-metrics{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:18px 0}}#owner-account .metric-value{{font-size:clamp(20px,2.15vw,29px)}}#owner-account summary{{cursor:pointer;color:var(--gold);padding:10px 0}}@media(max-width:760px){{#owner-account .account-metrics{{grid-template-columns:1fr}}}}</style>
<article class="panel full" id="owner-account"><div class="eyebrow">券商損益報表・兆豐金測試交易已排除</div><h2>實際帳戶績效</h2><p class="sub">已實現查詢 {r['first_trade']} ～ {r['asof']} · 目前持股依 Owner 提供的 10/2 盤中截圖，精確報價時間未提供。這是券商畫面快照，重新整理網站不會即時更新報價。</p>
{summary(r)}<p>券商原始已實現小計 {amount(b['raw_realized'])}，扣掉兆豐金測試損益 {amount(b['excluded_realized'])}，得到 <b>{amount(r['realized'])}</b> 元。兆豐金的買賣、損益均不計入績效。</p>
<p class="callout"><b>蔚華科已回補，實際虧損 {amount(r['short_realized'])} 元。</b>已含實際費稅、借券費與利息；融券剩餘 0 股。其他現股已實現 {amount(r['long_realized'])} 元。</p>
<h3>目前持股・9 檔・券商損益試算</h3><p class="sub">成本合計 {r['open_cost']:,.0f} 元，券商現值 {r['market_value']:,.0f} 元，未實現 {amount(r['unrealized'])} 元。直接採畫面「現值」與「損益試算」，含預估賣出費稅；不可再扣一次。現價是截圖報價，並非官方收盤或成交保證。</p>
<div class="table-wrap"><table><thead><tr><th>股票</th><th class="num">股數</th><th class="num">截圖現價</th><th class="num">券商成本</th><th class="num">券商現值（淨估）</th><th class="num">未實現損益</th><th class="num">報酬率</th></tr></thead><tbody>{''.join(held)}</tbody></table></div>
<p class="callout"><b>本次投信偏好：</b>暫不跟進。原策略訊號保留，沒有實際成交就不產生成交。</p>
<p class="sub"><b>來源年份待核對：</b>本次已實現畫面成交日印為 2025，先前 HTML 對帳單印為 2026。21 筆按委託單號、股票、股數、價格與淨收付核對，採本次損益金額；兩個來源日期都保存，尚未判定正確年份。</p>
<details><summary>查看 21 筆已實現明細（不含兆豐金測試單）</summary><p>券商買進／賣出金額保留原畫面。現股為賣出−買進；融券為返還款−原繳保證金，不把保證金當獲利。</p><div class="table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th>狀態</th><th class="num">股數</th><th class="num">券商買進欄</th><th class="num">券商賣出欄</th><th class="num">已實現損益</th></tr></thead><tbody>{''.join(closed)}</tbody></table></div></details>
<details><summary>為什麼已實現與前版差 {delta:,.0f} 元？查看來源成本差額</summary><p>前版依完整成交對帳單 FIFO 實付重建，已實現 {amount(reference['realized'])} 元；本版依券商已實現報表的配對成本，已實現 {amount(r['realized'])} 元，差 {amount(delta)} 元。兩者賣出股數與淨收款一致，差額来自以下成本欄。<b>券商成本差額原因未提供，不推測配息或其他調整。</b></p>
<div class="table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th class="num">股數</th><th class="num">成交單 FIFO 成本</th><th class="num">券商損益報表成本</th><th class="num">損益差額</th></tr></thead><tbody>{''.join(diffs)}</tbody></table></div>
<p>成交對帳單的應收應付仍獨立核帳；此次未改寫成交現金，也未把 {delta:,.0f} 元當新增交易或現金收入。四策略曲線仍按原分配及官方收盤重播，其日期與成本口徑不同，另行標示。</p></details>
<p><a href="intake/">下載去識別損益明細、完整成交與來源收據</a>。原始截圖含帳號姓名，只存本機，不公開。</p></article>'''
