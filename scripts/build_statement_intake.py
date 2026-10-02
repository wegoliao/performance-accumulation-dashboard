"""Publish only sanitized sources, never the owner's raw statement HTML."""
import html
import json
import shutil
from pathlib import Path
import owner_account
import statement_account

def build(root):
    target=root/'intake';target.mkdir(exist_ok=True)
    r=owner_account.load_account(root)  # Validate source paths before copying public files.
    files=['broker_statement_fills.csv','broker_statement_receipt.json','actual_fills.csv',
           'latest_strategy_signals.csv','strategy_card_returns.csv','owner_preferences.csv']
    if (root/'inputs/broker_pnl_snapshot.json').exists():
        receipt=json.loads((root/'inputs/broker_pnl_snapshot.json').read_text(encoding='utf-8'))
        files+=['broker_pnl_snapshot.json',receipt['realized_file'],receipt['positions_file']]
    for name in files:shutil.copyfile(root/'inputs'/name,target/name)
    (target/'account_reconciliation.json').write_text(json.dumps(r,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    labels={'broker_statement_fills.csv':'完整成交對帳・59 筆（已移除帳號姓名）',
            'broker_statement_receipt.json':'來源 SHA 與總額檢核收據',
            'actual_fills.csv':'四策略已核對成交・58 列（含歷史分配）',
            'latest_strategy_signals.csv':'10/1 原始策略卡・31 列',
            'strategy_card_returns.csv':'各日卡片表頭（來源報酬）',
            'owner_preferences.csv':'Owner 本次投信偏好',
            'account_reconciliation.json':'券商主指標與獨立成交現金重建（分開保存）'}
    if 'broker_snapshot' in r:
        labels.update({'broker_pnl_snapshot.json':'最新券商損益截圖・來源口徑與小計',
                       receipt['realized_file']:'券商已實現明細・22 列（兆豐金標示排除）',
                       receipt['positions_file']:'券商目前持股・9 檔損益試算'})
    links=''.join(f'<li><a href="{name}" download>{html.escape(label)}</a></li>' for name,label in labels.items())
    rows=[]
    for x in statement_account.read(root/'inputs/broker_statement_fills.csv'):
        fields=['trade_date','type','stock_code','stock_name','shares','price','gross','fee','tax','interest','borrow_fee','payable','receivable']
        cells=[]
        for k in fields:
            val=x[k]
            if k in statement_account.NUMBERS:val=f'{statement_account.D(val):,.2f}'
            cells.append('<td>'+html.escape(val)+'</td>')
        rows.append('<tr>'+''.join(cells)+'</tr>')
    header=''.join(f'<th>{x}</th>' for x in ['成交日','交易別','代碼','名稱','股數','成交價','價金','手續費','交易稅','利息','借券費','應付','應收'])
    panel=owner_account.render(r).replace('href="intake/"','href="./"')
    page=f'''<!doctype html><html lang="zh-Hant-TW"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>成交對帳來源・59 筆</title>
<style>body{{margin:0;background:#0b1512;color:#ecf4ef;font:15px/1.65 "Segoe UI","Noto Sans TC",sans-serif}}main{{max-width:1200px;margin:auto;padding:24px}}a{{color:#72a7ff}}.panel{{background:#14231f;padding:22px;border-radius:14px}}.metrics{{display:grid;gap:12px}}.metric-card{{border:1px solid #2a4039;padding:15px;border-radius:12px}}.metric-label,.sub,.metric-note{{color:#9eaaa5}}.metric-value{{font-weight:bold;white-space:nowrap}}.negative{{color:#ff7f7f}}.positive{{color:#57d3a2}}.callout{{padding:12px;background:#2a2618;border-left:3px solid #f5bd58}}.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #2a4039;white-space:nowrap}}.num{{text-align:right}}summary{{cursor:pointer}}</style></head>
<body><main><p><a href="../">← 回實際績效</a></p><h1>成交對帳來源與驗算</h1><p>對帳單期間 2026-08-11 ～ 2026-10-01，59 筆。使用券商實際價金及淨收付，不以成交價乘股數取代整數交割金額。</p><ul>{links}</ul>{panel}
<details><summary>展開全部 59 筆成交（横向捲動查看費用與收付）</summary><div class="table-wrap"><table><thead><tr>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></details>
<details><summary>先前貼文回報・保留歷史</summary><p><a href="owner_reported_fills_2026-10-01.csv">24 列 Owner 先前貼文</a>的費用欄當時未提供，現在已由完整券商對帳單取代；不重複加進成交或損益。</p></details></main></body></html>'''
    (target/'index.html').write_text(page,encoding='utf-8')

if __name__=='__main__':build(Path(__file__).resolve().parents[1])
