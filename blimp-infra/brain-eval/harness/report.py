#!/usr/bin/env python3
"""
report.py — render a brain-bench results JSON into a self-contained HTML
scorecard (no italics, no external deps, light/dark aware — house style).

Usage: report.py <results.json> [out.html]
"""
from __future__ import annotations

import os
import sys
import json
import html


def _bar(acc: float) -> str:
    pct = int(round((acc or 0) * 100))
    hue = int(120 * (acc or 0))  # red->green
    return (f'<div class="bar"><div class="fill" style="width:{pct}%;'
            f'background:hsl({hue},60%,45%)"></div><span>{pct}%</span></div>')


def render(data: dict) -> str:
    s = data["summary"]
    m, o = s["meta"], s["overall"]
    rows = ""
    for cat, c in s["by_category"].items():
        rows += (f"<tr><td>{html.escape(cat)}</td><td class='n'>{c['correct']}/"
                 f"{c['total']}</td><td>{_bar(c['accuracy'])}</td></tr>")
    items_rows = ""
    for it in data.get("items", []):
        mark = "ok" if it["correct"] else "miss"
        ans = html.escape(str(it.get("answer", "—"))[:160])
        items_rows += (
            f"<tr class='{mark}'><td>{html.escape(it['category'])}</td>"
            f"<td>{html.escape(it['question'][:110])}</td>"
            f"<td>{html.escape(str(it['gold'])[:80])}</td>"
            f"<td>{ans}</td>"
            f"<td class='c'>{'PASS' if it['correct'] else 'MISS'}</td></tr>")
    acc_pct = int(round((o["accuracy"] or 0) * 100))
    rhr = o.get("retrieval_hit_rate_nonabstain")
    rhr_line = (f"<div class='kv'><span>Retrieval hit (non-abstention)</span>"
                f"<b>{int(round(rhr*100))}%</b></div>" if rhr is not None else "")
    return f"""<!doctype html><meta charset=utf-8>
<title>Brain Bench — {html.escape(m['suite'])}</title>
<style>
:root{{--bg:#faf7f1;--fg:#17140f;--mut:#6b6559;--hair:#e7e0d3;--card:#fff;--accent:#cc785c}}
@media(prefers-color-scheme:dark){{:root{{--bg:#17140f;--fg:#f3efe6;--mut:#a29a8c;--hair:#2c2822;--card:#1e1a15}}}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;
background:var(--bg);color:var(--fg);font-style:normal}}
em,i{{font-style:normal}}
.wrap{{max-width:940px;margin:0 auto;padding:40px 22px}}
h1{{font-size:26px;margin:0 0 4px}}.sub{{color:var(--mut);margin:0 0 26px}}
.hero{{display:flex;gap:26px;align-items:center;background:var(--card);border:1px solid var(--hair);
border-radius:16px;padding:24px;margin-bottom:22px;flex-wrap:wrap}}
.score{{font-size:56px;font-weight:700;color:var(--accent);line-height:1}}
.score small{{font-size:18px;color:var(--mut);font-weight:400}}
.kv{{display:flex;justify-content:space-between;gap:18px;min-width:230px;
padding:6px 0;border-bottom:1px solid var(--hair)}}
.kv span{{color:var(--mut)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--hair);
border-radius:12px;overflow:hidden;margin:14px 0 30px}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid var(--hair);vertical-align:top}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}}
td.n,td.c{{white-space:nowrap;font-variant-numeric:tabular-nums}}
tr.miss td.c{{color:#c0392b;font-weight:600}}tr.ok td.c{{color:#1e8449;font-weight:600}}
.bar{{position:relative;background:var(--hair);border-radius:6px;height:20px;min-width:120px}}
.fill{{height:100%;border-radius:6px}}
.bar span{{position:absolute;right:6px;top:0;line-height:20px;font-size:12px;
font-variant-numeric:tabular-nums}}
.note{{background:var(--card);border:1px solid var(--hair);border-left:3px solid var(--accent);
border-radius:8px;padding:14px 16px;color:var(--mut);font-size:14px;margin:8px 0 26px}}
.small{{font-size:12px;color:var(--mut)}}
</style>
<div class=wrap>
<h1>Brain Bench — {html.escape(m['suite'])}</h1>
<p class=sub>Our own measured score on a public memory benchmark · {html.escape(m['date'])}</p>
<div class=hero>
  <div class=score>{acc_pct}%<br><small>{o['correct']}/{o['questions']} correct</small></div>
  <div style="flex:1;min-width:240px">
    <div class=kv><span>Suite / mode</span><b>{html.escape(m['suite'])} · {html.escape(m['mode'])}</b></div>
    <div class=kv><span>Recall top-k</span><b>{m['k']}</b></div>
    {rhr_line}
    <div class=kv><span>Recall latency p50 / p95</span><b>{s['latency']['recall_p50_s']}s / {s['latency']['recall_p95_s']}s</b></div>
    <div class=kv><span>Answer / judge model</span><b class=small>{html.escape(m['answer_model'])} / {html.escape(m['judge_model'])}</b></div>
  </div>
</div>
<div class=note>This is a number we measured ourselves, in an isolated throwaway
bank, through the real recall path. It is comparable to our own past runs;
against other vendors' published numbers, treat it as indicative only — every
lab runs its own harness, judge, and baseline (a system has scored 66% and 75%
on the same test depending on who ran it).</div>
<h3>By category</h3>
<table><tr><th>Category</th><th>Correct</th><th>Accuracy</th></tr>{rows}</table>
<h3>Every question</h3>
<table><tr><th>Category</th><th>Question</th><th>Gold</th><th>Brain answer</th><th></th></tr>{items_rows}</table>
<p class=small>groups={m['groups_run']} · errors={m['errors']} · wall={m['wall_seconds']}s ·
generated by blimp-infra/brain-eval/harness</p>
</div>"""


def main():
    if len(sys.argv) < 2:
        print("usage: report.py <results.json> [out.html]"); sys.exit(1)
    src = sys.argv[1]
    data = json.load(open(src))
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + ".html"
    with open(out, "w") as f:
        f.write(render(data))
    print(out)


if __name__ == "__main__":
    main()
