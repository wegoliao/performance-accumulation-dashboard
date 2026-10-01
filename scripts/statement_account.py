"""Authoritative settlement cash, independent of card returns and broker snapshots.

HTML import projects an explicit safe field allowlist. No account/name column is
read into an artifact. Source totals and every cash identity must reconcile.
FIFO is by stock; existing sleeve allocations are preserved in actual_fills.
"""
import csv
import hashlib
import json
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal as D, ROUND_DOWN
from html.parser import HTMLParser
from pathlib import Path

FIELDS = {'trade_date':'成交日','type':'交易別','stock_code':'股票代碼',
          'stock_name':'股票名稱','shares':'數量','price':'單價','gross':'價金',
          'financing':'融資金額','collateral':'擔保品','margin':'保證金',
          'fee':'手續費','tax':'交易稅','interest':'利息','borrow_fee':'借券費',
          'payable':'應付額','receivable':'應收額','order_id':'委託單號','currency':'幣別'}
NUMBERS = ['shares','price','gross','financing','collateral','margin','fee','tax',
           'interest','borrow_fee','payable','receivable']
TOTAL_FIELDS = [k for k in NUMBERS if k != 'price']

def read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def write(path, rows, fields=None):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields or list(rows[0]),lineterminator='\n')
        w.writeheader();w.writerows(rows)

class Table(HTMLParser):
    def __init__(self):
        super().__init__();self.rows=[];self.row=None;self.cell=None
    def handle_starttag(self, tag, attrs):
        if tag=='tr':self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(''.join(self.cell).strip());self.cell=None
        elif tag=='tr' and self.row is not None:
            self.rows.append(self.row);self.row=None

def numeric(row):
    result=dict(row)
    for k in NUMBERS:
        value=D(str(row[k]).replace(',',''))
        if not value.is_finite() or value < 0:raise ValueError('invalid statement number')
        result[k]=value
    return result

def validate_rows(rows):
    seen=set();values=[]
    for raw in rows:
        r=numeric(raw)
        key=(r['trade_date'],r['order_id'],r['stock_code'],r['type'],r['price'],r['shares'])
        if key in seen:raise ValueError('duplicate physical statement fill')
        seen.add(key)
        if r['currency'] not in ('台幣','TWD'):raise ValueError('unsupported statement currency')
        if r['shares']<=0 or r['shares']!=int(r['shares']):raise ValueError('shares must be positive integers')
        # Broker truncates price * quantity to integer settlement gross.
        if not D(0)<=r['price']*r['shares']-r['gross']<D(1):raise ValueError('gross price reconciliation failed')
        if r['type']=='現買':
            valid=r['payable']==r['gross']+r['fee']+r['tax'] and r['receivable']==0
        elif r['type']=='現賣':
            valid=r['receivable']==r['gross']-r['fee']-r['tax'] and r['payable']==0
        elif r['type']=='券賣':
            valid=(r['collateral']==r['gross']-r['fee']-r['tax']-r['borrow_fee']
                   and r['payable']==r['margin'] and r['receivable']==0)
        elif r['type']=='券買':
            valid=(r['receivable']==r['margin']+r['collateral']-r['gross']-r['fee']+r['interest']-r['borrow_fee']
                   and r['payable']==0)
        else:raise ValueError('unsupported transaction type')
        if not valid:raise ValueError('statement cash identity failed')
        if r['financing']:raise ValueError('financed position requires separate accounting')
        values.append(r)
    return values

def import_html(source, root):
    data=source.read_bytes();table=Table();table.feed(data.decode('cp950'))
    header=next(r for r in table.rows if set(FIELDS.values())<=set(r))
    rows=[];total=None
    for cells in table.rows[table.rows.index(header)+1:]:
        if len(cells)!=len(header):continue
        raw=dict(zip(header,cells))
        safe={k:raw[v] for k,v in FIELDS.items()}
        if safe['type']:
            safe['trade_date']=datetime.strptime(safe['trade_date'],'%Y/%m/%d').date().isoformat()
            for k in NUMBERS:safe[k]=format(D(safe[k].replace(',','')),'f')
            rows.append(safe)
        elif safe['gross']:total={k:D(safe[k]) for k in TOTAL_FIELDS}
    values=validate_rows(rows)
    totals={k:sum((r[k] for r in values),D(0)) for k in TOTAL_FIELDS}
    if total is None or total!=totals:raise ValueError('statement grand totals mismatch')
    path=root/'inputs/broker_statement_fills.csv';write(path,rows)
    receipt={'source_label':source.name,'source_sha256':hashlib.sha256(data).hexdigest(),
             'computer_time':datetime.now().astimezone().isoformat(),
             'data_asof':max(r['trade_date'] for r in rows),'first_trade':min(r['trade_date'] for r in rows),
             'row_count':len(rows),'totals':{k:str(v) for k,v in totals.items()},
             'status':'PASS','safe_projection':list(FIELDS),'excluded_columns':['帳號','帳號名']}
    (root/'inputs/broker_statement_receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return receipt

def allocate(amount, quantities):
    """Allocate source money to historical sleeves at cents; remainder to last."""
    total=sum(quantities,D(0));values=[]
    for qty in quantities[:-1]:values.append((amount*qty/total).quantize(D('.01'),rounding=ROUND_DOWN))
    values.append(amount-sum(values,D(0)))
    return values

def reconcile_strategy_fills(root):
    old=read(root/'inputs/actual_fills.csv')
    statement=validate_rows(read(root/'inputs/broker_statement_fills.csv'))
    indexed=defaultdict(list)
    for r in old:
        key=(r['fill_date'],r['trade_id'].split('-2026')[0],r['stock_code'],r['side'],D(r['fill_price']))
        indexed[key].append(r)
    updated=[];changes=[];consumed=set()
    for r in statement:
        if r['type'] not in ('現買','現賣') or r['stock_code']=='2886':continue
        side='BUY' if r['type']=='現買' else 'SELL'
        key=(r['trade_date'],r['order_id'],r['stock_code'],side,r['price'])
        matches=indexed.get(key,[])
        if not matches:
            # New sale inherits existing bought sleeve; do not infer sleeve from card.
            sleeves={x['strategy_id'] for x in old if x['stock_code']==r['stock_code'] and x['side']=='BUY'}
            if side!='SELL' or len(sleeves)!=1:raise ValueError('unmapped strategy fill needs owner evidence')
            matches=[dict.fromkeys(old[0],'')]
            matches[0].update(trade_id=r['order_id']+'-'+r['trade_date'].replace('-',''),strategy_id=sleeves.pop(),
                             stock_code=r['stock_code'],stock_name=r['stock_name'],side=side,fill_date=r['trade_date'],
                             fill_price=str(r['price']),shares=str(r['shares']),currency='TWD')
        if sum(D(x['shares']) for x in matches)!=r['shares']:raise ValueError('strategy quantity mismatch')
        quantity=[D(x['shares']) for x in matches]
        amounts={out:allocate(r[src],quantity) for out,src in [('consideration_twd','gross'),('fee_twd','fee'),
                 ('tax_twd','tax'),('cash_out_twd','payable'),('cash_in_twd','receivable')]}
        for i,x in enumerate(matches):
            consumed.add(x['trade_id']);new=dict(x)
            for k,v in amounts.items():new[k]=format(v[i],'f')
            new['source']='broker_statement_2026-10-01;SHA256_11f75ebc;historical_sleeve_preserved;split_cash_proportional_shares'
            if new!=x:changes.append({'trade_id':x['trade_id'],'before':x,'after':new})
            updated.append(new)
    if any(x['trade_id'] not in consumed for x in old):raise ValueError('historical fill missing from statement')
    write(root/'inputs/actual_fills.csv',updated,old[0].keys())
    return changes

def load_account(root):
    rows=validate_rows(read(root/'inputs/broker_statement_fills.csv'))
    receipt=json.loads((root/'inputs/broker_statement_receipt.json').read_text(encoding='utf-8'))
    dates=[r['trade_date'] for r in rows]
    if dates!=sorted(dates):raise ValueError('statement transactions must be chronological')
    latest=max(dates)
    for path in (root/'inputs').glob('owner_reported_fills_*.csv'):
        if any(x['trade_date']>latest and x['status']!='ALREADY_RECORDED' for x in read(path)):
            raise ValueError('new owner fills after statement date require reconciliation')
    totals={k:sum((r[k] for r in rows),D(0)) for k in TOTAL_FIELDS}
    if {k:str(v) for k,v in totals.items()}!={k:str(D(v)) for k,v in receipt['totals'].items()}:
        # Numeric compare permits different harmless decimal serialization.
        if totals!={k:D(v) for k,v in receipt['totals'].items()}:raise ValueError('statement receipt totals mismatch')
    books=defaultdict(deque);shorts={};names={};closes=[];short_closes=[];unmatched=[]
    for r in rows:  # Source order retained: statement itself is chronological.
        c=r['stock_code'];names[c]=r['stock_name'];qty=r['shares']
        if r['type']=='現買':books[c].append([qty,r['payable'],r['trade_date']])
        elif r['type']=='現賣':
            held=sum((lot[0] for lot in books[c]),D(0))
            if held<qty:
                if held:raise ValueError('partially missing historical cost')
                unmatched.append({**r,'reason':'BUY_COST_NOT_IN_STATEMENT'});continue
            remaining=qty;basis=D(0)
            while remaining:
                lot=books[c][0];take=min(remaining,lot[0])
                cost=lot[1] if take==lot[0] else lot[1]*take/lot[0]
                basis+=cost;lot[1]-=cost;lot[0]-=take;remaining-=take
                if not lot[0]:books[c].popleft()
            closes.append({'stock_code':c,'stock_name':names[c],'date':r['trade_date'],'shares':qty,
                           'price':r['price'],'gross':r['gross'],'fees':r['fee'],'tax':r['tax'],
                           'entry_cash_out':basis,'proceeds':r['receivable'],'pnl':r['receivable']-basis})
        elif r['type']=='券賣':
            if c in shorts:raise ValueError('multiple short lots need explicit collateral allocation')
            shorts[c]=r
        elif r['type']=='券買':
            opened=shorts.pop(c,None)
            if not opened or qty!=opened['shares'] or r['collateral']!=opened['collateral'] or r['margin']!=opened['margin']:
                raise ValueError('short collateral/quantity mismatch')
            price_pnl=opened['gross']-r['gross'];fees=opened['fee']+r['fee'];tax=opened['tax']+r['tax']
            borrow=opened['borrow_fee']+r['borrow_fee'];interest=opened['interest']+r['interest']
            pnl=r['receivable']-opened['payable']
            if pnl!=price_pnl-fees-tax-borrow+interest:raise ValueError('short P&L cash bridge failed')
            short_closes.append({'stock_code':c,'stock_name':names[c],'date':r['trade_date'],
                                 'open_date':opened['trade_date'],'shares':qty,'entry_price':opened['price'],
                                 'exit_price':r['price'],'price_pnl':price_pnl,'fees':fees,'tax':tax,
                                 'borrow_fee':borrow,'interest_credit':interest,'margin_paid':opened['payable'],
                                 'released_cash':r['receivable'],'pnl':pnl})
    if shorts:raise ValueError('open shorts require a separately sourced current liability mark')
    positions={c:sum((x[0] for x in lots),D(0)) for c,lots in books.items() if lots}
    costs={c:sum((x[1] for x in books[c]),D(0)) for c in positions}
    prices=defaultdict(dict)
    for r in read(root/'inputs/price_history.csv'):
        if r['stock_code'] in positions:prices[r['stock_code']][r['asof_date']]=r
    common=set.intersection(*(set(prices[c]) for c in positions))
    eligible={d for d in common if d>=max(r['trade_date'] for r in rows)}
    if not eligible:raise ValueError('no common official marks after latest fill')
    day=max(eligible)
    marks={c:{'date':day,'close':D(prices[c][day]['close']),'source':prices[c][day]['source']} for c in positions}
    realized=sum((x['pnl'] for x in closes+short_closes),D(0)).quantize(D('.01'))
    market=sum((positions[c]*marks[c]['close'] for c in positions),D(0))
    open_cost=sum(costs.values(),D(0));unmatched_cash=sum((x['receivable'] for x in unmatched),D(0))
    cash_change=totals['receivable']-totals['payable']
    if cash_change+open_cost-unmatched_cash!=realized:raise ValueError('full account cash bridge failed')
    return {'asof':max(r['trade_date'] for r in rows),'first_trade':min(r['trade_date'] for r in rows),
            'statement_rows':len(rows),'totals':totals,'source_sha256':receipt['source_sha256'],
            'positions':positions,'costs':costs,'names':names,'marks':marks,'marks_asof':day,
            'long_closes':closes,'short_closes':short_closes,'unmatched':unmatched,
            'unmatched_receipts':unmatched_cash,'long_realized':sum((x['pnl'] for x in closes),D(0)).quantize(D('.01')),
            'short_realized':sum((x['pnl'] for x in short_closes),D(0)),'realized':realized,
            'cash_change':cash_change,'open_cost':open_cost,'market_value':market,'unrealized':market-open_cost,
            'combined':realized+market-open_cost,'short_remaining_shares':D(0),
            'exact_net_status':'EXACT_MATCHED_SETTLEMENTS','coverage_status':'MATCHED_TRADES_WITH_ONE_UNKNOWN_COST' if unmatched else 'COMPLETE',
            'settled_book_asof':max(r['trade_date'] for r in rows),
            'owner_preferences':read(root/'inputs/owner_preferences.csv') if (root/'inputs/owner_preferences.csv').exists() else []}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    print(json.dumps(import_html(args.source,root),ensure_ascii=False))
