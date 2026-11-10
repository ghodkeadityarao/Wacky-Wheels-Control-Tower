import json
from datetime import date

import pytest

from control_tower.agents import Coordinator
from control_tower.clients import TwinClient
from control_tower.config import DATA_DIR, Settings
from control_tower.kg import KG
from control_tower.llm import Backend
from control_tower.nlp import Resolver, find_dates
from control_tower.pipeline import handle_event
from control_tower.verify import WRITE_TOOLS


@pytest.fixture(scope="module")
def kg():
    return KG(DATA_DIR)


@pytest.fixture(scope="module")
def coord(kg):
    return Coordinator(kg, Settings(transfer_anchor="original_date"))


def run(coord, text, eid="T-1"):
    client = TwinClient(namespace="test")
    tr = handle_event(coord, {"event_id": client.fresh(eid), "text": text, "source": "test"}, client)
    return tr, client


def writes(tr):
    return [r for r in tr["executed"] if r["tool"] in WRITE_TOOLS and r.get("ok")]


# ------------------------------------------------------------------ language layer
def test_relative_dates_keep_both_readings():
    nf = find_dates("next Friday")[0].candidates
    assert nf == {"next_week": date(2027, 3, 19), "upcoming": date(2027, 3, 12)}
    assert find_dates("last Monday")[0].candidates == {"explicit": date(2027, 3, 1)}
    assert find_dates("Monday Mar 22")[0].candidates == {"explicit": date(2027, 3, 22)}
    assert "does not match" in find_dates("Tuesday Mar 22")[0].note


def test_resolver_aliases_and_vague_words(kg):
    r = Resolver(kg)
    ids = lambda t, c: {k for m in r.find(t) if m.cls == c for k in m.ids}
    assert ids("waiting on battery packs from Lumen", "Supplier") == {"SUP-18"}
    assert ids("waiting on battery packs from Lumen", "Part") == {"BATT-60"}
    assert ids("Volt", "Supplier") == {"SUP-05"}
    assert ids("the Texas plant", "Plant") == {"P_TX"}
    vague = [m for m in r.find("seat material") if m.cls == "Part"]
    assert vague and vague[0].ambiguous


def test_inferred_lot_edges_follow_bom(kg):
    # LOT-0017 is harness B at P_TX: Terra (harness C) must not be linked
    models = {kg.attr(p)["model"] for p in kg.out("Lot:LOT-0017", "INSTALLED_IN")}
    assert models == {"RANGER", "SUMMIT"}


# ------------------------------------------------------------------ the 15 events
@pytest.fixture(scope="module")
def feed(coord):
    events = json.loads((DATA_DIR / "events.json").read_text(encoding="utf-8"))
    client = TwinClient(namespace="feed")
    return {e["event_id"]: handle_event(coord, e, client) for e in events}


def test_every_real_incident_is_proven_and_has_three_kinds(feed):
    for eid, tr in feed.items():
        if tr["dry_run"] is not None:
            assert tr["dry_run"]["ok"], (eid, [c for c in tr["dry_run"]["checks"] if not c["ok"]])
            assert len({r["tool"] for r in writes(tr)}) >= 3, eid
            assert tr["stats"]["failed_live_calls"] == 0, eid
            assert tr["incidents"] and all(i["example_path"] for i in tr["incidents"]), eid


def test_noise_and_rumors_never_write(feed):
    for eid, tr in feed.items():
        if tr["category"] in (["noise"], ["unconfirmed"]):
            assert not writes(tr), eid
            esc = [r for r in tr["executed"] if r["tool"] == "escalate"]
            assert bool(esc) == (tr["category"] == ["unconfirmed"]), eid


def test_compound_event_shares_one_notice_per_dealer(feed):
    tr = next(t for t in feed.values() if len(t["category"]) == 2)
    dealers = [r["args"]["dealer_id"] for r in writes(tr) if r["tool"] == "dealer.notify"]
    assert len(dealers) == len(set(dealers))


# ------------------------------------------------------------------ unseen wording (generalization)
@pytest.mark.parametrize("text,kind,binding", [
    ("Heads-up from Volt: the harness delivery for the Texas plant due Mon Mar 15 slips to Wed Mar 17.",
     "supplier_delay", {"part": "HARN-C", "plant": "P_TX", "supplier": "SUP-05"}),
    ("Greensboro is down for a flood Wednesday Mar 17 through Friday Mar 19.",
     "plant_outage", {"plant": "P_NC", "start": "2027-03-17", "end": "2027-03-19"}),
    ("Quality alert: Ridgeline says harness B at the Kentucky plant has cracked connectors. Built Mar 4 to Mar 5 is suspect.",
     "quality_hold", {"part": "HARN-B", "plant": "P_KY", "supplier": "SUP-16"}),
])
def test_unseen_incidents_ground_and_prove(coord, text, kind, binding):
    tr, _ = run(coord, text)
    assert tr["category"] == [kind]
    b = tr["incidents"][0]["bindings"]
    assert {k: b[k] for k in binding} == binding
    assert tr["dry_run"]["ok"]


@pytest.mark.parametrize("text,category", [
    ("Snowstorm expected in Denver this weekend, flights cancelled.", ["noise"]),
    ("New accessories price list goes live April 1.", ["noise"]),
    ("Heard Helix might be having trouble, nothing confirmed.", ["unconfirmed"]),
    # supplier does not match the lot at that plant -> refuse to act
    ("Quality alert: Quanta says the infotainment chip sent to Jackson had a faulty weld. "
     "Anything built Thursday Feb 25 through Monday Mar 1 is suspect.", ["unconfirmed"]),
    # window does not line up with any lot boundary
    ("Quality alert: Kestrel infotainment chip at Jackson faulty, built Feb 26 to Feb 26.", ["unconfirmed"]),
    # new ETA before the original one
    ("Shipment SH-0182 delayed: was due 2027-03-15, will now arrive 2027-03-12.", ["unconfirmed"]),
])
def test_guardrails(coord, text, category):
    tr, _ = run(coord, text)
    assert tr["category"] == category, tr["verdict"]
    assert not writes(tr)


# vague operational phrasings must escalate: never act, never be ignored
@pytest.mark.parametrize("text,category", [
    ("Battery packs are running late. The line is waiting.", ["unconfirmed"]),
    ("The assembly plant is down through Wednesday.", ["unconfirmed"]),
    ("Quality confirmed a defective battery lot.", ["unconfirmed"]),
    ("Can someone send me the latest marketing brochure?", ["noise"]),
    ("Heard there might be trouble at one of the plants?", ["unconfirmed"]),
    ("Plant is offline, and the battery shipment is delayed.", ["unconfirmed"]),
])
def test_vague_phrasings(coord, text, category):
    tr, _ = run(coord, text)
    assert tr["category"] == category, tr["verdict"]
    assert not writes(tr)
    assert bool(tr["escalation"]) == (category == ["unconfirmed"])


def test_open_start_outage_runs_from_today(coord):
    tr, _ = run(coord, "Greensboro plant offline until Friday Mar 19.")
    b = tr["incidents"][0]["bindings"]
    assert (b["start"], b["end"]) == ("2027-03-08", "2027-03-19") and tr["dry_run"]["ok"]


def test_unnumbered_compound_is_split(coord):
    tr, _ = run(coord, "Jackson is dark Friday Mar 26 through Tuesday Mar 30, and shipment SH-0182 slips to 2027-03-18.")
    assert tr["category"] == ["plant_outage", "supplier_delay"]
    assert tr["dry_run"]["ok"]


# ------------------------------------------------------------------ failed calls
class FlakyTwin(TwinClient):
    """Fails the first reschedule as if the slot had been taken by someone else."""
    def __init__(self):
        super().__init__(namespace="flaky")
        self.tripped = False

    def act(self, event_id, tool, args):
        if tool == "mes.reschedule" and not self.tripped:
            self.tripped = True
            self.calls.append({"event_id": event_id, "tool": tool, "args": args, "ok": False})
            return False, {"error": "no free slot at that day"}
        return super().act(event_id, tool, args)


def test_failed_move_is_recovered_from_a_live_reread(coord):
    events = json.loads((DATA_DIR / "events.json").read_text(encoding="utf-8"))
    ev = next(e for e in events if "SH-0182" in e["text"])
    client = FlakyTwin()
    tr = handle_event(coord, dict(ev, event_id=client.fresh("flaky")), client)
    failed = [r for r in tr["executed"] if r["tool"] == "mes.reschedule" and r.get("recovered") is not None]
    assert failed and failed[0]["recovered"] is True
    assert not tr["escalation"]


# ------------------------------------------------------------------ LLM reader contract
class FakeBackend(Backend):
    def __init__(self, payload):
        super().__init__("ollama", "fake-model")
        self.payload = payload

    def complete_json(self, user):
        self.calls += 1
        return self.payload


def test_llm_reading_is_resolved_by_code(kg):
    payload = {"incidents": [{"kind": "supplier_delay", "evidence": "Huntsville line is waiting on 60 kWh battery packs",
                              "plant": "Huntsville", "part": "60 kWh battery packs", "supplier": "Lumen",
                              "dates": {"original_eta": "Monday Mar 29", "new_eta": "Thursday Apr 1"},
                              "confirmed": True, "reason": "late shipment"}]}
    c = Coordinator(kg, Settings(), FakeBackend(payload))
    tr, _ = run(c, "Huntsville line is waiting on 60 kWh battery packs from Lumen. Original drop was Monday Mar 29, "
                   "new ETA is Thursday Apr 1 because of a truck breakdown.")
    assert tr["incidents"][0]["bindings"]["shipment"] == "SH-0248"
    assert tr["dry_run"]["ok"]


def test_reader_disagreement_escalates(kg):
    payload = {"incidents": [{"kind": "supplier_delay", "evidence": "x", "plant": "Huntsville",
                              "part": "60 kWh battery packs", "supplier": "Lumen",
                              "dates": {"original_eta": "Monday Mar 29", "new_eta": "Friday Apr 2"},
                              "confirmed": True, "reason": "late"}]}
    c = Coordinator(kg, Settings(), FakeBackend(payload))
    tr, _ = run(c, "Huntsville line is waiting on 60 kWh battery packs from Lumen. Original drop was Monday Mar 29, "
                   "new ETA is Thursday Apr 1.")
    assert not writes(tr) and tr["escalation"]


def test_llm_naming_a_different_plant_on_a_grounded_sentence_still_escalates(kg):
    # same sentence, but the LLM saw a second plant that it could not ground: that may be a second incident
    text = ("Quick heads up from Orbit: the surround-view cameras run for Huntsville can't make it Monday Mar 29. "
            "Carrier says Thursday Apr 1 now (a customs inspection).")
    payload = {"incidents": [{"kind": "plant_outage", "evidence": text, "plant": "Jackson", "part": None,
                              "supplier": None, "dates": {}, "confirmed": True, "reason": "second incident?"}]}
    tr, _ = run(Coordinator(kg, Settings(), FakeBackend(payload)), text)
    assert tr["incidents"] and tr["escalation"]


def test_tool_catalog_blocks_a_malformed_call_before_it_is_sent(coord):
    from control_tower.agents import Action, Audit
    client = TwinClient(namespace="cat")
    eid = client.fresh("cat")
    bad = Action("DealerNetwork", "dealer.notify", {"dealer_id": "D01", "customer_order_ids": "CO-00001"}, "bad args")
    log, failures = coord.execute(client, eid, [bad], Audit())
    assert failures and log[0]["blocked"] and not client.calls


def test_escalations_are_plain_language(feed):
    for eid in ("E-02", "E-13"):
        esc = [r for r in feed[eid]["executed"] if r["tool"] == "escalate"][0]["args"]["reason"]
        assert "[rules]" not in esc and "To act we need" in esc
        assert feed[eid]["human_review"]


def test_independent_audit_passes_on_the_feed(feed, tmp_path):
    import subprocess
    import sys
    rows = [json.dumps({"event_id": eid, "tool": r["tool"], "args": r["args"], "ok": r["ok"]})
            for eid, tr in feed.items() for r in tr["executed"] if not r.get("blocked") and not r.get("skipped")]
    (tmp_path / "action_log.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    audit = DATA_DIR.parent / "tools" / "audit_run.py"
    out = subprocess.run([sys.executable, "-I", str(audit), str(tmp_path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout[-2000:]


def test_llm_misclassifying_a_grounded_sentence_does_not_add_an_escalation(kg):
    text = ("Quick heads up from Orbit: the surround-view cameras run for Huntsville can't make it Monday Mar 29. "
            "Carrier says Thursday Apr 1 now (a customs inspection).")
    payload = {"incidents": [{"kind": "plant_outage", "evidence": text, "plant": None, "part": None,
                              "supplier": None, "dates": {}, "confirmed": True, "reason": "wrong kind"}]}
    tr, _ = run(Coordinator(kg, Settings(), FakeBackend(payload)), text)
    assert tr["incidents"] and tr["dry_run"]["ok"] and not tr["escalation"]
