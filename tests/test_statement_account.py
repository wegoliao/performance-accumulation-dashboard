"""Independent golden amounts from the owner's broker statement, not estimates."""
import csv
import sys
from decimal import Decimal as D
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

def report():
    import statement_account
    return statement_account.load_account(ROOT)

def test_settlement_totals_and_cash_bridge():
    r = report()
    assert r['statement_rows'] == 59
    assert r['totals']['gross'] == D('3941514')
    assert r['totals']['fee'] == D('5588')
    assert r['totals']['tax'] == D('4885')
    assert r['totals']['payable'] == D('2243555')
    assert r['totals']['receivable'] == D('1553321')
    assert r['cash_change'] == D('-690234')
    assert r['open_cost'] == D('675866')
    assert r['cash_change'] + r['open_cost'] - r['unmatched_receipts'] == r['realized']

def test_exact_realized_and_credit_interest_on_short():
    r = report()
    assert r['long_realized'] == D('56634')
    assert r['short_realized'] == D('-71052')
    assert r['realized'] == D('-14418')
    short = r['short_closes'][0]
    assert short['interest_credit'] == D('24')
    assert short['borrow_fee'] == D('117')
    assert short['fees'] == D('518')
    assert short['tax'] == D('441')
    assert short['margin_paid'] == D('147000')
    assert short['released_cash'] == D('75948')
    assert short['pnl'] == short['released_cash'] - short['margin_paid']

def test_integer_broker_cash_replaces_fractional_price_products():
    r = report()
    closes = {x['stock_code']: x for x in r['long_closes'] if x['date'] == '2026-10-01'}
    assert closes['2354']['proceeds'] == D('52014')
    assert closes['2354']['pnl'] == D('2458')
    assert closes['3231']['proceeds'] == D('95018')
    assert closes['3231']['pnl'] == D('2703')
    assert r['costs']['6108'] == D('50072')
    assert r['costs']['6179'] == D('48171')

def test_unknown_inherited_share_does_not_become_profit():
    r = report()
    assert r['unmatched_receipts'] == D('50')
    assert len(r['unmatched']) == 1
    assert r['unmatched'][0]['stock_code'] == '2886'
    assert r['unmatched'][0]['shares'] == D('1')
    assert r['coverage_status'] == 'MATCHED_TRADES_WITH_ONE_UNKNOWN_COST'

def test_open_quantity_and_marks_use_latest_common_official_day():
    r = report()
    assert len(r['positions']) == 9
    assert not {'2354','3231','3055','2886'} & r['positions'].keys()
    prices=list(csv.DictReader((ROOT/'inputs/price_history.csv').open(encoding='utf-8-sig')))
    common=set.intersection(*({x['asof_date'] for x in prices if x['stock_code']==c} for c in r['positions']))
    assert r['marks_asof']==max(d for d in common if d>=r['asof'])
    assert all(m['date'] == r['marks_asof'] for m in r['marks'].values())
    assert r['market_value'] == sum(q * r['marks'][c]['close'] for c,q in r['positions'].items())
    assert r['unrealized'] == r['market_value'] - D('675866')
    assert r['combined'] == r['realized'] + r['unrealized']

def test_csv_is_an_allowlist_without_account_or_personal_identifiers():
    with (ROOT/'inputs/broker_statement_fills.csv').open(encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        assert not {'帳號','帳號名','account','account_name'} & set(reader.fieldnames)
        assert len(list(reader)) == 59

def test_invalid_settlement_cash_fails_closed():
    import statement_account as s
    rows = s.read(ROOT/'inputs/broker_statement_fills.csv')
    rows[0]['payable'] = '0'
    with pytest.raises(ValueError, match='cash identity'):
        s.validate_rows(rows)

def test_duplicate_physical_fill_fails_closed():
    import statement_account as s
    rows = s.read(ROOT/'inputs/broker_statement_fills.csv')
    with pytest.raises(ValueError, match='duplicate'):
        s.validate_rows(rows + [rows[0]])

def test_strategy_cash_and_quantity_reconcile_to_statement():
    r = report()
    rows = list(csv.DictReader((ROOT/'inputs/actual_fills.csv').open(encoding='utf-8-sig')))
    cash = sum(D(x['cash_in_twd'])-D(x['cash_out_twd']) for x in rows)
    assert cash + r['open_cost'] == r['long_realized']
    assert len(rows) == 58

def test_preference_is_dated_and_does_not_change_original_signals():
    import owner_account
    assert owner_account.preference(ROOT,'TRUST','2026-10-01')=='Owner 本次暫不跟進'
    assert owner_account.preference(ROOT,'TRUST','2026-10-02')==''
    assert owner_account.preference(ROOT,'YOY','2026-10-01')==''
    signals=list(csv.DictReader((ROOT/'inputs/latest_strategy_signals.csv').open(encoding='utf-8-sig')))
    trust={r['stock_code']:r for r in signals if r['strategy_id']=='TRUST'}
    assert trust['6026']['signal']=='出' and trust['3042']['signal']=='進'
    assert trust['3042']['entry_price']==''
    assert trust['3042']['effective_date']=='2026-10-02'
    assert len(signals)==31

def test_future_unreconciled_owner_fill_cannot_be_silently_ignored(tmp_path):
    import shutil
    import statement_account as s
    target=tmp_path/'inputs';target.mkdir()
    for name in ['broker_statement_fills.csv','broker_statement_receipt.json','price_history.csv']:
        shutil.copyfile(ROOT/'inputs'/name,target/name)
    (target/'owner_reported_fills_2026-10-02.csv').write_text('trade_date,status\n2026-10-02,CONFIRMED_FILL_FEES_NET_UNKNOWN\n',encoding='utf-8')
    with pytest.raises(ValueError,match='after statement'):
        s.load_account(tmp_path)
