"""Run the control tower over the event feed.

  python enterprise_server.py --data data --events data/events.json --out runs/run1 --port 8000
  python -m control_tower.run --server http://127.0.0.1:8000 --out runs/run1
  python -m control_tower.run --twin --out runs/offline        # no server: execute on the local twin

LLM (optional, local): set CT_LLM=ollama CT_LLM_MODEL=qwen2.5:7b-instruct (or CT_LLM=openai CT_LLM_URL=...).
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

from .agents import Coordinator
from .clients import HttpClient, TwinClient
from .config import DATA_DIR, Settings
from .kg import KG
from .llm import Backend
from .pipeline import handle_event


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--twin", action="store_true", help="execute on the local twin instead of the server")
    ap.add_argument("--events", default=str(DATA_DIR / "events.json"))
    ap.add_argument("--out", default="runs/run1")
    ap.add_argument("--only", nargs="*", help="event ids to run")
    args = ap.parse_args(argv)

    out = Path(args.out)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    kg = KG(DATA_DIR)
    stats = kg.export(out / "ontology")
    settings = Settings()
    backend = Backend.from_env()
    coord = Coordinator(kg, settings, backend)
    if args.twin:
        client = TwinClient(namespace="live")
        events = json.loads(Path(args.events).read_text(encoding="utf-8"))
    else:
        client = HttpClient(args.server)
        events = client.events()
    if args.only:
        events = [e for e in events if e["event_id"] in args.only]
    cat = coord.catalog(client)
    print(f"KG: {stats['nodes']} nodes, {stats['edges']} edges ({time.time() - t0:.1f}s). "
          f"LLM: {backend.kind + ':' + backend.model if backend else 'none (rule reader only)'}. "
          f"Executing on: {'local twin' if args.twin else args.server}. "
          f"Tools: {len(cat.spec) if cat else 0} discovered via {cat.source if cat else 'nothing (calls unchecked)'}",
          flush=True)
    rows = []
    for ev in events:
        tr = handle_event(coord, ev, client)
        (out / "traces" / f"{ev['event_id']}.json").write_text(json.dumps(tr, indent=1, default=str), encoding="utf-8")
        dr = tr["dry_run"]
        rows.append({"event_id": ev["event_id"], "category": "+".join(tr["category"]), **tr["stats"],
                     "proof": None if dr is None else f"{sum(c['ok'] for c in dr['checks'])}/{len(dr['checks'])}",
                     "summary": tr["summary"]})
        print(f"{ev['event_id']:6} {rows[-1]['category']:30} calls={tr['stats']['live_calls']:4} "
              f"writes={tr['stats']['writes']:4} failed={tr['stats']['failed_live_calls']} proof={rows[-1]['proof']}", flush=True)
    if args.twin:
        with open(out / "action_log.jsonl", "w", encoding="utf-8") as f:
            for c in client.calls:
                f.write(json.dumps({k: c[k] for k in ("event_id", "tool", "args", "ok")}) + "\n")
    run = {"events": rows, "kg": stats, "settings": settings.to_json(),
           "tool_catalog": {"source": cat.source, "tools": sorted(cat.spec)} if cat else None,
           "llm": None if not backend else {"backend": backend.kind, "model": backend.model, "calls": backend.calls,
                                            "seconds": round(backend.seconds, 1), "tokens": backend.tokens},
           "machine": {"python": sys.version.split()[0], "platform": platform.platform()},
           "seconds": round(time.time() - t0, 1)}
    (out / "run_summary.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    print(f"done in {run['seconds']}s -> {out}")


if __name__ == "__main__":
    main()
