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
    assert '9,561' in content and '1,367' in content and '10,928' in content
    assert '兆豐金測試交易已排除' in content
    assert '預估賣出費稅' in content
    assert '盤中截圖' in content and '精確報價時間未提供' in content
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
        assert '9,561' in content and '1,367' in content and '10,928' in content
        assert '券商盤中截圖' in content and '以下技術量測' in content
        assert '2026-10-02' in content
