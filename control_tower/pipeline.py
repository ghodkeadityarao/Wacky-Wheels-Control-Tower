"""Per-event pipeline: understand -> ground -> compile -> prove on twin -> execute -> explain."""
from __future__ import annotations

import time
from collections import Counter

from .agents import Audit, Coordinator
from .clients import TwinClient
from .kg import idx_to_date, key_of, nid
from .verify import WRITE_TOOLS, check

MAX_EDGES = 4000

KIND_WORDS = {"supplier_delay": "a supplier delay", "plant_outage": "a plant outage", "quality_hold": "a quality problem"}
NEEDS = {
    "supplier_delay": "which part (or the shipment id), the receiving plant or the supplier, the original ETA and the new ETA",
    "plant_outage": "which plant, and the first and last day it cannot build",
    "quality_hold": "which part, the plant, and the build window (or the lot id)",
    None: "what happened, to which part or plant, and on which dates",
}
ROLE = {"original_eta": "original ETA", "new_eta": "new ETA", "start": "first day", "end": "last day"}
_FAIL = [("no plant named", "No plant is named."), ("outage dates not stated", "No outage dates are given."),
         ("no new ETA stated", "No new arrival date is given."), ("no part or shipment named", "No part or shipment is named."),
         ("quality report without a specific part and plant", "No specific part and plant are named."),
         ("no build window stated", "No build window is given."),
         ("no shipment matches", "No inbound shipment matches the part, plant, supplier and date given.")]


def _join(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " or " + xs[-1]


def _plain_fail(reason: str) -> str:
    for key, text in _FAIL:
        if reason.startswith(key):
            return text
    return reason[:1].upper() + reason[1:] + ("" if reason.endswith(".") else ".")


def plain_review(verdict, kg) -> list[dict]:
    """Escalation reasons a non-engineer can act on: what is wrong, and what we would need to act."""
    items, seen = [], set()
    for it in verdict.review:
        st, kind, why = it["status"], it.get("kind"), []
        if st == "unconfirmed":
            head = "The report is not confirmed" + (f" (it sounds like {KIND_WORDS[kind]})" if kind in KIND_WORDS else "")
            if it["hedges"]:
                why.append("Hedged wording: " + ", ".join(f"“{h}”" for h in it["hedges"][:5]) + ".")
            if it["missing"]:
                why.append("It does not name " + _join(it["missing"]) + ".")
        elif st == "veto":
            head, why = "A matching record exists, but the report is hedged", list(it["reasons"])
        elif st == "conflict":
            head, why = "Two readings of the message point to different records", list(it["reasons"])
        elif st == "ambiguous":
            head, why = f"Several records fit this {KIND_WORDS.get(kind, 'report').split(' ', 1)[-1]}", list(it["reasons"])
        else:
            head = f"It reads like {KIND_WORDS.get(kind, 'an incident')}, but it does not match our records"
            why = list(dict.fromkeys(_plain_fail(r) for r in it["reasons"]))
        for vg in it["vague"]:
            names = [kg.attr(nid("Part", p)).get("name", p) if kg.has(nid("Part", p)) else p for p in vg["could_be"]]
            why.append(f"“{vg['surface']}” could mean any of: " + ", ".join(names) + ".")
        k = (head, tuple(why))
        if k not in seen:
            seen.add(k)
            items.append({"headline": head, "why": why, "need": NEEDS.get(kind if kind in NEEDS else None),
                          "source": it["source"], "text": it["text"]})
    return items


def plain_dates(verdict) -> dict:
    """Every date phrase behind a grounded incident, the date it resolved to, and why that reading."""
    out = []
    for g in verdict.incidents:
        r = next((res.reading for res in verdict.results if res.status == "grounded" and res.reading.source == g.source
                  and res.reading.text == g.segment_text), None)
        if r is None:
            continue
        labels = g.bindings.get("date_readings", {})
        for role, m in r.dates.items():
            d = m.candidates.get(labels.get(role))
            if d is None:
                continue
            alts = sorted({x for x in m.candidates.values() if x != d})
            out.append({"incident": g.kind, "phrase": m.phrase, "role": ROLE.get(role, role),
                        "date": d.strftime("%a %Y-%m-%d"), "alternative": [a.strftime("%a %Y-%m-%d") for a in alts]})
    why, expl = verdict.convention_reason, ""
    if any(x["alternative"] for x in out):
        if why.get("basis") == "evidence":
            expl = ("Only this reading matches our records. Read the other way: "
                    + (why.get("other_fails") or "part of the message no longer matches anything") + ".")
        elif why.get("basis") == "default":
            expl = ("Our records fit either reading, so we use the house convention: “next <weekday>” is that weekday "
                    "next week, the reading the records confirm wherever they can tell the two apart.")
    return {"dates": out, "why": expl}


def plain_escalation(eid: str, items: list[dict], extra: list[str]) -> str:
    parts = [f"{eid} needs human review."]
    for i in items:
        parts.append(f"{i['headline']}. " + " ".join(i["why"]) + f" To act we need {i['need']}.")
    parts += extra
    return " ".join(parts)


def _edges_json(ev):
    seen, out = set(), []
    for e in ev.edges:
        if e not in seen:
            seen.add(e)
            out.append(list(e))
    return out[:MAX_EDGES]


def _example_path(kg, imp):
    """One concrete end-to-end chain, for the headline 'graph path' citation."""
    e = imp.evidence.edges
    k = imp.grounded.kind
    if k == "quality_hold":
        lot = f"Lot:{imp.lots[0]}"
        po = next((b for a, r, b in e if a == lot and r == "INSTALLED_IN"), None)
        v = next((b for a, r, b in e if a == po and r == "PRODUCED"), None)
        if not v:
            return []
        va = kg.attr(v)
        tail = f"Customer:{va['customer']}" if va["status"] == "sold" else f"Dealer:{va['dealer']}"
        return [lot, po, v, tail]
    po = f"ProductionOrder:{imp.pos[0]}" if imp.pos else None
    co = next((a for a, r, b in e if b == po and r == "FULFILLED_BY"), None)
    if not co:
        return []
    dealer = next(b for a, r, b in e if a == co and r == "SOLD_THROUGH")
    cust = next(b for a, r, b in e if a == co and r == "PLACED_BY")
    if k == "supplier_delay":
        sh = f"Shipment:{imp.grounded.bindings['shipment']}"
        part = f"Part:{imp.grounded.bindings['part']}"
        model = f"Model:{kg.attr(po)['model']}"
        return [sh, part, model, po, co, dealer, cust]
    return [f"Plant:{imp.plant}", po, co, dealer, cust]


def summarize(event, verdict, impacts, moves, executed, escalated, unplaced, review=None) -> str:
    if not verdict.incidents and not escalated:
        why = verdict.noise_reasons[0] if verdict.noise_reasons else "no operational content"
        why = why.split("] ", 1)[-1]
        return f"No action. Not about our operations: {why}."
    if not verdict.incidents:
        if review:
            return ("No operational change; escalated to a human. " + " ".join(
                f"{i['headline']}. " + " ".join(i["why"]) for i in review))[:700]
        return "No operational change. Escalated to a human: " + "; ".join(verdict.escalations)[:600]
    parts = []
    for imp in impacts:
        b = imp.grounded.bindings
        if imp.grounded.kind == "supplier_delay":
            parts.append(f"Supplier delay: shipment {b['shipment']} ({b['part']} for {b['plant']}) moves "
                         f"{b['original_eta']} -> {b['new_eta']}; {len(imp.pos)} build(s) needing the part are blocked.")
        elif imp.grounded.kind == "plant_outage":
            parts.append(f"Plant outage at {b['plant']} {b['start']}..{b['end']}; {len(imp.pos)} build(s) blocked.")
        else:
            parts.append(f"Quality hold on {', '.join(b['lots'])} ({b['part']} @ {b['plant']}, built {b['first']}..{b['last']}); "
                         f"{len(imp.vehicles)} vehicles affected.")
    c = Counter(r["tool"] for r in executed if r.get("ok") and r["tool"] in WRITE_TOOLS)
    if moves:
        tr = sum(m["type"] == "transfer" for m in moves)
        parts.append(f"Moved {len(moves)} production order(s) ({tr} transferred).")
    parts.append("Actions: " + ", ".join(f"{n} x {t}" for t, n in sorted(c.items())) + ".")
    if unplaced:
        parts.append(f"{len(unplaced)} order(s) could not be placed in the window and were escalated.")
    if escalated:
        parts.append("Escalated: " + escalated[:300])
    return " ".join(parts)


def handle_event(coord: Coordinator, event: dict, client) -> dict:
    t0 = time.time()
    audit = Audit()
    eid = event["event_id"]
    audit.log("Coordinator", "receive", f"{event.get('source')}: {event['text'][:120]}")
    verdict, readings, reader_info = coord.understand(event, audit)
    review = plain_review(verdict, coord.kg)
    trace = {"event": event, "readers": reader_info, "verdict": verdict.to_json(), "category": verdict.category,
             "incidents": [], "plan": [], "dry_run": None, "executed": [], "settings": coord.s.to_json(),
             "human_review": review, "dates_plain": plain_dates(verdict), "loaners": []}
    executed, escalation = [], ""
    impacts, moves, unplaced = [], [], []
    if verdict.incidents:
        impacts, actions, moves, unplaced = coord.compile(verdict, audit)
        trace["plan"] = [a.to_json() for a in actions]
        trace["loaners"] = coord.last_extras.get("loaners", [])
        twin = TwinClient()
        dry_id = twin.fresh(eid)
        _, dry_fail = coord.execute(twin, dry_id, actions, audit)
        rep = check(coord.kg, verdict.incidents, twin.calls, twin.state(dry_id), coord.scp.base_loc)
        trace["dry_run"] = {"ok": rep.ok, "calls": len(twin.calls), "checks": rep.checks}
        audit.log("Coordinator", "prove", f"dry run on twin: {len(twin.calls)} calls, "
                                          f"{sum(c['ok'] for c in rep.checks)}/{len(rep.checks)} invariants hold")
        if rep.ok and not dry_fail:
            executed, failures = coord.execute(client, eid, actions, audit)
            extra = []
            if unplaced:
                extra.append(f"No free slot anywhere in the planning window for {', '.join(unplaced)}; "
                             f"a planner has to decide.")
            if failures:
                extra.append(f"{len(failures)} call(s) failed even after recovery: "
                             + "; ".join(f"{f['tool']} {f['args']} -> {f.get('blocked') or f['result']}" for f in failures[:3]))
            if review or extra:
                escalation = plain_escalation(eid, review, extra)
        else:
            escalation = (f"{eid} needs human review. The plan was withheld because the dry run did not prove it safe. "
                          "Failing checks: " + "; ".join(f"{c['check']} ({c['detail']})" for c in rep.failures())
                          + (f"; {len(dry_fail)} dry-run call failures" if dry_fail else "") + ".")
    elif verdict.escalations:
        escalation = plain_escalation(eid, review, [])
    if escalation:
        executed.append(coord.escalate(client, eid, escalation, audit))
    for imp in impacts:
        trace["incidents"].append({
            "id": imp.incident_id, "kind": imp.grounded.kind, "bindings": imp.grounded.bindings,
            "grounding_checks": imp.grounded.checks, "path_schema": imp.evidence.schema,
            "example_path": _example_path(coord.kg, imp), "evidence_edges": _edges_json(imp.evidence),
            "affected_production_orders": imp.pos, "lots": imp.lots, "vehicles": len(imp.vehicles),
            "blocked": [str(idx_to_date(imp.blocked[0])), str(idx_to_date(imp.blocked[1]))] if imp.blocked else None,
            "restart": str(idx_to_date(imp.restart_idx)) if imp.restart_idx is not None and imp.restart_idx <= 19 else None,
        })
    trace["moves"] = [{**m, "from": [m["from"][0], str(idx_to_date(m["from"][1]))],
                       "to": [m["to"][0], str(idx_to_date(m["to"][1]))]} for m in moves]
    trace["unplaced"] = unplaced
    trace["executed"] = executed
    trace["escalation"] = escalation
    trace["summary"] = summarize(event, verdict, impacts, moves, executed, escalation, unplaced, review)
    trace["audit"] = audit.rows
    trace["stats"] = {"seconds": round(time.time() - t0, 2),
                      "live_calls": sum(not r.get("blocked") and not r.get("skipped") for r in executed),
                      "failed_live_calls": sum(not r.get("ok") and not r.get("skipped") and not r.get("blocked")
                                               for r in executed),
                      "blocked_by_catalog": sum(bool(r.get("blocked")) for r in executed),
                      "reads": sum(r["tool"] in ("mes.get_schedule", "crm.loaner_availability") and r.get("ok") for r in executed),
                      "writes": sum(r["tool"] in WRITE_TOOLS and r.get("ok") for r in executed)}
    audit.log("Coordinator", "done", trace["summary"][:200])
    return trace


__all__ = ["handle_event", "key_of"]
