"""Replay an action_log.jsonl on a fresh world and re-check every invariant.

  python -m control_tower.replay runs/run1
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from .agents import SupplyChainPlanner
from .clients import TwinClient
from .config import DATA_DIR, Settings
from .ground import Grounded
from .kg import KG
from .verify import WRITE_TOOLS, check


def main(run_dir: str):
    run = Path(run_dir)
    rows = [json.loads(l) for l in (run / "action_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    kg = KG(DATA_DIR)
    base = SupplyChainPlanner(kg, Settings()).base_loc
    twin = TwinClient(namespace="replay")
    by_event = defaultdict(list)
    ids = {}
    for r in rows:
        if r["event_id"] not in ids:
            ids[r["event_id"]] = twin.fresh(r["event_id"])
        ok, _ = twin.act(ids[r["event_id"]], r["tool"], r["args"])
        by_event[r["event_id"]].append({**r, "ok": ok, "logged_ok": r["ok"]})
    all_ok = True
    for tf in sorted((run / "traces").glob("*.json")):
        tr = json.loads(tf.read_text(encoding="utf-8"))
        eid = tr["event"]["event_id"]
        incs = [Grounded(g["kind"], tuple(g["key"]), g["bindings"], [], "", "", "") for g in tr["verdict"]["incidents"]]
        calls = by_event.get(eid, [])
        mismatch = sum(c["ok"] != c["logged_ok"] for c in calls)
        state = twin.state(ids.get(eid) or twin.fresh(eid))
        rep = check(kg, incs, calls, state, base)
        esc = sum(c["tool"] == "escalate" for c in calls)
        w = sum(c["tool"] in WRITE_TOOLS for c in calls)
        ok = rep.ok and mismatch == 0
        all_ok &= ok
        bad = "; ".join(c["check"] for c in rep.failures())
        print(f"{eid:6} {'+'.join(tr['category']):30} writes={w:4} escalations={esc} replay={'OK' if ok else 'FAIL ' + bad}")
    print("ALL OK" if all_ok else "SOME CHECKS FAILED")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "runs/run1"))
