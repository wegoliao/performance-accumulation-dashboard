"""Prevent card snapshots and selected sleeves from becoming account returns."""
import importlib.util
import json
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('return_basis_dashboard', ROOT / 'scripts/build_dashboard.py')
b = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(b)


def chart_payload(rendered):
    return json.loads(re.search(r'class="chart-data">(.*?)</script>', rendered, re.S)[1])


def test_card_chart_preserves_negative_header_despite_positive_first_card():
    rendered = b.line_chart({'投信卡片': [(date(2026, 8, 10), 102.3), (date(2026, 10, 1), 99.1)]}, 'card', rebase=False)
    payload = chart_payload(rendered)
    assert payload['series'][0]['points'][-1][1] == 99.1
    assert '-0.90%' in rendered
    assert payload['geom']['basisLabel'] == '卡片原始表頭；不是累積淨值'


def test_missing_or_partial_strategy_nav_never_uses_card_percentages():
    for curves in ({}, {'TRUST': [(date(2026, 8, 10), 100), (date(2026, 10, 1), 99)]}):
        rendered = b.theoretical_nav_panel(curves, {})
        assert 'WAITING_STRATEGY_NAV' in rendered
        assert 'chart-data' not in rendered


def test_supplied_nav_keeps_realized_gains_after_rotation_and_aligns_benchmark():
    # A strategy realizes +10%, reinvests, then earns +5%: NAV 115.5.
    # Its current member might show +5%; that must not replace the NAV.
    dates = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]
    curves = {sid: list(zip(dates, [200, 220, 231])) for sid in b.STRATEGY_LABELS}
    benchmarks = {'TAIEX': list(zip(dates, [500, 510, 520]))}
    payload = chart_payload(b.theoretical_nav_panel(curves, benchmarks))
    assert len(payload['series']) == 5
    assert payload['series'][0]['points'][-1][1] == 115.5
    assert payload['series'][-1]['points'][-1][1] == 104


def test_generated_page_separates_account_and_reconstruction():
    path, receipt = b.build()
    content = path.read_text(encoding='utf-8')
    assert '四策略現股成交重建 · 假設資金累積曲線' in content
    assert '不含蔚華科融券損益' in content
    assert 'WAITING_STRATEGY_NAV' in content
    assert '卡片原始表頭；不是累積淨值' in content
    assert receipt['history']['theoretical_nav_status'] == 'WAITING_STRATEGY_NAV'
