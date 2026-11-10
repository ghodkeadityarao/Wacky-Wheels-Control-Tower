"""Impact analysis by graph traversal, with the walked edges kept as citable evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .ground import Grounded
from .kg import KG, LAST_IDX, date_to_idx, key_of, nid, working_idx_on_or_after, working_idx_on_or_before


@dataclass
class Evidence:
    schema: list                                   # the class-level path, e.g. Shipment -> Part -> Model ...
    edges: list = field(default_factory=list)      # concrete (src, rel, dst) triples actually walked

    def add(self, a, rel, b):
        self.edges.append((a, rel, b))


@dataclass
class Impact:
    incident_id: str
    grounded: Grounded
    evidence: Evidence
    # schedule disruptions
    plant: str | None = None
    blocked: tuple | None = None                   # (first_idx, last_idx) inclusive at `plant`
    models: set | None = None                      # None = every model at the plant
    restart_idx: int | None = None
    pos: list = field(default_factory=list)        # affected production order ids
    # quality
    lots: list = field(default_factory=list)
    vehicles: list = field(default_factory=list)   # VINs

    def blocks(self, plant: str, idx: int, model: str) -> bool:
        return (self.blocked is not None and plant == self.plant and self.blocked[0] <= idx <= self.blocked[1]
                and (self.models is None or model in self.models))


def analyse(kg: KG, g: Grounded, incident_id: str) -> Impact:
    b = g.bindings
    if g.kind == "supplier_delay":
        ev = Evidence(["Shipment", "Part", "Model (BOM)", "ProductionOrder", "CustomerOrder", "Dealer / Customer"])
        sh = nid("Shipment", b["shipment"])
        part = kg.one(sh, "CARRIES")
        plant = kg.one(sh, "DELIVERS_TO")
        ev.add(sh, "CARRIES", part)
        ev.add(sh, "DELIVERS_TO", plant)
        lo = working_idx_on_or_after(date.fromisoformat(b["original_eta"]))
        new_idx = working_idx_on_or_after(date.fromisoformat(b["new_eta"]))
        imp = Impact(incident_id, g, ev, plant=key_of(plant), blocked=(lo, new_idx - 1), restart_idx=new_idx,
                     models=set())
        for model in kg.inn(part, "USES"):
            ev.add(model, "USES", part)
            imp.models.add(key_of(model))
            for po in kg.inn(model, "BUILDS"):
                a = kg.attr(po)
                if a["plant"] == imp.plant and max(lo, 0) <= a["idx"] < new_idx:
                    ev.add(po, "BUILDS", model)
                    ev.add(po, "AT_PLANT", plant)
                    imp.pos.append(key_of(po))
    elif g.kind == "plant_outage":
        ev = Evidence(["Plant", "ProductionOrder", "CustomerOrder", "Dealer / Customer"])
        plant = nid("Plant", b["plant"])
        lo = working_idx_on_or_after(date.fromisoformat(b["start"]))
        hi = working_idx_on_or_before(date.fromisoformat(b["end"]))
        imp = Impact(incident_id, g, ev, plant=b["plant"], blocked=(lo, hi), restart_idx=hi + 1, models=None)
        for po in kg.inn(plant, "AT_PLANT"):
            a = kg.attr(po)
            if max(lo, 0) <= a["idx"] <= hi:
                ev.add(po, "AT_PLANT", plant)
                imp.pos.append(key_of(po))
    else:
        ev = Evidence(["Lot", "ProductionOrder", "Vehicle", "Dealer / Customer"])
        imp = Impact(incident_id, g, ev, lots=list(b["lots"]))
        part = nid("Part", b["part"])
        for lot_id in b["lots"]:
            lot = nid("Lot", lot_id)
            la = kg.attr(lot)
            ev.add(lot, "OF_PART", part)
            for po in kg.out(lot, "INSTALLED_IN"):
                ev.add(lot, "INSTALLED_IN", po)
                for v in kg.out(po, "PRODUCED"):
                    va = kg.attr(v)
                    # rule 6, literally: built at the lot's plant, on the lot's build dates
                    if va["plant"] == la["plant"] and la["first"] <= va["build_date"] <= la["last"]:
                        ev.add(po, "PRODUCED", v)
                        imp.vehicles.append(key_of(v))
        imp.vehicles = sorted(set(imp.vehicles))
    imp.pos = sorted(set(imp.pos), key=lambda p: (kg.attr(nid("ProductionOrder", p))["idx"], p))
    return imp


def customer_orders(kg: KG, po_ids, ev: Evidence | None = None) -> list[str]:
    out = []
    for p in po_ids:
        po = nid("ProductionOrder", p)
        for co in kg.inn(po, "FULFILLED_BY"):
            out.append(key_of(co))
            if ev is not None:
                ev.add(co, "FULFILLED_BY", po)
                ev.add(co, "SOLD_THROUGH", nid("Dealer", kg.attr(co)["dealer"]))
                ev.add(co, "PLACED_BY", nid("Customer", kg.attr(co)["customer"]))
    return sorted(out)


def in_window(idx: int) -> bool:
    return 0 <= idx <= LAST_IDX


__all__ = ["Impact", "Evidence", "analyse", "customer_orders", "in_window", "date_to_idx"]
