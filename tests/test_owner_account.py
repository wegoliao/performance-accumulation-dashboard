from pathlib import Path
import sys
from decimal import Decimal
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

def load_module():
    import owner_account
    return owner_account

def test_owner_reported_closes_remove_positions_without_known_fees():
    m = load_module()
    report = m.load_account(ROOT)
    assert report['positions'].get('2354', 0) == 0
    assert report['positions'].get('3231', 0) == 0
    assert report['short_remaining_shares'] == 0
    assert len(report['positions']) == 9
    assert report['short_price_pnl'] == Decimal('-70000')
    assert report['short_estimated_cost'] == Decimal('959')
    assert report['short_net_estimate'] == Decimal('-70959')
    assert report['exact_net_status'] == 'UNKNOWN_UNREPORTED_FEES'

def test_reported_fragments_aggregate_once_and_preserve_known_buy_costs():
    m = load_module()
    r = m.load_account(ROOT)
    assert len(r['new_long_closes']) == 2
    by_code = {x['stock_code']:x for x in r['new_long_closes']}
    assert by_code['2354']['gross'] == Decimal('52244.40')
    assert by_code['2354']['entry_cash_out'] == Decimal('49556')
    assert by_code['3231']['gross'] == Decimal('95440.50')
    assert r['known_realized_before_missing_costs'] == Decimal('-12711.60')
    assert r['net_realized_estimate'] == Decimal('-14322.60')

def test_rendered_current_view_is_separate_from_historical_snapshot():
    m = load_module()
    r = m.load_account(ROOT)
    content = m.render(r)
    assert '已回補' in content and '70,959' in content
    assert '已平倉' in content and '9 檔' in content
    assert '費稅估算' in content and '借券費' in content
    assert max(mark['date'] for mark in r['marks'].values()) in content
    assert r['asof'] in content

def test_current_subpages_use_replayed_quantity_not_old_snapshot():
    import build_prep
    snapshot_day, held = build_prep.latest_holdings()
    assert snapshot_day.isoformat() == '2026-09-17'
    assert len(held) == 9
    assert not ({'2354', '3231', '3055'} & held.keys())

def test_long_sale_cannot_exceed_confirmed_open_quantity():
    m = load_module()
    with pytest.raises(ValueError, match='exceeds'):
        m.apply_close({'2354': 788}, '2354', Decimal('789'))
