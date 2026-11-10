# Design card — "Ground, Compile, Prove" control tower

**Idea.** The LLM reads; the knowledge graph decides which reading is real; the handbook is compiled into an exact plan;
the plan is proven on a digital twin before a single live call is made.

## Architecture
```
event ─► Coordinator ─┬─ LLM reader (qwen2.5:1.5b, JSON-constrained) ─┐
                      └─ rule reader (lexical, second opinion)        ├─► readings ─► Grounder (binds to KG records)
                                                                       ┘     unique binding → act · conflict/none → escalate · no ops entity → noise
 policy compiler (one shared slot + loaner ledger per event):
   SupplyChainPlanner  mes.get_schedule / reschedule / transfer      DealerNetwork  dealer.notify / stop_sale
   CustomerCareQuality crm.loaner_availability / assign_loaner / notify_customer / quality.hold_lot
 Prove: dry run on the provided enterprise model + 13 invariants vs an independent table-only oracle → execute live
```
**Ontology.** 11 classes (Supplier, Part, Model, Plant, Shipment, Lot, ProductionOrder, CustomerOrder, Customer, Dealer,
Vehicle), 17 relations, one inferred (`Lot -INSTALLED_IN-> ProductionOrder` via plant, build window, BOM); ~17k nodes,
28k edges. Every impact set is a traversal that records the edges it walked; those edges are the cited graph path.

## LLM versus code
| Concern | Who |
|---|---|
| Split digests, classify, pick out names and date phrases | LLM, with the rule reader as a second opinion |
| Names → ids, phrases → dates | code (aliases + tables, calendar): small models invent ids and fumble dates |
| Which reading is real | knowledge graph (existence and consistency are checkable) |
| Who is affected, where orders go, who gets a loaner | code (policy-as-code, exact rules) |
| Dealer and owner message text | templates (deterministic, cheap) |

"Next Friday" on a Monday is ambiguous (Mar 12 or 19). The grounder tries both and keeps the one under which every
incident binds: in the morning digest only "next week" matches a real shipment (SH-0190), which also fixes the outage
window to Mar 19–22 (6 affected orders, not 21).

## Guardrails
- No operational entity → noise, zero calls. Hedged or unspecific → escalate with what is missing, zero writes.
- Supplier, part, plant and dates must agree with a record; two readers binding to different records → escalate.
- The plan is withheld unless the dry run passes all 13 invariants (only affected orders moved, capacity respected,
  one notice per dealer listing exactly its orders, loaners equal the policy winners per pool, ≥ 3 action kinds…).
- Tool catalog read from `GET /tools`; every call is checked against it before sending. A failed move is re-read and
  retried once, then escalated; transport errors retry with backoff.

## Evaluation (15 events)
- Categories: 1 compound, 3 delays, 3 outages, 3 quality holds, 3 noise, 2 unconfirmed.
- Live run: **879 calls, 0 failed**, 2 escalations (rumors only), 0 writes on noise or rumors. `action_log.jsonl` has one
  line per tool call; the 3 noise events make no calls, so 12 of the 15 events appear in it.
- Replay on a fresh world: all events pass. Independent audit (`tools/audit_run.py`, imports none of our code): all
  handbook rules hold. 31 tests pass.

**Model cited:** `qwen2.5:1.5b` (Qwen2.5 1.5B Instruct, ~1 GB, Q4) served locally by Ollama on a MacBook, no cloud.
`runs/final`: 15 LLM calls, 6,423 tokens, 27.8 s of model time, 28.9 s end to end. The 1.5B model sometimes misreads a
sentence; the rule reader and grounding overrule it, so such slips cause no extra action. Results match the rule-only run.

## Assumptions (the handbook is silent)
1. Transfers keep the order's original build date (`CT_TRANSFER_ANCHOR=original_date`); this changes 5 orders (E-09, E-11).
2. When slots are scarce, orders are placed in original (date, id) order.
3. "next <weekday>" means that day next week unless the data says otherwise (E-04).
4. One loaner per customer even with several affected orders.

## What we would improve
LLM-written dealer messages behind a validator; a confidence score that routes borderline reads to a human before
execution; a date convention learned per source; a graph-database export for ad-hoc queries.
