"""Golden values transcribed from two owner-supplied broker PnL screens."""
import sys
from decimal import Decimal as D
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))

def report():
    import owner_account
    return owner_account.load_account(ROOT)

def test_primary_pnl_uses_broker_cost_and_excludes_test_trade():
    r=report()
    assert r['realized']==D('-9561')
    assert r['unrealized']==D('-1367')
    assert r['combined']==D('-10928')
    assert r['broker_snapshot']['raw_realized']==D('-9551')
    assert r['broker_snapshot']['excluded_realized']==D('10')
    assert r['short_realized']==D('-71052')
    assert r['long_realized']==D('61491')

def test_broker_current_values_and_statement_reference_stay_separate():
    r=report()
    assert r['open_cost']==D('675866')
    assert r['market_value']==D('674499')
    assert r['marks_asof']=='2026-10-02'
    assert r['marks']['3006']['close']==D('287.50')
    assert r['position_pnl']['3006']==D('-758')
    assert sum(r['position_values'].values())==D('674499')
    assert r['statement_reference']['realized']==D('-14418')
    assert r['realized']-r['statement_reference']['realized']==D('4857')
    assert r['market_value_basis']=='BROKER_REPORTED_NET_ESTIMATE'

def test_all_realized_rows_match_statement_quantities_and_net_proceeds():
    r=report()
    assert len(r['long_closes'])==20
    assert not any(x['stock_code']=='2886' for x in r['long_closes'])
    assert sum(x['pnl'] for x in r['long_closes'])==D('61491')
    assert sum(x['delta'] for x in r['broker_snapshot']['cost_differences'])==D('4857')
    by_id={x['order_id']:x for x in r['long_closes']}
    assert by_id['X-04RE']['entry_cash_out']==D('55579')
    assert by_id['X-04RE']['pnl']==D('-25')

def test_wrong_screen_subtotal_or_position_quantity_fails_closed():
    import broker_pnl_snapshot as b
    import statement_account as s
    import json
    receipt=json.loads((ROOT/'inputs/broker_pnl_snapshot.json').read_text(encoding='utf-8'))
    realized=s.read(ROOT/'inputs'/receipt['realized_file'])
    held=s.read(ROOT/'inputs'/receipt['positions_file'])
    receipt['unrealized_totals']['pnl']='0'
    with pytest.raises(ValueError,match='subtotal'):
        b.validate_snapshot(realized,held,receipt,s.load_account(ROOT))
    receipt['unrealized_totals']['pnl']='-1367'
    held[0]['shares']='1067'
    with pytest.raises(ValueError,match='quantity'):
        b.validate_snapshot(realized,held,receipt,s.load_account(ROOT))

def test_new_statement_fill_cannot_reuse_older_realized_screen():
    import broker_pnl_snapshot as b
    import statement_account as s
    import json
    raw=s.load_account(ROOT)
    raw['asof']='2026-10-03'
    receipt=json.loads((ROOT/'inputs/broker_pnl_snapshot.json').read_text(encoding='utf-8'))
    with pytest.raises(ValueError,match='predates'):
        b.validate_snapshot(s.read(ROOT/'inputs'/receipt['realized_file']),
                            s.read(ROOT/'inputs'/receipt['positions_file']),receipt,raw)

def test_main_panel_labels_snapshot_estimates_and_exclusion():
    import owner_account
    content=owner_account.render(report())
    assert '9,561' in content  # the 10/2 screen remains the base
    assert '兆豐' not in content and '2886' not in content
    assert '預估賣出' in content and '公式估算' in content
    assert '成本缺口' not in content
    assert '4,857' in content and '原因未提供' in content
    assert '來源年份待核對' in content
    r=report()
    assert len(r['broker_snapshot']['date_conflicts'])==21
    assert all(x['broker_date'].startswith('2025-') and x['statement_date'].startswith('2026-')
               for x in r['broker_snapshot']['date_conflicts'])

def test_subpages_build_with_broker_pnl_and_separate_official_analytics():
    import build_prep,build_positions,build_watch
    for module in (build_prep,build_positions,build_watch):
        path,_=module.build()
        content=path.read_text(encoding='utf-8')
        v=report()['current']
        assert f"{v['realized']:+,.2f}" in content and f"{v['combined']:+,.2f}" in content
        assert '公式估算' in content and v['marks_asof'] in content
        assert '兆豐' not in content

def test_trade_detail_rolls_screen_forward_with_fifo_and_verified_fee_formula():
    """10/6-10/12 SinoPac trade detail on top of the 10/2 broker screen."""
    v=report()['current']
    assert v['snapshot_realized']==D('-9561')
    assert {c:x['shares'] for c,x in v['held'].items()}=={'1717':1210,'2489':3000,'2637':1000,'3006':348,'6108':136}
    by=lambda code:[x for x in v['closes'] if x['stock_code']==code]
    # 6426: 157 @352 -> 55264 - fee 78 - tax 165 = 55021 vs broker cost 49525
    assert by('6426')[0]['pnl']==D('5496')
    # 1712: 1066 broker lot (43433) + 934 of the 10/6 lot (39800+56) sold first
    assert by('1712')[0]['basis']==D('43433')+D('39856')*934/1000
    # 6108: old 2136 + 1864 of new lot sold; 136 left at new-lot unit cost
    assert v['held']['6108']['cost']==D('51673')*136/2000
    assert v['realized']==v['snapshot_realized']+sum(x['pnl'] for x in v['closes'])
    assert v['unrealized']==sum(x['value']-x['cost'] for x in v['held'].values())

def test_trade_detail_layer_rejects_unsourced_post_screen_fill(tmp_path):
    import shutil,broker_pnl_snapshot as b,statement_account as s
    root=tmp_path/'r';shutil.copytree(ROOT/'inputs',root/'inputs')
    rows=s.read(root/'inputs/actual_fills.csv')
    rows[-1]['source']='hand_typed'
    s.write(root/'inputs/actual_fills.csv',rows)
    base=b.apply(root,s.load_account(root))
    with pytest.raises(ValueError,match='trade-detail'):
        b.apply_trade_detail(root,base)
