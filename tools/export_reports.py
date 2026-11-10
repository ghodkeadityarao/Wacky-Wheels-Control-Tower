"""Render per-event Markdown reports and an ontology SVG from a run folder (generated, never hand-written).

usage: python tools/export_reports.py runs/<run> [out_dir]
"""
from __future__ import annotations

import collections
import json
import math
import sys
from pathlib import Path


def event_md(t: dict) -> str:
    ev = t["event"]
    L = [f"# {ev['event_id']} — {', '.join(t['category'])}", "",
         f"*{ev['received_at']} · {ev['source']}*", "", "> " + ev["text"].replace("\n", "\n> "), "",
         "## Summary", t["summary"], ""]
    v = t.get("verdict", {})
    dp = t.get("dates_plain") or {}
    if dp.get("dates"):
        L += ["## Date reading"] + [f"- “{d['phrase']}” → {d['role']} **{d['date']}**"
                                    + (f" (other reading: {', '.join(d['alternative'])})" if d["alternative"] else "")
                                    for d in dp["dates"]] + ([dp["why"]] if dp.get("why") else []) + [""]
    elif v.get("convention_note"):
        L += ["## Date reading", v["convention_note"], ""]
    if t.get("human_review"):
        L += ["## Why a human must review"]
        for i in t["human_review"]:
            L += [f"- **{i['headline']}.** " + " ".join(i["why"]) + f" To act we need {i['need']}."]
        L += [""]
    else:
        for r in v.get("noise_reasons", []) + v.get("escalations", []):
            L += [f"- {r}"]
    for inc in t.get("incidents", []):
        L += [f"## Incident {inc['id']}", "", "**Grounding checks**"] + [f"- {c}" for c in inc["grounding_checks"]]
        L += ["", "**Graph path**", "", " → ".join(inc["path_schema"]), "", "`" + " → ".join(inc["example_path"]) + "`", "",
              f"Affected production orders: {', '.join(inc['affected_production_orders']) or 'none'}", ""]
    if t.get("moves"):
        L += ["## Production moves", "", "| PO | Model | From | To | Type |", "|---|---|---|---|---|"]
        L += [f"| {m['po']} | {m['model']} | {m['from'][0]} {m['from'][1]} | {m['to'][0]} {m['to'][1]} | {m['type']} |"
              for m in t["moves"]]
        L += [""]
    writes = [c for c in t.get("executed", []) if not c["tool"].startswith(("mes.get", "crm.loaner_avail"))]
    if writes:
        n = collections.Counter(f"{c['agent']} · {c['tool']}" for c in writes)
        L += ["## Actions executed", ""] + [f"- {k}: {c}" for k, c in sorted(n.items())] + [""]
    dr = t.get("dry_run") or {}
    if dr.get("checks"):
        L += ["## Proof (dry run)", ""] + [f"- {'PASS' if c['ok'] else 'FAIL'} — {c['check']}" for c in dr["checks"]] + [""]
    L += ["## Audit trail", ""] + [f"- **{a['agent']}** `{a['step']}` {a['detail'][:300]}" for a in t.get("audit", [])]
    return "\n".join(L) + "\n"


def ontology_svg(onto_path: Path) -> str:
    o = json.loads(onto_path.read_text())
    edges = o.get("relations") or o.get("edges") or []
    classes = o.get("classes") or []
    names = [c["name"] if isinstance(c, dict) else c for c in classes]
    rel = []
    for e in edges:
        if isinstance(e, dict):
            rel.append((e.get("source") or e.get("from"), e.get("name") or e.get("label") or e.get("type"), e.get("target") or e.get("to")))
        else:
            rel.append(tuple(e[:3]))
    if not names:
        names = sorted({x for a, _, b in rel for x in (a, b)})
    W, H, R = 900, 700, 270
    pos = {n: (W / 2 + R * math.cos(2 * math.pi * i / len(names) - math.pi / 2),
               H / 2 + R * math.sin(2 * math.pi * i / len(names) - math.pi / 2)) for i, n in enumerate(names)}
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="11">',
         '<rect width="100%" height="100%" fill="white"/>',
         '<defs><marker id="a" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8z" fill="#888"/></marker></defs>']
    for a, lab, b in rel:
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            d = math.hypot(x2 - x1, y2 - y1) or 1
            x2s, y2s = x2 - (x2 - x1) / d * 38, y2 - (y2 - y1) / d * 18
            s.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2s:.0f}" y2="{y2s:.0f}" stroke="#888" marker-end="url(#a)"/>')
            s.append(f'<text x="{(x1+x2)/2:.0f}" y="{(y1+y2)/2:.0f}" fill="#555" text-anchor="middle">{lab}</text>')
    for n, (x, y) in pos.items():
        s.append(f'<rect x="{x-48:.0f}" y="{y-16:.0f}" width="96" height="32" rx="6" fill="#e8f0fe" stroke="#2b5fb3"/>'
                 f'<text x="{x:.0f}" y="{y+4:.0f}" text-anchor="middle" font-weight="bold">{n}</text>')
    return "\n".join(s + ["</svg>"])


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help") or len(args) > 2:
        print(__doc__.strip())
        sys.exit(0 if args and args[0] in ("-h", "--help") else 2)
    run = Path(args[0])
    if not (run / "traces").is_dir():
        print(f"error: {run} is not a run folder (expected {run / 'traces'}/ with E-xx.json traces)")
        sys.exit(2)
    out = Path(args[1]) if len(args) > 1 else run / "reports"
    out.mkdir(parents=True, exist_ok=True)
    for f in sorted((run / "traces").glob("E-*.json")):
        (out / f"{f.stem}.md").write_text(event_md(json.loads(f.read_text(encoding="utf-8"))), encoding="utf-8")
    onto = run / "ontology" / "ontology.json"
    if onto.exists():
        (out / "ontology.svg").write_text(ontology_svg(onto), encoding="utf-8")
    print(f"wrote {len(list(out.glob('*')))} files to {out}")


if __name__ == "__main__":
    main()
