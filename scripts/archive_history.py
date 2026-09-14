"""Keep every day's dashboard, so the owner can go back and read it as it was.

Git already holds every version of index.html. This script makes that history
readable without git: for each holdings snapshot date it freezes the dashboard
that was live at the end of that day under history/<date>/index.html and
writes an index of those days with the account numbers each one carried.

For the current snapshot date the copy is taken from the working tree (the
build that is about to be committed). For earlier dates it is taken from the
last commit made on or before that day, so a re-run never rewrites the past.

Pure standard library plus a read-only ``git show``. No network, no broker,
no order path.
"""

from __future__ import annotations

import csv
import html
import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
SITE = ROOT / "history"
REPO_URL = "https://github.com/wegoliao/performance-accumulation-dashboard"
SUBPATHS = ("prep/", "realized/", "mainline2/", "claude/", "positions/", "history/")


def read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any(row.values())]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


def relink(page: str) -> str:
    """Point the frozen copy's relative links back at the live sub-pages."""
    for sub in SUBPATHS:
        page = re.sub(rf'href="{re.escape(sub)}', f'href="../../{sub}', page)
    return page


def commit_on_or_before(day: date) -> str:
    return git("log", "-1", "--format=%H", f"--before={day.isoformat()}T23:59:59+08:00", "--", "index.html")


def freeze(day: date, is_current: bool) -> tuple[str, str]:
    """Return (sha, status) after making sure history/<day>/index.html exists."""
    target = SITE / day.isoformat() / "index.html"
    if is_current:
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relink(page), encoding="utf-8", newline="\n")
        return "WORKING_TREE", "FROZEN_FROM_CURRENT_BUILD"
    sha = commit_on_or_before(day)
    if target.exists():
        return sha, "KEPT"
    if not sha:
        return "", "NO_COMMIT_ON_OR_BEFORE"
    page = git("show", f"{sha}:index.html")
    if not page:
        return sha, "INDEX_NOT_IN_COMMIT"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(relink(page), encoding="utf-8", newline="\n")
    return sha, "FROZEN_FROM_GIT"


def build() -> tuple[Path, dict]:
    summaries = sorted(INPUTS.glob("snapshot_summary_????-??-??.csv"))
    days = [datetime.strptime(p.stem[-10:], "%Y-%m-%d").date() for p in summaries]
    current = max(days)
    rows = []
    for path, day in zip(summaries, days):
        summary = read(path)[0]
        sha, status = freeze(day, day == current)
        rows.append({"day": day, "summary": summary, "sha": sha, "status": status})

    body = []
    for row in sorted(rows, key=lambda r: r["day"], reverse=True):
        s = row["summary"]
        pnl = float(s.get("unrealized_pnl_twd") or 0)
        klass = "positive" if pnl > 0 else "negative" if pnl < 0 else "neutral"
        link = f'<a href="{row["day"].isoformat()}/">{row["day"].isoformat()}</a>'
        sha_link = (
            f'<a href="{REPO_URL}/commit/{row["sha"]}">{row["sha"][:7]}</a>'
            if row["sha"] and row["sha"] != "WORKING_TREE" else "<small>本次建置</small>"
        )
        body.append(
            "<tr>"
            f"<td>{link}</td>"
            f'<td class="num">{float(s.get("shares") or 0):,.0f}</td>'
            f'<td class="num">{float(s.get("current_value_twd") or 0):,.0f}</td>'
            f'<td class="num">{float(s.get("cost_basis_twd") or 0):,.0f}</td>'
            f'<td class="num {klass}">{pnl:+,.0f}<br><small>{float(s.get("unrealized_return_pct") or 0):+.2f}%</small></td>'
            f'<td><small>{html.escape(s.get("source", ""))}</small></td>'
            f"<td>{sha_link}</td>"
            "</tr>"
        )

    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>歷史存檔 · 每一天的儀表板</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--raise:#1c2430;--ink:#e6edf3;--muted:#8b949e;--line:#26303b;--accent:#58a6ff;--green:#3fb950;--red:#f85149}}
@media(prefers-color-scheme:light){{:root{{--bg:#f6f8fa;--panel:#fff;--raise:#f0f3f6;--ink:#1f2328;--muted:#59636e;--line:#d8dee4;--accent:#0969da;--green:#1a7f37;--red:#cf222e}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;line-height:1.6}}
.wrap{{max-width:1100px;margin:0 auto;padding:30px 20px 80px}}a{{color:var(--accent)}}
h1{{font-size:clamp(24px,4vw,34px);margin:8px 0 10px}}.lede{{color:var(--muted);max-width:70ch}}
.eyebrow{{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:700}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin-top:20px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th{{text-align:left;color:var(--muted);font-size:11.5px;border-bottom:1px solid var(--line);padding:9px 8px}}
td{{padding:10px 8px;border-bottom:1px solid var(--line)}}td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
.positive{{color:var(--green)}}.negative{{color:var(--red)}}.neutral{{color:var(--muted)}}small{{color:var(--muted)}}
.table-wrap{{overflow-x:auto}}
</style></head><body><div class="wrap">
<div class="eyebrow">History &middot; frozen copies</div>
<h1>歷史存檔 · 每一天的儀表板長什麼樣</h1>
<p class="lede">每一個庫存快照日，儀表板當天結束時的版本都凍結在這裡，可以點回去看。數字是那一天的，不會被之後的建置改寫。
完整的逐次變更在 <a href="{REPO_URL}/commits/main">GitHub 提交紀錄</a>。</p>
<div class="panel"><div class="table-wrap"><table><thead><tr><th>日期</th><th class="num">股數</th><th class="num">市值</th><th class="num">成本</th><th class="num">未實現</th><th>快照來源</th><th>提交</th></tr></thead>
<tbody>{"".join(body)}</tbody></table></div></div>
<p class="lede" style="margin-top:20px"><a href="../">&larr; 回到實際績效</a></p>
</div></body></html>
"""
    SITE.mkdir(parents=True, exist_ok=True)
    target = SITE / "index.html"
    target.write_text(page, encoding="utf-8", newline="\n")
    receipt = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days": [{"day": r["day"].isoformat(), "sha": r["sha"], "status": r["status"]} for r in rows],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "history_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return target, receipt


if __name__ == "__main__":
    target, receipt = build()
    print(f"SUCCESS: {target}")
    for day in receipt["days"]:
        print(f'  {day["day"]} {day["status"]} {day["sha"][:7]}')
