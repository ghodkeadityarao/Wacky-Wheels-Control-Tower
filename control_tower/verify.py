"""Proof before action.

`oracle()` recomputes the handbook outcome straight from the CSV rows (no graph, no planner code), and
`check()` compares it with the final state of the digital twin after a dry run of the plan. The plan is
only sent to the live server if every invariant holds. Two independent implementations have to agree.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date

from .kg import KG, LAST_IDX, date_to_idx, working_idx_on_or_after, working_idx_on_or_before

WRITE_TOOLS = {"mes.reschedule", "mes.transfer", "dealer.notify", "dealer.stop_sale", "crm.assign_loaner",
               "crm.notify_customer", "quality.hold_lot"}
TIER_RANK = {"Platinum": 0, "Gold": 1}


def oracle(kg: KG, incidents) -> dict:
    t = kg.tables
    bom = defaultdict(set)
    for r in t["bom"]:
        bom[r["model"]].add(r["part_id"])
    pos = {r["production_order_id"]: r for r in t["production_orders"]}
    idx = {p: date_to_idx(date.fromisoformat(r["build_date"])) for p, r in pos.items()}
    exp = {"pos": set(), "blocks": [], "restart": {}, "lots": set(), "unsold": set(), "owners": set()}
    for g in incidents:
        b = g.bindings
        if g.kind == "supplier_delay":
            sh = next(r for r in t["inbound_shipments"] if r["shipment_id"] == b["shipment"])
            lo = working_idx_on_or_after(date.fromisoformat(sh["eta_date"]))
            hi = working_idx_on_or_after(date.fromisoformat(b["new_eta"]))
            models = {m for m, ps in bom.items() if sh["part_id"] in ps}
            hit = {p for p, r in pos.items() if r["plant_id"] == sh["plant_id"] and r["model"] in models
                   and max(lo, 0) <= idx[p] < hi}
            exp["blocks"].append((sh["plant_id"], lo, hi - 1, models))
        elif g.kind == "plant_outage":
            lo = working_idx_on_or_after(date.fromisoformat(b["start"]))
            hi_inc = working_idx_on_or_before(date.fromisoformat(b["end"]))
            hi = hi_inc + 1
            hit = {p for p, r in pos.items() if r["plant_id"] == b["plant"] and max(lo, 0) <= idx[p] < hi}
            exp["blocks"].append((b["plant"], lo, hi_inc, None))
        else:
            for lot_id in b["lots"]:
                lot = next(r for r in t["part_lots"] if r["lot_id"] == lot_id)
                exp["lots"].add(lot_id)
                for v in t["finished_vehicles"]:
                    if (v["plant_id"] == lot["plant_id"] and lot["first_build_date"] <= v["build_date"] <= lot["last_build_date"]
                            and lot["part_id"] in bom[v["model"]]):
                        (exp["unsold"] if v["status"] == "in_stock" else exp["owners"]).add(
                            v["vin"] if v["status"] == "in_stock" else v["customer_id"])
            continue
        for p in hit:
            exp["restart"][p] = max(exp["restart"].get(p, -99), hi)
        exp["pos"] |= hit
    cust = {r["customer_id"]: r for r in t["customers"]}
    pool = {r["dealer_id"]: int(r["loaners_available"]) for r in t["loaners"]}
    exp["notified"] = defaultdict(set)
    entitled = defaultdict(list)
    for co in t["customer_orders"]:
        if co["production_order_id"] in exp["pos"]:
            exp["notified"][co["dealer_id"]].add(co["customer_order_id"])
            tier = cust[co["customer_id"]]["tier"]
            if tier in TIER_RANK:
                entitled[co["dealer_id"]].append((TIER_RANK[tier], co["promised_date"], co["customer_order_id"], co["customer_id"]))
    exp["loaned"] = set()
    for d, rows in entitled.items():
        seen, kept = set(), []
        for r in sorted(rows):
            if r[3] not in seen:
                seen.add(r[3])
                kept.append(r[2])
        exp["loaned"] |= set(kept[:pool[d]])
    exp["entitled"] = sum(len(v) for v in entitled.values())
    return exp


class Report:
    def __init__(self):
        self.checks = []

    def add(self, name, ok, detail=""):
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    @property
    def ok(self):
        return all(c["ok"] for c in self.checks)

    def failures(self):
        return [c for c in self.checks if not c["ok"]]


def check(kg: KG, incidents, calls: list[dict], state: dict, base_loc: dict) -> Report:
    rep = Report()
    exp = oracle(kg, incidents)
    ok_calls = [c for c in calls if c["ok"]]
    rep.add("no failed calls in dry run", all(c["ok"] for c in calls),
            f"{sum(not c['ok'] for c in calls)} failed" if any(not c["ok"] for c in calls) else "")
    if not incidents:
        rep.add("no write actions", not any(c["tool"] in WRITE_TOOLS for c in calls))
        return rep
    moved = {p for p, loc in state["loc"].items() if base_loc.get(p) != loc}
    rep.add("moved exactly the affected production orders", moved == exp["pos"],
            f"extra={sorted(moved - exp['pos'])[:5]} missing={sorted(exp['pos'] - moved)[:5]} (n={len(exp['pos'])})")
    over = [(k, len(v)) for k, v in state["slots"].items() if len(v) > kg.slots(k[0])]
    rep.add("no plant-day over capacity", not over, str(over[:3]))

    def blocked(plant, i, model):
        return any(plant == p and lo <= i <= hi and (ms is None or model in ms) for p, lo, hi, ms in exp["blocks"])
    bad, early, lazy = [], [], []
    for p in moved:
        plant, i = state["loc"][p]
        model = kg.attr(f"ProductionOrder:{p}")["model"]
        if blocked(plant, i, model):
            bad.append(p)
        if plant == base_loc[p][0]:
            if i < exp["restart"][p]:
                early.append(p)
        else:  # a transfer is only allowed when the home plant has no free slot from restart onwards
            home = base_loc[p][0]
            free = [d for d in range(max(exp["restart"][p], 0), LAST_IDX + 1)
                    if not blocked(home, d, model) and len(state["slots"][(home, d)]) < kg.slots(home)]
            if free:
                lazy.append((p, free[:2]))
    rep.add("no order moved into a disrupted day", not bad, str(bad[:5]))
    rep.add("same-plant moves are at/after the restart day", not early, str(early[:5]))
    rep.add("transfers only when the home plant is full", not lazy, str(lazy[:3]))
    notified = {d: v for d, v in state["notified"].items() if v}
    rep.add("dealer notices list exactly the affected customer orders",
            notified == {d: v for d, v in exp["notified"].items() if v},
            f"dealers expected={len(exp['notified'])} got={len(notified)}")
    per_dealer = Counter(c["args"]["dealer_id"] for c in ok_calls if c["tool"] == "dealer.notify")
    rep.add("exactly one notice per dealer", all(n == 1 for n in per_dealer.values()), str(per_dealer.most_common(2)))
    rep.add("loaners match pool limits and priority", state["loaned"] == exp["loaned"],
            f"expected {len(exp['loaned'])} of {exp['entitled']} entitled; got {len(state['loaned'])}")
    rep.add("lots held", state["held"] == exp["lots"], f"{sorted(state['held'])} vs {sorted(exp['lots'])}")
    rep.add("stop-sale exactly the unsold affected vehicles", state["stopped"] == exp["unsold"],
            f"{len(state['stopped'])} vs {len(exp['unsold'])}")
    rep.add("owners of sold affected vehicles contacted (and nobody else)", state["customers"] == exp["owners"],
            f"{len(state['customers'])} vs {len(exp['owners'])}")
    kinds = {c["tool"] for c in ok_calls if c["tool"] in WRITE_TOOLS}
    rep.add("at least three kinds of action per real incident", len(kinds) >= 3, str(sorted(kinds)))
    return rep
