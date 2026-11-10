"""One coordinator and three specialist agents.

Coordinator ............ reads the message (LLM + rule reader), grounds it, routes work, proves the plan
                         on the digital twin, executes, escalates. Owns `escalate`.
SupplyChainPlanner ..... slot ledger and moves. Owns mes.get_schedule / mes.reschedule / mes.transfer.
DealerNetwork .......... dealer communications. Owns dealer.notify / dealer.stop_sale.
CustomerCareQuality .... loaners, owners, lots. Owns crm.loaner_availability / crm.assign_loaner /
                         crm.notify_customer / quality.hold_lot.

Specialists are deterministic: the handbook is policy-as-code. The LLM is used only where language is.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .config import Settings
from .ground import Grounder, Verdict
from .impact import Impact, analyse, customer_orders
from .kg import KG, LAST_IDX, idx_to_date, key_of, nid
from .llm import Backend, LLMError, LLMReader
from .nlp import Resolver, RuleReader

TIER_RANK = {"Platinum": 0, "Gold": 1, "Standard": 2}
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ToolCatalog:
    """The tools the server advertises (GET /tools). Every call is checked against it before it is sent,
    so a malformed call is caught locally instead of failing (and being counted) on the server."""

    def __init__(self, tools: list[dict], source: str):
        self.spec = {t["tool"]: t["args"] for t in tools}
        self.source = source

    def check(self, tool: str, args: dict) -> str | None:
        if tool not in self.spec:
            return f"'{tool}' is not in the server's tool catalog"
        want = self.spec[tool]
        if set(args) != set(want):
            return f"arguments {sorted(args)} do not match the catalog {sorted(want)}"
        for k, kind in want.items():
            v = args[k]
            if kind.startswith("list"):
                ok = isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)
            elif kind == "YYYY-MM-DD":
                ok = isinstance(v, str) and bool(_DATE_RX.match(v))
            else:
                ok = isinstance(v, str) and bool(v.strip())
            if not ok:
                return f"argument '{k}' is not a valid {kind}"
        return None


@dataclass
class Action:
    agent: str
    tool: str
    args: dict
    why: str
    incidents: list = field(default_factory=list)

    def to_json(self):
        return {"agent": self.agent, "tool": self.tool, "args": self.args, "why": self.why, "incidents": self.incidents}


class Audit:
    def __init__(self):
        self.rows = []

    def log(self, agent, step, detail, **extra):
        self.rows.append({"t": round(time.time(), 3), "agent": agent, "step": step, "detail": detail, **extra})


# ============================================================================ specialists
class SupplyChainPlanner:
    name = "SupplyChainPlanner"
    TOOLS = {"mes.get_schedule", "mes.reschedule", "mes.transfer"}

    def __init__(self, kg: KG, settings: Settings):
        self.kg, self.s = kg, settings
        self.base_count = defaultdict(int)
        self.base_loc = {}
        for po in kg.nodes("ProductionOrder"):
            a = kg.attr(po)
            if a["idx"] >= 0:
                self.base_count[(a["plant"], a["idx"])] += 1
                self.base_loc[key_of(po)] = (a["plant"], a["idx"])

    def plan(self, impacts: list[Impact], audit: Audit):
        """Earliest-feasible rescheduling over ONE shared ledger for every incident in the event."""
        count = defaultdict(int, self.base_count)
        blocked = lambda plant, idx, model: any(i.blocks(plant, idx, model) for i in impacts)
        owners = defaultdict(list)
        for imp in impacts:
            for po in imp.pos:
                owners[po].append(imp)
        order = sorted(owners, key=lambda p: (self.base_loc[p][1], p))
        moves, unplaced = [], []
        for po in order:
            plant, idx = self.base_loc[po]
            model = self.kg.attr(nid("ProductionOrder", po))["model"]
            restart = max(i.restart_idx for i in owners[po])
            d_start = min(i.blocked[0] for i in owners[po])
            count[(plant, idx)] -= 1
            slots = self.kg.slots(plant)
            day = next((d for d in range(max(restart, 0), LAST_IDX + 1)
                        if not blocked(plant, d, model) and count[(plant, d)] < slots), None)
            if day is not None:
                count[(plant, day)] += 1
                moves.append({"po": po, "model": model, "from": (plant, idx), "to": (plant, day), "type": "reschedule",
                              "incidents": [i.incident_id for i in owners[po]],
                              "why": f"{po} ({model}) blocked at {plant} on {idx_to_date(idx)}; earliest free "
                                     f"{plant} slot on/after restart {idx_to_date(restart) if restart <= LAST_IDX else 'beyond window'} "
                                     f"is {idx_to_date(day)} ({count[(plant, day)]}/{slots} used after move)"})
                continue
            anchor = {"today": 0, "disruption_start": max(d_start, 0), "original_date": idx,
                      "restart": restart}[self.s.transfer_anchor]
            cands = [(d, p) for d in range(max(anchor, 0), LAST_IDX + 1) for p in self.kg.plants_building(model)
                     if p != plant and not blocked(p, d, model) and count[(p, d)] < self.kg.slots(p)]
            if cands:
                d, p = min(cands)
                count[(p, d)] += 1
                moves.append({"po": po, "model": model, "from": (plant, idx), "to": (p, d), "type": "transfer",
                              "incidents": [i.incident_id for i in owners[po]],
                              "why": f"no free {plant} slot from {idx_to_date(restart) if restart <= LAST_IDX else 'restart'} "
                                     f"to {idx_to_date(LAST_IDX)}; {p} builds {model} and has a free slot on "
                                     f"{idx_to_date(d)} (earliest on/after {self.s.transfer_anchor} {idx_to_date(max(anchor, 0))})"})
            else:
                count[(plant, idx)] += 1
                unplaced.append(po)
        actions = []
        for t in sorted({m["to"] for m in moves}):
            actions.append(Action(self.name, "mes.get_schedule", {"plant_id": t[0], "date": idx_to_date(t[1]).isoformat()},
                                  "confirm live free slots before moving", []))
        for m in moves:
            args = {"production_order_id": m["po"], "new_date": idx_to_date(m["to"][1]).isoformat()}
            if m["type"] == "transfer":
                args = {"production_order_id": m["po"], "to_plant_id": m["to"][0], "new_date": args["new_date"]}
            actions.append(Action(self.name, f"mes.{m['type']}", args, m["why"], m["incidents"]))
        audit.log(self.name, "plan", f"{len(moves)} moves ({sum(m['type'] == 'transfer' for m in moves)} transfers), "
                                     f"{len(unplaced)} unplaceable", unplaced=unplaced)
        return moves, unplaced, actions, count

    def recover(self, client, event_id, action: Action, audit: Audit):
        """A move failed live: re-read the schedule and take the next genuinely free slot, once."""
        a = action.args
        plant = a.get("to_plant_id") or self.base_loc[a["production_order_id"]][0]
        start = idx_of(a["new_date"]) + 1
        for d in range(start, LAST_IDX + 1):
            ok, res = client.act(event_id, "mes.get_schedule", {"plant_id": plant, "date": idx_to_date(d).isoformat()})
            if ok and res.get("free_slots", 0) > 0:
                new = dict(a, new_date=idx_to_date(d).isoformat())
                ok2, res2 = client.act(event_id, action.tool, new)
                audit.log(self.name, "recover", f"retried {a['production_order_id']} on {new['new_date']}: {ok2}")
                return ok2, res2, new
        return False, {"error": "no free slot found on re-read"}, a


def idx_of(iso: str) -> int:
    from datetime import date
    from .kg import date_to_idx
    return date_to_idx(date.fromisoformat(iso))


class DealerNetwork:
    name = "DealerNetwork"
    TOOLS = {"dealer.notify", "dealer.stop_sale"}

    def __init__(self, kg: KG):
        self.kg = kg

    def plan_notify(self, cos: list[str], new_date_of: dict, labels: dict, audit: Audit):
        by_dealer = defaultdict(list)
        for co in cos:
            by_dealer[self.kg.attr(nid("CustomerOrder", co))["dealer"]].append(co)
        actions = []
        for dealer in sorted(by_dealer):
            ids = sorted(by_dealer[dealer])
            lines = "; ".join(f"{c} -> new build {new_date_of[c]}" for c in ids)
            msg = (f"Delay notice ({' + '.join(sorted({labels[c] for c in ids}))}): {len(ids)} of your customer "
                   f"order(s) are affected. {lines}. Platinum/Gold customers are being offered loaners where available.")
            actions.append(Action(self.name, "dealer.notify", {"dealer_id": dealer, "customer_order_ids": ids, "message": msg},
                                  f"dealer has {len(ids)} affected customer order(s); exactly one notice, listing only those",
                                  sorted({labels[c] for c in ids})))
        audit.log(self.name, "plan", f"{len(actions)} dealer notices covering {len(cos)} customer orders")
        return actions

    def plan_stop_sale(self, vins: list[str], incident_id: str, lot_label: str, audit: Audit):
        by_dealer = defaultdict(list)
        for v in vins:
            a = self.kg.attr(nid("Vehicle", v))
            if a["status"] == "in_stock":
                by_dealer[a["dealer"]].append(v)
        actions = [Action(self.name, "dealer.stop_sale", {"dealer_id": d, "vins": sorted(v)},
                          f"{len(v)} unsold vehicle(s) built with {lot_label}", [incident_id])
                   for d, v in sorted(by_dealer.items())]
        audit.log(self.name, "plan", f"stop-sale at {len(actions)} dealers for {sum(map(len, by_dealer.values()))} unsold vehicles")
        return actions


class CustomerCareQuality:
    name = "CustomerCareQuality"
    TOOLS = {"crm.loaner_availability", "crm.assign_loaner", "crm.notify_customer", "quality.hold_lot"}

    def __init__(self, kg: KG):
        self.kg = kg

    def loaner_queue(self, cos: list[str]):
        """Per dealer: entitled customer orders in policy priority, and how many the pool can serve."""
        per = defaultdict(list)
        for co in cos:
            a = self.kg.attr(nid("CustomerOrder", co))
            tier = self.kg.attr(nid("Customer", a["customer"]))["tier"]
            if tier in ("Platinum", "Gold"):
                per[a["dealer"]].append((TIER_RANK[tier], a["promised"], co, a["customer"], tier))
        out = {}
        for d, rows in per.items():
            rows.sort()
            seen, ordered = set(), []
            for r in rows:                       # one loaner per customer
                if r[3] not in seen:
                    seen.add(r[3])
                    ordered.append(r)
            out[d] = {"queue": ordered, "pool": self.kg.attr(nid("Dealer", d))["loaners"]}
        return out

    def allocation(self, cos: list[str], labels: dict) -> list[dict]:
        """Per dealer: pool, every entitled customer in policy order, and who got a loaner (for the UI)."""
        per = defaultdict(list)
        for co in cos:
            per[self.kg.attr(nid("CustomerOrder", co))["dealer"]].append(co)
        q = self.loaner_queue(cos)
        out = []
        for d in sorted(per):
            da = self.kg.attr(nid("Dealer", d))
            pool = da["loaners"]
            queue = q.get(d, {"queue": []})["queue"]
            std = sum(self.kg.attr(nid("Customer", self.kg.attr(nid("CustomerOrder", c))["customer"]))["tier"] == "Standard"
                      for c in per[d])
            out.append({"dealer": d, "dealer_name": da["name"], "pool": pool, "affected_orders": len(per[d]),
                        "standard_orders": std,
                        "entitled": [{"rank": i + 1, "customer_order": co, "customer": cust, "tier": tier,
                                      "promised": promised, "incident": labels.get(co), "loaner": i < pool}
                                     for i, (_, promised, co, cust, tier) in enumerate(queue)]})
        return out

    def plan_loaners(self, cos: list[str], labels: dict, audit: Audit):
        actions = []
        q = self.loaner_queue(cos)
        for d in sorted(q):
            actions.append(Action(self.name, "crm.loaner_availability", {"dealer_id": d}, "check pool before assigning"))
            winners = q[d]["queue"][:q[d]["pool"]]
            for rank, (_, promised, co, cust, tier) in enumerate(winners, 1):
                actions.append(Action(self.name, "crm.assign_loaner", {"customer_order_id": co},
                                      f"{tier} customer {cust}, promised {promised}; priority #{rank} of "
                                      f"{len(q[d]['queue'])} entitled at {d} (pool {q[d]['pool']})", [labels[co]]))
        n_ent = sum(len(v["queue"]) for v in q.values())
        n_win = sum(min(len(v["queue"]), v["pool"]) for v in q.values())
        audit.log(self.name, "plan", f"{n_ent} entitled Platinum/Gold customers at {len(q)} dealers; {n_win} loaners fit the pools")
        return actions

    def plan_quality(self, imp: Impact, audit: Audit):
        b = imp.grounded.bindings
        actions = [Action(self.name, "quality.hold_lot", {"lot_id": lot},
                          f"lot {lot} ({b['supplier']} {b['part']} @ {b['plant']}) reported defective", [imp.incident_id])
                   for lot in imp.lots]
        owners = defaultdict(list)
        for v in imp.vehicles:
            a = self.kg.attr(nid("Vehicle", v))
            if a["status"] == "sold" and a["customer"]:
                owners[a["customer"]].append(v)
        for cust in sorted(owners):
            vins = sorted(owners[cust])
            msg = (f"Safety/quality notice: your vehicle(s) {', '.join(vins)} were built with {b['part']} from lot "
                   f"{', '.join(imp.lots)}, now on quality hold. Your dealer will contact you to schedule an inspection.")
            actions.append(Action(self.name, "crm.notify_customer", {"customer_id": cust, "message": msg},
                                  f"owner of {len(vins)} sold affected vehicle(s)", [imp.incident_id]))
        audit.log(self.name, "plan", f"hold {len(imp.lots)} lot(s); contact {len(owners)} owners")
        return actions


# ============================================================================ coordinator
class Coordinator:
    name = "Coordinator"

    def __init__(self, kg: KG, settings: Settings | None = None, backend: Backend | None = None):
        self.kg, self.s = kg, settings or Settings()
        self.resolver = Resolver(kg)
        self.rules = RuleReader(self.resolver)
        self.llm = LLMReader(backend, self.resolver) if backend else None
        self.grounder = Grounder(kg)
        self.scp = SupplyChainPlanner(kg, self.s)
        self.dealers = DealerNetwork(kg)
        self.care = CustomerCareQuality(kg)
        self._catalogs: dict = {}
        self.last_extras: dict = {"loaners": []}

    # ---------------------------------------------------------------- understanding
    def understand(self, event: dict, audit: Audit) -> tuple[Verdict, list, dict]:
        readings, reader_info = [], {}
        rr = self.rules.read(event["text"])
        readings += rr
        reader_info["rules"] = [r.to_json() for r in rr]
        if self.llm:
            try:
                lr = self.llm.read(event["text"])
                readings += lr
                reader_info[self.llm.name] = [r.to_json() for r in lr]
                audit.log(self.name, "read", f"{self.llm.name}: {[r.kind for r in lr]}; rules: {[r.kind for r in rr]}")
            except LLMError as e:
                reader_info["llm_error"] = str(e)
                audit.log(self.name, "read", f"LLM unavailable, continuing with rule reader only: {e}")
        else:
            audit.log(self.name, "read", f"rule reader: {[r.kind for r in rr]} (no LLM configured)")
        verdict = self.grounder.decide(readings)
        audit.log(self.name, "ground", f"category={verdict.category}; {len(verdict.incidents)} grounded incident(s); "
                                       f"{len(verdict.escalations)} escalation reason(s); {verdict.convention_note}")
        return verdict, readings, reader_info

    # ---------------------------------------------------------------- planning
    def compile(self, verdict: Verdict, audit: Audit):
        incs = sorted(verdict.incidents, key=lambda g: g.key)
        impacts = [analyse(self.kg, g, f"I{i + 1}:{g.kind}") for i, g in enumerate(incs)]
        actions, moves, unplaced = [], [], []
        self.last_extras = {"loaners": []}
        sched = [i for i in impacts if i.grounded.kind != "quality_hold"]
        if sched:
            moves, unplaced, a, _ = self.scp.plan(sched, audit)
            actions += a
            labels, new_date = {}, {}
            moved_to = {m["po"]: m["to"] for m in moves}
            cos = []
            for imp in sched:
                for co in customer_orders(self.kg, imp.pos, imp.evidence):
                    labels[co] = imp.incident_id
                    po = self.kg.attr(nid("CustomerOrder", co))["po"]
                    to = moved_to.get(po)
                    new_date[co] = f"{idx_to_date(to[1])} at {to[0]}" if to else "TBD (escalated)"
                    cos.append(co)
            cos = sorted(set(cos))
            actions += self.dealers.plan_notify(cos, new_date, labels, audit)
            actions += self.care.plan_loaners(cos, labels, audit)
            self.last_extras["loaners"] = self.care.allocation(cos, labels)
        for imp in impacts:
            if imp.grounded.kind == "quality_hold":
                acts = self.care.plan_quality(imp, audit)
                holds = [a for a in acts if a.tool == "quality.hold_lot"]
                actions += holds
                actions += self.dealers.plan_stop_sale(imp.vehicles, imp.incident_id, ", ".join(imp.lots), audit)
                actions += [a for a in acts if a.tool != "quality.hold_lot"]
        return impacts, actions, moves, unplaced

    # ---------------------------------------------------------------- execution
    def catalog(self, client) -> ToolCatalog | None:
        """Tool discovery, once per client: the live server's GET /tools, or the twin's copy of the same list."""
        key = getattr(client, "base", type(client).__name__)
        if key not in self._catalogs:
            try:
                src = f"GET {client.base}/tools" if hasattr(client, "base") else "enterprise.TOOLS (digital twin)"
                self._catalogs[key] = ToolCatalog(client.tools(), src)
            except Exception:  # an unreachable /tools must not stop operations; calls are then sent unchecked
                self._catalogs[key] = None
        return self._catalogs[key]

    def execute(self, client, event_id: str, actions: list[Action], audit: Audit):
        log, failures = [], []
        live_pool = {}
        assigned = defaultdict(int)
        cat = self.catalog(client)
        for act in actions:
            bad = cat.check(act.tool, act.args) if cat else None
            if bad:
                rec = {**act.to_json(), "ok": False, "blocked": bad, "result": "not sent: failed the tool-catalog check"}
                failures.append(rec)
                log.append(rec)
                audit.log(act.agent, "blocked", f"{act.tool} not sent: {bad}")
                continue
            if act.tool == "crm.assign_loaner":
                d = self.kg.attr(nid("CustomerOrder", act.args["customer_order_id"]))["dealer"]
                if d in live_pool and assigned[d] >= live_pool[d]:
                    log.append({**act.to_json(), "ok": False, "skipped": True, "result": "live pool exhausted"})
                    audit.log(act.agent, "skip", f"pool at {d} smaller live than planned; skipped {act.args}")
                    continue
            ok, res = client.act(event_id, act.tool, act.args)
            rec = {**act.to_json(), "ok": ok, "result": res}
            if act.tool == "crm.loaner_availability" and ok:
                live_pool[act.args["dealer_id"]] = res.get("available", 0)
            if act.tool == "crm.assign_loaner" and ok:
                assigned[res.get("dealer_id")] += 1
            if not ok and act.tool in ("mes.reschedule", "mes.transfer"):
                ok2, res2, new_args = self.scp.recover(client, event_id, act, audit)
                rec.update({"recovered": ok2, "recovery_args": new_args, "recovery_result": res2})
                ok = ok2
            if not ok:
                failures.append(rec)
                audit.log(act.agent, "failed", f"{act.tool} {act.args} -> {res}")
            log.append(rec)
        return log, failures

    def escalate(self, client, event_id, reason, audit: Audit):
        ok, res = client.act(event_id, "escalate", {"reason": reason[:2000]})
        audit.log(self.name, "escalate", reason[:300], ok=ok)
        return {"agent": self.name, "tool": "escalate", "args": {"reason": reason[:2000]}, "ok": ok, "result": res,
                "why": "human in the loop"}
