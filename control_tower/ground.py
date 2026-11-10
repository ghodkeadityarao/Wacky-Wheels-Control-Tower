"""Grounding: the knowledge graph decides which reading of a message is real.

Each reading (from the rule reader and/or the LLM) is expanded into concrete hypotheses: every
candidate id x every candidate date under a message-wide date convention. A hypothesis survives only
if it binds to records that exist and agree (a shipment with that supplier, part, plant and ETA; a
lot whose build window matches; a plant with an outage window that overlaps the plan). The result:

* exactly one surviving binding -> act on it
* operational wording but nothing binds, or several bindings conflict -> escalate (human in the loop)
* no operational entity at all -> noise, do nothing
* hedged / rumor wording -> escalate, with what *would* be touched if it were true
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date

from .kg import KG, LAST_IDX, START, idx_to_date, key_of, nid
from .nlp import CONVENTIONS, Reading

OPERATIONAL_KINDS = ("supplier_delay", "plant_outage", "quality_hold")
WINDOW_END = idx_to_date(LAST_IDX)


def _overlap(a: str, b: str) -> float:
    ta, tb = set(a.lower().split()), set(b.lower().split())
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _entities(r: Reading) -> set:
    return set(r.plants) | set(r.parts) | set(r.suppliers) | set(r.shipments) | set(r.lots)


def _bound(g) -> set:
    out = set()
    for v in g.bindings.values():
        if isinstance(v, str):
            out.add(v)
        elif isinstance(v, list):
            out |= {x for x in v if isinstance(x, str)}
    return out


def _missing(r: Reading) -> list:
    return [w for w, ok in (("a specific part", r.parts), ("a plant", r.plants), ("any date", r.dates),
                            ("a shipment or lot", r.shipments or r.lots)) if not ok]


def _suspected(r: Reading):
    if r.kind in OPERATIONAL_KINDS:
        return r.kind
    if r.cues:
        return max(r.cues, key=lambda k: len(r.cues[k]))
    return None


@dataclass
class Grounded:
    kind: str
    key: tuple
    bindings: dict
    checks: list
    convention: str
    source: str
    segment_text: str

    def to_json(self):
        return {"kind": self.kind, "key": list(self.key), "bindings": self.bindings, "checks": self.checks,
                "convention": self.convention, "source": self.source, "text": self.segment_text}


@dataclass
class ReadingResult:
    reading: Reading
    status: str                       # grounded | ambiguous | failed | noise | unconfirmed
    grounded: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

    def to_json(self):
        return {"reading": self.reading.to_json(), "status": self.status,
                "grounded": [g.to_json() for g in self.grounded], "reasons": self.reasons}


@dataclass
class Verdict:
    incidents: list                   # list[Grounded] to act on
    escalations: list                 # list[str] reasons for a human
    noise_reasons: list
    convention: str
    convention_note: str
    results: list                     # list[ReadingResult] under the chosen convention
    conflicts: list
    review: list = field(default_factory=list)          # structured escalation items, for a human
    convention_reason: dict = field(default_factory=dict)

    @property
    def category(self) -> list[str]:
        if self.incidents:
            return sorted({g.kind for g in self.incidents})
        if self.escalations:
            return ["unconfirmed"]
        return ["noise"]

    def to_json(self):
        return {"category": self.category, "convention": self.convention, "convention_note": self.convention_note,
                "incidents": [g.to_json() for g in self.incidents], "escalations": self.escalations,
                "noise_reasons": self.noise_reasons, "conflicts": self.conflicts,
                "review": self.review, "convention_reason": self.convention_reason,
                "readings": [r.to_json() for r in self.results]}


class Grounder:
    def __init__(self, kg: KG):
        self.kg = kg
        self.shipments = [kg.attr(n) | {"id": key_of(n)} for n in kg.nodes("Shipment")]
        self.lots = [kg.attr(n) | {"id": key_of(n)} for n in kg.nodes("Lot")]

    # ---------------------------------------------------------------- hypotheses
    def _hypotheses(self, r: Reading, convention: str):
        opt = lambda s: sorted(s) or [None]
        roles = list(r.dates)
        date_opts = [r.dates[role].resolve(convention) for role in roles]
        # a vague part word ("harness") is expanded into its candidates; the records decide which is real
        parts = r.parts or {p for v in r.vague_parts for p in v["could_be"]}
        for plant, part, sup in itertools.product(opt(r.plants), opt(parts), opt(r.suppliers)):
            for combo in itertools.product(*date_opts) if date_opts else [()]:
                labels = {roles[i]: combo[i][0] for i in range(len(roles))}
                dates = {roles[i]: combo[i][1] for i in range(len(roles))}
                yield {"plant": plant, "part": part, "supplier": sup, "dates": dates, "labels": labels}

    def ground_reading(self, r: Reading, convention: str) -> ReadingResult:
        if r.kind == "noise":
            return ReadingResult(r, "noise", reasons=[r.rationale or "no operational content"])
        if r.kind == "unconfirmed" or r.unconfirmed:
            return ReadingResult(r, "unconfirmed", reasons=self._rumor_reasons(r))
        fn = {"supplier_delay": self._delay, "plant_outage": self._outage, "quality_hold": self._quality}[r.kind]
        found, fails = {}, []
        for h in self._hypotheses(r, convention):
            g, why = fn(r, h)
            if g:
                g.convention = convention
                found.setdefault(g.key, g)
            else:
                fails.append(why)
        if len(found) == 1:
            return ReadingResult(r, "grounded", list(found.values()))
        if found:
            return ReadingResult(r, "ambiguous", list(found.values()),
                                 [f"{len(found)} different records fit this message: {[g.key for g in found.values()]}"])
        uniq = list(dict.fromkeys(fails)) or ["no hypothesis could be formed"]
        return ReadingResult(r, "failed", reasons=uniq[:6])

    def _rumor_reasons(self, r: Reading) -> list[str]:
        out = [f"wording is hedged or unconfirmed ({', '.join(r.unconfirmed) or r.rationale})"]
        missing = [w for w, ok in (("a specific part", r.parts), ("a plant", r.plants),
                                   ("any date", r.dates), ("a shipment or lot", r.shipments or r.lots)) if not ok]
        if missing:
            out.append("missing: " + ", ".join(missing))
        for v in r.vague_parts:
            out.append(f"'{v['surface']}' could mean any of {v['could_be']}")
        return out

    # ---------------------------------------------------------------- per kind
    def _delay(self, r: Reading, h):
        d = h["dates"]
        orig, new = d.get("original_eta"), d.get("new_eta")
        if new is None:
            return None, "no new ETA stated"
        if r.shipments:
            cands = [s for s in self.shipments if s["id"] in r.shipments]
        elif h["part"]:
            cands = [s for s in self.shipments if s["part"] == h["part"]]
        else:
            return None, "no part or shipment named"
        checks = []
        for fld, val, label in (("part", h["part"], "part"), ("plant", h["plant"], "plant"),
                                ("supplier", h["supplier"], "supplier")):
            if val:
                cands = [s for s in cands if s[fld] == val]
                checks.append(f"{label} {val} matches")
        if orig:
            cands = [s for s in cands if s["eta"] == orig.isoformat()]
            checks.append(f"original ETA {orig} matches")
        else:
            cands = [s for s in cands if START.isoformat() <= s["eta"] < new.isoformat()]
            if len(cands) > 1:
                return None, f"original ETA not stated and {len(cands)} shipments could be meant"
        if len(cands) != 1:
            return None, (f"no shipment matches part={h['part']} plant={h['plant']} supplier={h['supplier']} "
                          f"eta={orig}" if not cands else f"{len(cands)} shipments match")
        s = cands[0]
        eta = date.fromisoformat(s["eta"])
        if new <= eta:
            return None, f"new ETA {new} is not after the original ETA {eta}"
        if eta > WINDOW_END:
            return None, f"original ETA {eta} is outside the planning window"
        checks.insert(0, f"Shipment {s['id']} exists ({s['supplier']} -> {s['part']} -> {s['plant']}, ETA {s['eta']})")
        checks.append(f"new ETA {new} is after original ETA {eta}")
        b = {"shipment": s["id"], "supplier": s["supplier"], "part": s["part"], "plant": s["plant"],
             "original_eta": s["eta"], "new_eta": new.isoformat(), "date_readings": h["labels"]}
        return Grounded("supplier_delay", ("supplier_delay", s["id"], new.isoformat()), b, checks, "", r.source,
                        r.text), ""

    def _outage(self, r: Reading, h):
        d = h["dates"]
        plant, start, end = h["plant"], d.get("start"), d.get("end")
        if not plant:
            return None, "no plant named"
        if not (start and end):
            return None, "outage dates not stated"
        if end < start:
            return None, f"outage end {end} is before start {start}"
        if end < START or start > WINDOW_END:
            return None, f"outage {start}..{end} is outside the planning window"
        checks = [f"Plant {plant} exists", f"outage window {start}..{end} is ordered and overlaps the plan"]
        notes = [m.note for m in r.dates.values() if m.note]
        if notes:
            return None, "; ".join(notes)
        b = {"plant": plant, "start": start.isoformat(), "end": end.isoformat(), "date_readings": h["labels"]}
        return Grounded("plant_outage", ("plant_outage", plant, start.isoformat(), end.isoformat()), b, checks, "",
                        r.source, r.text), ""

    def _quality(self, r: Reading, h):
        d = h["dates"]
        start, end = d.get("start"), d.get("end")
        checks = []
        if r.lots:
            cands = [l for l in self.lots if l["id"] in r.lots]
            if len(cands) != len(r.lots):
                return None, f"unknown lot id(s) {sorted(r.lots)}"
            for fld, val in (("part", h["part"]), ("plant", h["plant"]), ("supplier", h["supplier"])):
                bad = [l["id"] for l in cands if val and l[fld] != val]
                if bad:
                    return None, f"lot {bad} has {fld} != {val}"
                if val:
                    checks.append(f"lot {fld} {val} matches")
            if start and end and (min(l["first"] for l in cands) != start.isoformat()
                                  or max(l["last"] for l in cands) != end.isoformat()):
                return None, f"stated window {start}..{end} differs from the lot's build window"
        else:
            if not (h["part"] and h["plant"]):
                return None, "quality report without a specific part and plant"
            if not (start and end):
                return None, "no build window stated"
            cands = [l for l in self.lots if l["part"] == h["part"] and l["plant"] == h["plant"]
                     and (not h["supplier"] or l["supplier"] == h["supplier"])
                     and l["last"] >= start.isoformat() and l["first"] <= end.isoformat()]
            if not cands:
                return None, (f"no lot of {h['part']} at {h['plant']}"
                              f"{' from ' + h['supplier'] if h['supplier'] else ''} overlaps {start}..{end}")
            cands.sort(key=lambda l: l["first"])
            if cands[0]["first"] != start.isoformat() or cands[-1]["last"] != end.isoformat():
                return None, (f"window {start}..{end} does not line up with lot boundaries "
                              f"{[(l['id'], l['first'], l['last']) for l in cands]}")
            checks += [f"part {h['part']} at plant {h['plant']}"] + ([f"supplier {h['supplier']}"] if h["supplier"] else [])
        if start and start > START:
            return None, f"build window starts {start}, in the future"
        lots = sorted(l["id"] for l in cands)
        checks.insert(0, "lot(s) " + ", ".join(f"{l['id']} ({l['supplier']} {l['part']} @ {l['plant']} "
                                              f"{l['first']}..{l['last']})" for l in cands))
        b = {"lots": lots, "part": cands[0]["part"], "plant": cands[0]["plant"], "supplier": cands[0]["supplier"],
             "first": min(l["first"] for l in cands), "last": max(l["last"] for l in cands),
             "date_readings": h["labels"]}
        return Grounded("quality_hold", ("quality_hold", *lots), b, checks, "", r.source, r.text), ""

    # ---------------------------------------------------------------- message level
    def _decide(self, readings: list[Reading], convention: str):
        results = [self.ground_reading(r, convention) for r in readings]
        incidents: dict = {}
        for res in results:
            if res.status == "grounded":
                incidents.setdefault(res.grounded[0].key, res.grounded[0])
        # contradictions: same kind and same anchor bound to different keys by different readers
        conflicts = []
        by_anchor: dict = {}
        for g in incidents.values():
            anchor = (g.kind, g.bindings.get("plant"), g.bindings.get("part"))
            by_anchor.setdefault(anchor, []).append(g)
        for anchor, gs in by_anchor.items():
            if len(gs) > 1:
                conflicts.append(f"readers disagree on {anchor}: {[g.key for g in gs]}")
                for g in gs:
                    incidents.pop(g.key, None)
        escalations, noise = list(conflicts), []
        review = [{"source": "readers", "status": "conflict", "kind": None, "hedges": [], "missing": [], "vague": [],
                   "reasons": [c], "text": ""} for c in conflicts]

        def flag(res, status, line, reasons=None):
            r = res.reading
            escalations.append(line)
            review.append({"source": r.source, "status": status, "kind": _suspected(r),
                           "hedges": [h for h in r.unconfirmed if not h.startswith("llm")],
                           "missing": _missing(r), "vague": r.vague_parts,
                           "reasons": reasons if reasons is not None else res.reasons, "text": r.text})
        # 1) a lexical rumor cue (rules reader) vetoes any incident grounded from the same text: rule 8
        for res in results:
            r = res.reading
            if res.status == "unconfirmed" and not r.source.startswith("llm"):
                for k, g in list(incidents.items()):
                    if _overlap(g.segment_text, r.text) > 0.5:
                        incidents.pop(k)
                        flag(res, "veto", f"[{r.source}] vetoed {g.key}: the text is hedged/unconfirmed",
                             [f"a matching record was found ({g.key[1]}), but the wording is hedged or unconfirmed"])
        grounded_kinds = {g.kind for g in incidents.values()}
        for res in results:
            r = res.reading
            llm = r.source.startswith("llm")
            if res.status == "noise":
                noise.append(f"[{r.source}] " + "; ".join(res.reasons))
            elif res.status == "unconfirmed":
                # 2) an LLM 'not confirmed' vote only stands if nothing in the message grounds
                if llm and incidents:
                    continue
                flag(res, "unconfirmed", f"[{r.source}] unconfirmed: " + "; ".join(res.reasons))
            elif res.status in ("failed", "ambiguous"):
                # 3) a reader that failed is overruled when another reader grounded the same kind
                if r.kind in grounded_kinds and any(g.source != r.source for g in incidents.values()):
                    continue
                # 3b) an LLM reading that fails on a sentence another reader already grounded is a
                #     misclassification of that sentence (small models do this), but only if it names
                #     nothing the grounded incident does not already cover; a different plant, part or
                #     supplier may be a second incident and still goes to a human
                if llm and any(g.source != r.source and _overlap(g.segment_text, r.text) > 0.5
                               and _entities(r) <= _bound(g) for g in incidents.values()):
                    continue
                flag(res, res.status, f"[{r.source}] {r.kind} could not be grounded: " + "; ".join(res.reasons))
        seen, uniq = set(), []
        for it in review:
            k = (it["source"], it["status"], it["text"], tuple(it["reasons"]))
            if k not in seen:
                seen.add(k)
                uniq.append(it)
        return list(incidents.values()), list(dict.fromkeys(escalations)), noise, results, conflicts, uniq

    def decide(self, readings: list[Reading]) -> Verdict:
        tried = {}
        for conv in CONVENTIONS:
            tried[conv] = self._decide(readings, conv)
        def failures(res):
            return sum(r.status in ("failed", "ambiguous") for r in res[3])
        chosen = next((c for c in CONVENTIONS if failures(tried[c]) == 0), CONVENTIONS[0])
        other = [c for c in CONVENTIONS if c != chosen][0]
        keys = lambda res: sorted(g.key for g in res[0])
        if keys(tried[chosen]) == keys(tried[other]) and failures(tried[other]) == failures(tried[chosen]):
            note = "relative dates in this message read the same under every convention"
            why = {"basis": "same"}
        elif failures(tried[other]) > failures(tried[chosen]):
            bad = [r.reasons for r in tried[other][3] if r.status in ("failed", "ambiguous")]
            note = (f"'{chosen}' chosen: under '{other}' part of the message fails to ground {bad[:1]} "
                    f"(evidence for the '{chosen}' reading)")
            why = {"basis": "evidence", "other": other, "other_fails": bad[0][0] if bad and bad[0] else ""}
        else:
            note = (f"'{chosen}' chosen by default (house convention: 'next <weekday>' = that day next week, "
                    f"corroborated where the data can tell); '{other}' would give {keys(tried[other])}")
            why = {"basis": "default", "other": other, "other_keys": [list(k) for k in keys(tried[other])]}
        inc, esc, noise, results, conflicts, review = tried[chosen]
        return Verdict(inc, esc, noise, chosen, note, results, conflicts, review, why)
