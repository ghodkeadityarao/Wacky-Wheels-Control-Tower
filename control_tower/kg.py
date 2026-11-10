"""Ontology and knowledge graph over the Wacky Wheels tables.

Every table row becomes a typed node; every foreign key becomes a typed edge. One relation is
*inferred* rather than read: Lot -INSTALLED_IN-> ProductionOrder (same plant, build date inside the
lot window, and the order's model uses the lot's part per the BOM). Incident impact is computed by
traversing these edges, and each traversal records the edges it walked so the path can be cited.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import networkx as nx

START = date(2027, 3, 8)          # working-day index 0 (Monday); "today" in the handbook
FIRST_IDX, LAST_IDX = -10, 19


def idx_to_date(i: int) -> date:
    return START + timedelta(days=7 * (i // 5) + i % 5)


def date_to_idx(d: date) -> int:
    days = (d - START).days
    if days % 7 > 4:
        raise ValueError(f"{d} is a weekend")
    return 5 * (days // 7) + days % 7


def working_idx_on_or_after(d: date) -> int:
    while d.weekday() > 4:
        d += timedelta(days=1)
    return date_to_idx(d)


def working_idx_on_or_before(d: date) -> int:
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return date_to_idx(d)


ONTOLOGY = {
    "classes": {
        "Supplier": "External company that ships parts (suppliers.csv)",
        "Part": "Component in a bill of materials (parts.csv)",
        "Model": "Vehicle model (models.csv)",
        "Plant": "Assembly plant with daily build slots (plants.csv)",
        "Shipment": "Inbound shipment of one part to one plant with an ETA (inbound_shipments.csv)",
        "Lot": "Batch of a part installed at a plant over a build window (part_lots.csv)",
        "ProductionOrder": "One day's build of a model at a plant (production_orders.csv)",
        "CustomerOrder": "Customer purchase promised from a production order (customer_orders.csv)",
        "Customer": "Buyer or owner, with a loyalty tier (customers.csv)",
        "Dealer": "Retail dealer with a loaner pool (dealers.csv, loaners.csv)",
        "Vehicle": "Finished vehicle by VIN, sold or in stock (finished_vehicles.csv)",
    },
    "relations": [
        ("Supplier", "SHIPS", "Shipment"),
        ("Shipment", "CARRIES", "Part"),
        ("Shipment", "DELIVERS_TO", "Plant"),
        ("Supplier", "MADE", "Lot"),
        ("Lot", "OF_PART", "Part"),
        ("Lot", "INSTALLED_AT", "Plant"),
        ("Lot", "INSTALLED_IN", "ProductionOrder"),   # inferred
        ("Model", "USES", "Part"),                    # bill of materials
        ("Plant", "CAN_BUILD", "Model"),
        ("ProductionOrder", "AT_PLANT", "Plant"),
        ("ProductionOrder", "BUILDS", "Model"),
        ("CustomerOrder", "FULFILLED_BY", "ProductionOrder"),
        ("CustomerOrder", "PLACED_BY", "Customer"),
        ("CustomerOrder", "SOLD_THROUGH", "Dealer"),
        ("ProductionOrder", "PRODUCED", "Vehicle"),
        ("Vehicle", "AT_DEALER", "Dealer"),
        ("Vehicle", "OWNED_BY", "Customer"),
    ],
    "inferred": {
        "INSTALLED_IN": "Lot L -> ProductionOrder P if P.plant = L.plant, L.first <= P.build_date <= L.last, "
                        "and P.model USES L.part",
    },
}


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def nid(cls: str, key: str) -> str:
    return f"{cls}:{key}"


def key_of(node: str) -> str:
    return node.split(":", 1)[1]


class KG:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        rd = lambda n: read_csv(self.data_dir / n)
        self.tables = {n[:-4]: rd(n) for n in (
            "plants.csv", "models.csv", "parts.csv", "suppliers.csv", "dealers.csv", "customers.csv",
            "plant_models.csv", "bom.csv", "inbound_shipments.csv", "part_lots.csv", "production_orders.csv",
            "customer_orders.csv", "finished_vehicles.csv", "loaners.csv", "aliases.csv")}
        self.g = nx.MultiDiGraph()
        self._build()

    # ------------------------------------------------------------------ build
    def _node(self, cls, key, **attrs):
        self.g.add_node(nid(cls, key), cls=cls, key=key, **attrs)

    def _edge(self, a, rel, b):
        self.g.add_edge(a, b, key=rel, rel=rel)

    def _build(self):
        t = self.tables
        loaners = {r["dealer_id"]: int(r["loaners_available"]) for r in t["loaners"]}
        for r in t["plants"]:
            self._node("Plant", r["plant_id"], name=r["name"], city=r["city"], slots=int(r["slots_per_day"]))
        for r in t["models"]:
            self._node("Model", r["model"], segment=r["segment"])
        for r in t["parts"]:
            self._node("Part", r["part_id"], name=r["name"], category=r["category"])
        for r in t["suppliers"]:
            self._node("Supplier", r["supplier_id"], name=r["name"], city=r["city"])
        for r in t["dealers"]:
            self._node("Dealer", r["dealer_id"], name=r["name"], city=r["city"], region=r["region"],
                       loaners=loaners.get(r["dealer_id"], 0))
        for r in t["customers"]:
            self._node("Customer", r["customer_id"], name=r["name"], tier=r["tier"])
        for r in t["plant_models"]:
            self._edge(nid("Plant", r["plant_id"]), "CAN_BUILD", nid("Model", r["model"]))
        for r in t["bom"]:
            self._edge(nid("Model", r["model"]), "USES", nid("Part", r["part_id"]))
        for r in t["inbound_shipments"]:
            s = nid("Shipment", r["shipment_id"])
            self._node("Shipment", r["shipment_id"], eta=r["eta_date"], covers_days=int(r["covers_days"]),
                       supplier=r["supplier_id"], part=r["part_id"], plant=r["plant_id"])
            self._edge(nid("Supplier", r["supplier_id"]), "SHIPS", s)
            self._edge(s, "CARRIES", nid("Part", r["part_id"]))
            self._edge(s, "DELIVERS_TO", nid("Plant", r["plant_id"]))
        for r in t["part_lots"]:
            lot = nid("Lot", r["lot_id"])
            self._node("Lot", r["lot_id"], first=r["first_build_date"], last=r["last_build_date"],
                       supplier=r["supplier_id"], part=r["part_id"], plant=r["plant_id"])
            self._edge(nid("Supplier", r["supplier_id"]), "MADE", lot)
            self._edge(lot, "OF_PART", nid("Part", r["part_id"]))
            self._edge(lot, "INSTALLED_AT", nid("Plant", r["plant_id"]))
        for r in t["production_orders"]:
            p = nid("ProductionOrder", r["production_order_id"])
            d = date.fromisoformat(r["build_date"])
            self._node("ProductionOrder", r["production_order_id"], plant=r["plant_id"], build_date=r["build_date"],
                       idx=date_to_idx(d), model=r["model"], units=int(r["units"]))
            self._edge(p, "AT_PLANT", nid("Plant", r["plant_id"]))
            self._edge(p, "BUILDS", nid("Model", r["model"]))
        for r in t["customer_orders"]:
            c = nid("CustomerOrder", r["customer_order_id"])
            self._node("CustomerOrder", r["customer_order_id"], promised=r["promised_date"], dealer=r["dealer_id"],
                       customer=r["customer_id"], model=r["model"], po=r["production_order_id"])
            self._edge(c, "FULFILLED_BY", nid("ProductionOrder", r["production_order_id"]))
            self._edge(c, "PLACED_BY", nid("Customer", r["customer_id"]))
            self._edge(c, "SOLD_THROUGH", nid("Dealer", r["dealer_id"]))
        for r in t["finished_vehicles"]:
            v = nid("Vehicle", r["vin"])
            self._node("Vehicle", r["vin"], status=r["status"], plant=r["plant_id"], build_date=r["build_date"],
                       model=r["model"], dealer=r["dealer_id"], customer=r["customer_id"] or "")
            self._edge(nid("ProductionOrder", r["production_order_id"]), "PRODUCED", v)
            self._edge(v, "AT_DEALER", nid("Dealer", r["dealer_id"]))
            if r["customer_id"]:
                self._edge(v, "OWNED_BY", nid("Customer", r["customer_id"]))
        # inferred relation: which production orders consumed which lot
        for lot in self.nodes("Lot"):
            a = self.g.nodes[lot]
            for model in self.inn(nid("Part", a["part"]), "USES"):
                for po in self.inn(model, "BUILDS"):
                    pa = self.g.nodes[po]
                    if pa["plant"] == a["plant"] and a["first"] <= pa["build_date"] <= a["last"]:
                        self._edge(lot, "INSTALLED_IN", po)

    # ------------------------------------------------------------------ queries
    def nodes(self, cls: str) -> list[str]:
        return [n for n, a in self.g.nodes(data=True) if a["cls"] == cls]

    def attr(self, node: str) -> dict:
        return self.g.nodes[node]

    def has(self, node: str) -> bool:
        return node in self.g

    def out(self, node: str, rel: str) -> list[str]:
        return [v for _, v, k in self.g.out_edges(node, keys=True) if k == rel]

    def inn(self, node: str, rel: str) -> list[str]:
        return [u for u, _, k in self.g.in_edges(node, keys=True) if k == rel]

    def one(self, node: str, rel: str) -> str:
        res = self.out(node, rel)
        if len(res) != 1:
            raise LookupError(f"{node} -{rel}-> expected exactly one target, got {res}")
        return res[0]

    def slots(self, plant_id: str) -> int:
        return self.attr(nid("Plant", plant_id))["slots"]

    def plant_models(self, plant_id: str) -> set[str]:
        return {key_of(m) for m in self.out(nid("Plant", plant_id), "CAN_BUILD")}

    def plants_building(self, model: str) -> list[str]:
        return sorted(key_of(p) for p in self.inn(nid("Model", model), "CAN_BUILD"))

    def models_using(self, part_id: str) -> set[str]:
        return {key_of(m) for m in self.inn(nid("Part", part_id), "USES")}

    # ------------------------------------------------------------------ export
    def stats(self) -> dict:
        by_cls, by_rel = defaultdict(int), defaultdict(int)
        for _, a in self.g.nodes(data=True):
            by_cls[a["cls"]] += 1
        for *_, k in self.g.edges(keys=True):
            by_rel[k] += 1
        return {"nodes": self.g.number_of_nodes(), "edges": self.g.number_of_edges(),
                "nodes_by_class": dict(by_cls), "edges_by_relation": dict(by_rel)}

    def export(self, out_dir: str | Path, graphml: bool = True) -> dict:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stats = self.stats()
        (out / "ontology.json").write_text(json.dumps({**ONTOLOGY, "stats": stats}, indent=2), encoding="utf-8")
        lines = ["classDiagram"]
        for cls, desc in ONTOLOGY["classes"].items():
            lines.append(f"  class {cls}")
        for a, rel, b in ONTOLOGY["relations"]:
            lines.append(f"  {a} --> {b} : {rel}")
        (out / "ontology.mmd").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if graphml:
            nx.write_graphml(nx.DiGraph(self.g), out / "knowledge_graph.graphml")
        return stats
