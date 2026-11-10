"""Independent policy audit of an action log. Imports NOTHING from control_tower.

It replays the log into a fresh copy of the provided enterprise.py and checks the handbook rules
literally against expectations computed from the raw CSVs and a HAND-READ ANSWER KEY (a human's reading of
each event text). The key lives only in this evaluation script; the control tower never sees it.

  python tools/audit_run.py runs/<run>            # package dir defaults to the repo root
  python tools/audit_run.py <pkg_dir> <run_dir>

Exit code 0 when every rule holds, 1 otherwise.
"""
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
    print(__doc__)
    sys.exit(0)
if len(sys.argv) == 2:
    PKG, RUN = Path(__file__).resolve().parent.parent, Path(sys.argv[1])
elif len(sys.argv) == 3:
    PKG, RUN = Path(sys.argv[1]), Path(sys.argv[2])
else:
    print(__doc__)
    sys.exit(2)
if not (RUN / "action_log.jsonl").is_file():
    print(f"error: {RUN} has no action_log.jsonl")
    sys.exit(2)
sys.path.insert(0, str(PKG))
from enterprise import TOOLS, Enterprise  # noqa: E402  (provided enterprise code, unmodified)

TODAY = date(2027, 3, 8)
D = lambda s: date.fromisoformat(s)


def idx(d):
    days = (d - TODAY).days
    assert days % 7 < 5, f"{d} weekend"
    return 5 * (days // 7) + days % 7


def rd(n):
    with open(PKG / "data" / n, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


po = {r["production_order_id"]: r for r in rd("production_orders.csv")}
cos = rd("customer_orders.csv")
cust = {r["customer_id"]: r for r in rd("customers.csv")}
veh = rd("finished_vehicles.csv")
lots = rd("part_lots.csv")
ships = rd("inbound_shipments.csv")
pool = {r["dealer_id"]: int(r["loaners_available"]) for r in rd("loaners.csv")}
slots = {r["plant_id"]: int(r["slots_per_day"]) for r in rd("plants.csv")}
builds = defaultdict(set)
for r in rd("plant_models.csv"):
    builds[r["plant_id"]].add(r["model"])
bom = defaultdict(set)
for r in rd("bom.csv"):
    bom[r["model"]].add(r["part_id"])

# ---------------------------------------------------------------- answer key: a human reading of each event
KEY = {
    "E-01": [("outage", "P_TX", "2027-03-19", "2027-03-22"),
             ("delay", "CHIP-IVI", "P_IN", "SUP-12", "2027-03-15", "2027-03-19")],
    "E-02": "rumor",
    "E-03": [("quality", "CHIP-IVI", "P_MS", "SUP-06", "2027-02-25", "2027-03-01")],
    "E-04": [("outage", "P_KY", "2027-03-16", "2027-03-18")],          # assumes 'next Tuesday' = Mar 16
    "E-05": "noise", "E-06": "noise", "E-07": "noise",
    "E-08": [("outage", "P_KY", "2027-03-24", "2027-03-25")],
    "E-09": [("delay", "CAM-360", "P_AL", "SUP-11", "2027-03-29", "2027-04-01")],
    "E-10": [("delay", "BATT-60", "P_AL", "SUP-18", "2027-03-29", "2027-04-01")],
    "E-11": [("outage", "P_MS", "2027-03-26", "2027-03-30")],
    "E-12": [("delay", "CAM-360", "P_IN", "SUP-11", "2027-03-15", "2027-03-18")],
    "E-13": "rumor",
    "E-14": [("quality", "HARN-B", "P_TX", "SUP-07", "2027-02-22", "2027-02-24")],
    "E-15": [("quality", "CHIP-IVI", "P_IN", "SUP-12", "2027-03-02", "2027-03-03")],
}
WRITES = {k for k, v in TOOLS.items() if v["writes"]}


def expected(incidents):
    """Handbook rules 1, 2, 5, 6 computed straight from the CSVs."""
    e = {"pos": {}, "blocks": [], "lots": set(), "unsold": set(), "owners": set(), "inc_pos": []}
    for inc in incidents:
        if inc[0] == "outage":
            _, plant, a, b = inc
            lo, hi = idx(D(a)), idx(D(b))
            hit = {p for p, r in po.items() if r["plant_id"] == plant and max(lo, 0) <= idx(D(r["build_date"])) <= hi}
            e["blocks"].append((plant, lo, hi, None))
            restart = hi + 1
        elif inc[0] == "delay":
            _, part, plant, sup, a, b = inc
            sh = [s for s in ships if s["part_id"] == part and s["plant_id"] == plant and s["supplier_id"] == sup
                  and s["eta_date"] == a]
            assert len(sh) == 1, f"answer key does not match exactly one shipment: {inc} -> {sh}"
            models = {m for m, ps in bom.items() if part in ps}
            lo, new = idx(D(a)), idx(D(b))
            hit = {p for p, r in po.items() if r["plant_id"] == plant and r["model"] in models
                   and max(lo, 0) <= idx(D(r["build_date"])) < new}
            e["blocks"].append((plant, lo, new - 1, models))
            restart = new
        else:
            _, part, plant, sup, a, b = inc
            ls = [l for l in lots if l["part_id"] == part and l["plant_id"] == plant and l["supplier_id"] == sup
                  and l["first_build_date"] >= a and l["last_build_date"] <= b]
            assert ls and min(l["first_build_date"] for l in ls) == a and max(l["last_build_date"] for l in ls) == b, inc
            e["lots"] |= {l["lot_id"] for l in ls}
            for l in ls:
                for v in veh:
                    if (v["plant_id"] == plant and l["first_build_date"] <= v["build_date"] <= l["last_build_date"]
                            and part in bom[v["model"]]):
                        if v["status"] == "in_stock":
                            e["unsold"].add((v["vin"], v["dealer_id"]))
                        else:
                            e["owners"].add(v["customer_id"])
            e["inc_pos"].append(set())
            continue
        e["inc_pos"].append(hit)
        for p in hit:
            e["pos"][p] = max(e["pos"].get(p, -1), restart)
    aff_cos = [c for c in cos if c["production_order_id"] in e["pos"]]
    e["notified"] = defaultdict(set)
    ent = defaultdict(list)
    for c in aff_cos:
        e["notified"][c["dealer_id"]].add(c["customer_order_id"])
        t = cust[c["customer_id"]]["tier"]
        if t in ("Platinum", "Gold"):
            ent[c["dealer_id"]].append((0 if t == "Platinum" else 1, c["promised_date"], c["customer_order_id"]))
    e["loaned"] = {r[2] for d, rows in ent.items() for r in sorted(rows)[:pool[d]]}
    e["n_entitled"] = sum(len(v) for v in ent.values())
    return e


rows = [json.loads(l) for l in (RUN / "action_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
by_ev = defaultdict(list)
for r in rows:
    by_ev[r["event_id"].split("#")[0]].append(r)
ent = Enterprise(str(PKG / "data"))
base = {p: (r["plant_id"], idx(D(r["build_date"]))) for p, r in po.items() if idx(D(r["build_date"])) >= 0}
problems, notes = defaultdict(list), defaultdict(list)

# schema check against the server's own tool list
for r in rows:
    spec = TOOLS.get(r["tool"])
    if spec is None:
        problems[r["event_id"]].append(f"unknown tool {r['tool']}")
    elif set(r["args"]) != set(spec["args"]):
        problems[r["event_id"]].append(f"{r['tool']} args {sorted(r['args'])} != schema {sorted(spec['args'])}")
    if not r["ok"]:
        problems[r["event_id"]].append(f"failed call {r['tool']} {r['args']}")

for eid, key in KEY.items():
    calls = by_ev.get(eid, [])
    wid = "audit-" + eid
    for c in calls:
        try:
            ent.call(wid, c["tool"], c["args"])
        except Exception as ex:  # noqa: BLE001
            problems[eid].append(f"replay failed: {c['tool']} {ex}")
    s = ent.state(wid)
    writes = [c for c in calls if c["tool"] in WRITES]
    esc = [c for c in calls if c["tool"] == "escalate"]
    if key == "noise":
        if calls:
            problems[eid].append(f"noise must have no calls; got {Counter(c['tool'] for c in calls)}")
        continue
    if key == "rumor":
        if writes or len(esc) != 1:
            problems[eid].append(f"rumor needs exactly 1 escalate and 0 writes; got {len(esc)} / {len(writes)}")
        continue
    if esc:
        notes[eid].append(f"also escalated: {esc[0]['args']['reason'][:120]}")
    e = expected(key)
    moved = {p for p, loc in s["loc"].items() if base.get(p) != loc}
    if moved != set(e["pos"]):
        problems[eid].append(f"moved {len(moved)} vs expected {len(e['pos'])}: extra {sorted(moved - set(e['pos']))[:4]} "
                             f"missing {sorted(set(e['pos']) - moved)[:4]}")

    def blocked(plant, d, model):
        return any(plant == p and lo <= d <= hi and (ms is None or model in ms) for p, lo, hi, ms in e["blocks"])
    for p in moved & set(e["pos"]):
        plant, d = s["loc"][p]
        model, home, rs = po[p]["model"], base[p][0], e["pos"][p]
        if blocked(plant, d, model):
            problems[eid].append(f"{p} moved into a blocked day")
        if plant == home:
            if d < rs:
                problems[eid].append(f"{p} moved before the restart day")
            earlier = [x for x in range(max(rs, 0), d) if not blocked(home, x, model) and len(s["slots"][(home, x)]) < slots[home]]
            if earlier:
                problems[eid].append(f"{p} not at the earliest feasible day: free slot(s) remain on day idx {earlier[:3]}")
        else:
            free_home = [x for x in range(max(rs, 0), 20) if not blocked(home, x, model) and len(s["slots"][(home, x)]) < slots[home]]
            if free_home:
                problems[eid].append(f"{p} transferred although home plant {home} still has free days {free_home[:3]}")
            if model not in builds[plant]:
                problems[eid].append(f"{p} transferred to {plant}, which does not build {model}")
            notes[eid].append(f"transfer {p} {home}->{plant} day idx {base[p][1]}->{d}")
    over = [(k, len(v)) for k, v in s["slots"].items() if len(v) > slots[k[0]]]
    if over:
        problems[eid].append(f"over capacity {over[:3]}")
    got = {d: v for d, v in s["notified"].items() if v}
    if got != {d: v for d, v in e["notified"].items() if v}:
        problems[eid].append("dealer notifications do not match the affected customer orders")
    per = Counter(c["args"]["dealer_id"] for c in calls if c["tool"] == "dealer.notify")
    if any(n != 1 for n in per.values()):
        problems[eid].append(f"dealer notified more than once: {[d for d, n in per.items() if n != 1][:3]}")
    if s["loaned"] != e["loaned"]:
        problems[eid].append(f"loaners {len(s['loaned'])} vs expected {len(e['loaned'])}: "
                             f"extra {sorted(s['loaned'] - e['loaned'])[:3]} missing {sorted(e['loaned'] - s['loaned'])[:3]}")
    if s["held"] != e["lots"]:
        problems[eid].append(f"held {sorted(s['held'])} vs {sorted(e['lots'])}")
    if s["stopped"] != {v for v, _ in e["unsold"]}:
        problems[eid].append(f"stop-sale {len(s['stopped'])} vs {len(e['unsold'])}")
    if s["customers"] != e["owners"]:
        problems[eid].append(f"customers contacted {len(s['customers'])} vs owners {len(e['owners'])}")
    ncust = Counter(c["args"]["customer_id"] for c in calls if c["tool"] == "crm.notify_customer")
    if any(n > 1 for n in ncust.values()):
        problems[eid].append("an owner was contacted more than once")
    empty = [c["tool"] for c in calls if "message" in c["args"] and not c["args"]["message"].strip()]
    if empty:
        problems[eid].append(f"empty messages in {Counter(empty)}")
    # rule 9 per incident (attribute calls to the incident whose records they touch)
    for i, inc in enumerate(key):
        if inc[0] == "quality":
            kinds = {c["tool"] for c in writes if c["tool"] in ("quality.hold_lot", "dealer.stop_sale", "crm.notify_customer")}
        else:
            ip = e["inc_pos"][i]
            icos = {c["customer_order_id"] for c in cos if c["production_order_id"] in ip}
            idealers = {c["dealer_id"] for c in cos if c["production_order_id"] in ip}
            kinds = set()
            for c in writes:
                a = c["args"]
                if c["tool"].startswith("mes.") and a["production_order_id"] in ip:
                    kinds.add("move")
                elif c["tool"] == "dealer.notify" and a["dealer_id"] in idealers:
                    kinds.add("dealer.notify")
                elif c["tool"] == "crm.assign_loaner" and a["customer_order_id"] in icos:
                    kinds.add("crm.assign_loaner")
        if len(kinds) < 3:
            problems[eid].append(f"incident {inc[:2]} has only {len(kinds)} kinds of action: {sorted(kinds)}")
    notes[eid].append(f"{len(moved)} moved, {sum(map(len, got.values()))} COs notified at {len(got)} dealers, "
                      f"{len(s['loaned'])}/{e['n_entitled']} entitled got loaners, held {sorted(s['held'])}, "
                      f"{len(s['stopped'])} stop-sale, {len(s['customers'])} owners")

for eid in KEY:
    status = "OK  " if not problems[eid] else "FAIL"
    print(f"{status} {eid}  " + ("; ".join(notes[eid])[:230] if not problems[eid] else ""))
    for p in problems[eid]:
        print("       -", p)
extra = sorted(set(by_ev) - set(KEY))
if extra:
    print("calls for unknown events:", extra)
print("\nTOTAL:", "ALL RULES HOLD" if not any(problems.values()) else f"{sum(map(len, problems.values()))} problem(s)",
      f"| {len(rows)} calls, {sum(not r['ok'] for r in rows)} failed")
sys.exit(0 if not any(problems.values()) else 1)
