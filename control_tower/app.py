"""Control-tower view.   streamlit run control_tower/app.py"""
from __future__ import annotations

import html
import inspect as _inspect
import io
import json
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# Streamlit >=1.50 takes width="stretch"; older releases only know use_container_width.
STRETCH = ({"width": "stretch"} if "width" in _inspect.signature(st.dataframe).parameters
           and "stretch" in str(_inspect.signature(st.dataframe).parameters["width"].annotation)
           else {"use_container_width": True})

PKG = Path(__file__).resolve().parent.parent
RUNS = PKG / "runs"
SEQ = ["#e4eefc", "#b9d4f7", "#8bb8f0", "#5b98e6", "#2f72cc", "#1b4f9a"]
WRITE = {"mes.reschedule", "mes.transfer", "dealer.notify", "dealer.stop_sale", "crm.assign_loaner",
         "crm.notify_customer", "quality.hold_lot"}

# category -> (label, text colour, background)
CAT = {
    "supplier_delay": ("Supplier delay", "#8a5200", "#fdf0d8"),
    "plant_outage": ("Plant outage", "#17517e", "#dcecf9"),
    "quality_hold": ("Quality hold", "#6b2f8f", "#eddff7"),
    "noise": ("Noise · no action", "#4b5563", "#e8eaee"),
    "unconfirmed": ("Unconfirmed · escalated", "#9b1c2c", "#fbe0e3"),
}
AGENT_COLOR = {"Coordinator": "#1c232b", "SupplyChainPlanner": "#17517e", "DealerNetwork": "#8a5200",
               "CustomerCareQuality": "#6b2f8f"}
CLASS_COLOR = {"Shipment": "#fdf0d8", "Part": "#fdf0d8", "Model": "#e4eefc", "Plant": "#dcecf9", "Lot": "#eddff7",
               "ProductionOrder": "#dcecf9", "CustomerOrder": "#e3f3e8", "Dealer": "#fbe9d6", "Customer": "#e3f3e8",
               "Vehicle": "#e3f3e8", "Supplier": "#fdf0d8"}
# which handbook rule each tool call carries out (policy_handbook.md)
RULE = {"mes.get_schedule": "Rule 3 (read)", "mes.reschedule": "Rule 3", "mes.transfer": "Rule 3",
        "dealer.notify": "Rule 4", "crm.loaner_availability": "Rule 5 (read)", "crm.assign_loaner": "Rule 5",
        "quality.hold_lot": "Rule 6", "dealer.stop_sale": "Rule 6", "crm.notify_customer": "Rule 6", "escalate": "Rule 8"}
READS = {"mes.get_schedule", "crm.loaner_availability"}
STATUS_WORDS = {"grounded": "matched to records", "unconfirmed": "not confirmed", "failed": "did not match any record",
                "noise": "not about operations", "ambiguous": "several records fit"}

st.set_page_config(page_title="Wacky Wheels Control Tower", page_icon="🚦", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
html, body, [class*="css"] { font-family: Inter, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
[data-testid="stToolbar"], [data-testid="stDecoration"], footer { display: none !important; }
header[data-testid="stHeader"] { background: transparent; height: 2.2rem; }
.block-container { padding: 3rem 2.2rem 3rem; max-width: 1600px; }
.topbar { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 20px;
  background:#1c232b; color:#fff; border-radius:14px; margin-bottom:16px; }
.topbar .brand { font-size:21px; font-weight:800; letter-spacing:-.5px; }
.topbar .brand span { color:#ff5c6c; }
.topbar .sub { font-size:12px; color:#aab4c0; margin-top:2px; }
.chip { display:inline-block; font-size:11px; font-weight:600; padding:4px 10px; border-radius:20px;
  background:#2c3641; color:#d6dde5; margin-left:6px; }
.kpis { display:grid; grid-template-columns:repeat(6,1fr); gap:12px; margin-bottom:18px; }
.kpi { background:#fff; border:1px solid #e3e6ea; border-radius:12px; padding:14px 16px; }
.kpi .v { font-size:30px; font-weight:750; line-height:1.05; letter-spacing:-1px; }
.kpi .l { font-size:11px; color:#667085; text-transform:uppercase; letter-spacing:.8px; margin-top:5px; font-weight:600; }
.kpi.good .v { color:#1d7a55; } .kpi.warn .v { color:#b25e00; } .kpi.mute .v { color:#667085; }
.badge { display:inline-block; font-size:11px; font-weight:700; padding:3px 9px; border-radius:6px; margin-right:5px; }
.card { background:#fff; border:1px solid #e3e6ea; border-radius:12px; padding:16px 18px; margin-bottom:12px; }
.card h4 { margin:0 0 8px; font-size:12px; text-transform:uppercase; letter-spacing:1px; color:#667085; font-weight:700; }
.msg { font-size:15px; line-height:1.55; border-left:4px solid #c82132; background:#fff; }
.verdict { font-size:14px; line-height:1.55; }
.verdict.act { border-left:4px solid #1d7a55; } .verdict.esc { border-left:4px solid #c82132; }
.verdict.none { border-left:4px solid #98a2b3; }
.stepper { display:flex; gap:8px; margin:4px 0 14px; }
.step { flex:1; background:#fff; border:1px solid #e3e6ea; border-radius:10px; padding:10px 12px; }
.step .n { font-size:10px; color:#98a2b3; font-weight:700; letter-spacing:1px; }
.step .t { font-size:14px; font-weight:700; margin-top:1px; }
.step .d { font-size:12px; color:#667085; margin-top:2px; }
.step.done { border-color:#b7e0cc; background:#f2faf6; } .step.done .n { color:#1d7a55; }
.step.skip { opacity:.55; } .step.stop { border-color:#f2bcc3; background:#fdf3f4; } .step.stop .n { color:#c82132; }
.out { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; margin-bottom:14px; }
.out div { background:#fff; border:1px solid #e3e6ea; border-radius:10px; padding:10px 12px; }
.out b { display:block; font-size:22px; letter-spacing:-.5px; } .out span { font-size:11px; color:#667085; }
.tl { border-left:2px solid #e3e6ea; margin-left:8px; padding-left:16px; }
.tl .it { position:relative; padding:0 0 12px; font-size:13px; line-height:1.5; }
.tl .it::before { content:""; position:absolute; left:-22px; top:5px; width:10px; height:10px; border-radius:50%;
  background:var(--c,#98a2b3); border:2px solid #fff; box-shadow:0 0 0 1px #d0d5dd; }
.tl .ag { font-weight:700; color:var(--c,#1c232b); } .tl .st { color:#667085; font-style:italic; margin:0 4px; }
.feedrow { font-size:12px; color:#667085; margin:-6px 0 14px 2px; line-height:1.45; }
div[data-testid="stButton"] > button { width:100%; text-align:left; justify-content:flex-start; border-radius:10px;
  border:1px solid #e3e6ea; background:#fff; padding:.55rem .8rem; font-weight:600; }
div[data-testid="stButton"] > button[kind="primary"] { background:#fdf3f4; border-color:#c82132; color:#1c232b; }
button[data-baseweb="tab"] { font-weight:600; }
</style>
""", unsafe_allow_html=True)


@st.cache_data
def load_run(run: str):
    d = RUNS / run
    traces = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((d / "traces").glob("*.json"))}
    log = [json.loads(l) for l in (d / "action_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (d / "action_log.jsonl").exists() else []
    summary = json.loads((d / "run_summary.json").read_text(encoding="utf-8")) if (d / "run_summary.json").exists() else {}
    onto = json.loads((d / "ontology" / "ontology.json").read_text(encoding="utf-8")) if (d / "ontology" / "ontology.json").exists() else {}
    return traces, log, summary, onto


@st.cache_data
def base_schedule():
    po = pd.read_csv(PKG / "data" / "production_orders.csv")
    return po[po.build_date >= "2027-03-08"]


@st.cache_data
def names() -> dict:
    """id -> human name for plants, parts, suppliers and dealers."""
    out = {}
    for f, k, v in (("plants.csv", "plant_id", "name"), ("parts.csv", "part_id", "name"),
                    ("suppliers.csv", "supplier_id", "name"), ("dealers.csv", "dealer_id", "name")):
        df = pd.read_csv(PKG / "data" / f)
        out.update(dict(zip(df[k], df[v].str.replace("Wacky Wheels ", "", regex=False)
                            .str.replace(" Wacky Wheels", "", regex=False))))
    return out


@st.cache_data
def vehicles() -> dict:
    df = pd.read_csv(PKG / "data" / "finished_vehicles.csv", dtype=str).fillna("")
    return {r.vin: {"status": r.status, "dealer": r.dealer_id, "customer": r.customer_id} for r in df.itertuples()}


@st.cache_data
def bundle(run: str) -> bytes:
    """Everything one run produced (action log, traces, reports, ontology) as a zip; the large graphml is left out."""
    d, buf = RUNS / run, io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix != ".graphml":
                z.write(p, p.relative_to(d))
    return buf.getvalue()


def named(i: str) -> str:
    n = names().get(i)
    return f"{n} ({i})" if n else i


def pipeline_steps(t: dict) -> list:
    v, dr, acted = t["verdict"], t["dry_run"], bool(t["incidents"])
    readers = Counter(r["reading"]["source"].split(":")[0] for r in v["readings"])
    proof = f"{sum(c['ok'] for c in dr['checks'])}/{len(dr['checks'])} checks" if dr else "—"
    n_w = sum(counts(t).values())
    return [("1 · READ", "Understand", f"{'+'.join(sorted(readers))} reader" + ("s" if len(readers) > 1 else ""), "done"),
            ("2 · GROUND", "Match to records",
             f"{len(t['incidents'])} incident(s) bound" if acted else ("noise" if t["category"] == ["noise"] else "nothing specific"),
             "done" if acted else "stop"),
            ("3 · PLAN", "Apply handbook", f"{len(t['plan'])} planned calls" if t["plan"] else "no plan", "done" if t["plan"] else "skip"),
            ("4 · PROVE", "Dry run on twin", proof, "done" if dr and dr["ok"] else "skip"),
            ("5 · EXECUTE", "Live calls", f"{n_w} writes" + (" · 1 escalation" if t["escalation"] else ""),
             "done" if n_w else ("stop" if t["escalation"] else "skip"))]


def stepper_html(steps: list, upto: int = 5) -> str:
    return '<div class="stepper">' + "".join(
        f'<div class="step {s if i < upto else "skip"}"><div class="n">{a}</div><div class="t">{b}</div>'
        f'<div class="d">{esc(c) if i < upto else "…"}</div></div>' for i, (a, b, c, s) in enumerate(steps)) + "</div>"


def review_html(items: list) -> str:
    out = ""
    for i in items:
        out += (f"<div style='margin:4px 0 10px'><b>{esc(i['headline'])}.</b><ul style='margin:4px 0 4px 18px'>"
                + "".join(f"<li>{esc(w)}</li>" for w in i["why"]) + "</ul>"
                + (f"<span style='color:#1d7a55'><b>To act we need:</b> {esc(i['need'])}.</span>" if i.get("need") else "")
                + "</div>")
    return out


def path_dot(inc: dict) -> str:
    """The cited graph path, drawn faithfully: every arrow is a real ontology relation in its true direction
    (dir=back keeps a left-to-right layout), and the path branches where the ontology branches."""
    edges = inc["evidence_edges"]
    rel = {(a, b): r for a, r, b in edges}
    by_cls = defaultdict(set)
    for a, _, b in edges:
        by_cls[a.split(":")[0]].add(a)
        by_cls[b.split(":")[0]].add(b)
    nodes, links = {}, []

    def node(n, note="", more=None):
        c, key = n.split(":", 1)
        more = len(by_cls.get(c, ())) - 1 if more is None else more
        nodes.setdefault(n, (f"{c}\\n{key}" + (f"\\n(+{more} more)" if more > 0 else "") + (f"\\n{note}" if note else ""),
                             CLASS_COLOR.get(c, "#eee")))

    def link(a, b, label=""):
        if (a, b) in rel:
            links.append((a, b, rel[(a, b)], False))
        elif (b, a) in rel:
            links.append((a, b, rel[(b, a)], True))
        else:
            links.append((a, b, label, False))

    if inc["kind"] == "quality_hold" and inc.get("lots"):
        vs = vehicles()
        lot = f"Lot:{inc['lots'][0]}"
        node(lot)
        part = next((b for a, r, b in edges if a == lot and r == "OF_PART"), None)
        if part:
            node(part)
            link(lot, part)
        prods = [(a, b) for a, r, b in edges if r == "PRODUCED"]
        sold = [(p, v) for p, v in prods if vs.get(v.split(":", 1)[1], {}).get("status") == "sold"]
        unsold = [(p, v) for p, v in prods if vs.get(v.split(":", 1)[1], {}).get("status") == "in_stock"]
        owners = {vs[v.split(":", 1)[1]]["customer"] for _, v in sold}
        dealers = {vs[v.split(":", 1)[1]]["dealer"] for _, v in unsold}
        for group, tail_cls, tail_rel, note, n_tail in ((sold, "Customer", "OWNED_BY", "owner contacted · Rule 6", len(owners)),
                                                        (unsold, "Dealer", "AT_DEALER", "stop sale · Rule 6", len(dealers))):
            if not group:
                continue
            p, v = group[0]
            va = vs[v.split(":", 1)[1]]
            tail = f"{tail_cls}:{va['customer'] if tail_cls == 'Customer' else va['dealer']}"
            node(p)
            node(v, "sold" if tail_cls == "Customer" else "in stock", more=len(group) - 1)
            node(tail, note, more=n_tail - 1)
            link(lot, p)
            link(p, v)
            links.append((v, tail, tail_rel, False))
    else:
        ex = inc["example_path"]
        co_i = next((i for i, n in enumerate(ex) if n.startswith("CustomerOrder:")), len(ex) - 1)
        head, tails = ex[:co_i + 1], ex[co_i + 1:]
        for n in head:
            node(n)
        for a, b in zip(head, head[1:]):
            link(a, b)
        notes = {"Dealer": "notified · Rule 4", "Customer": "loaner if Platinum/Gold · Rule 5"}
        for n in tails:
            node(n, notes.get(n.split(":")[0], ""))
            link(head[-1], n)
    seen, uniq = set(), []
    for l in links:
        if (l[0], l[1]) not in seen:
            seen.add((l[0], l[1]))
            uniq.append(l)
    dot = ['digraph G { rankdir=LR; bgcolor="transparent"; nodesep=0.3; ranksep=0.5; '
           'node [shape=box, style="rounded,filled", color="#b8c2cc", fontname=Helvetica, fontsize=10, margin="0.12,0.06"]; '
           'edge [color="#7b8794", fontsize=9, fontname=Helvetica, fontcolor="#52606d"];']
    dot += [f'"{n}" [label="{lab}", fillcolor="{col}"];' for n, (lab, col) in nodes.items()]
    dot += [f'"{a}" -> "{b}" [label="{r}"{", dir=back" if back else ""}];' for a, b, r, back in uniq]
    return "\n".join(dot + ["}"])


def badges(cats) -> str:
    out = ""
    for c in cats:
        lab, fg, bg = CAT.get(c, (c, "#333", "#eee"))
        out += f'<span class="badge" style="color:{fg};background:{bg}">{html.escape(lab)}</span>'
    return out


def esc(x) -> str:
    return html.escape(str(x))


def counts(t: dict) -> Counter:
    return Counter(r["tool"] for r in t["executed"] if r.get("ok") and r["tool"] in WRITE)


_found = [p for p in RUNS.glob("*") if (p / "traces").exists()] if RUNS.exists() else []
# newest first, so the view opens on the latest run rather than whichever name sorts first
runs = [p.name for p in sorted(_found, key=lambda p: (p / "run_summary.json").stat().st_mtime
                               if (p / "run_summary.json").exists() else 0, reverse=True)]
if not runs:
    st.error("No runs found. Run `python -m control_tower.run --twin --out runs/offline` first.")
    st.stop()
with st.sidebar:
    st.header("Run")
    run = st.selectbox("Run folder", runs, index=runs.index("final") if "final" in runs else 0)
traces, log, summary, onto = load_run(run)
llm = summary.get("llm")
model_chip = f"{llm['model']} · {llm['backend']}" if llm else "rule reader (no LLM)"

st.markdown(f"""
<div class="topbar"><div><div class="brand">Wacky Wheels<span>.</span> Control Tower</div>
<div class="sub">Today is Mon 2027-03-08 · every event is handled in its own copy of the world · every plan is proven
on a digital twin before it touches the live systems</div></div>
<div><span class="chip">run: {esc(run)}</span><span class="chip">reader: {esc(model_chip)}</span>
<span class="chip">transfer: {esc(summary.get('settings', {}).get('transfer_anchor', '?'))}</span></div></div>
""", unsafe_allow_html=True)

n_inc = sum(len(t["incidents"]) for t in traces.values())
n_esc = sum(bool(t["escalation"]) for t in traces.values())
n_noise = sum(t["category"] == ["noise"] for t in traces.values())
n_fail = sum(not r["ok"] for r in log)
n_writes = sum(1 for r in log if r["tool"] in WRITE and r["ok"])
st.markdown(f"""
<div class="kpis">
<div class="kpi"><div class="v">{len(traces)}</div><div class="l">Events in feed</div></div>
<div class="kpi"><div class="v">{n_inc}</div><div class="l">Incidents handled</div></div>
<div class="kpi warn"><div class="v">{n_esc}</div><div class="l">Escalated to a human</div></div>
<div class="kpi mute"><div class="v">{n_noise}</div><div class="l">Ignored as noise</div></div>
<div class="kpi"><div class="v">{n_writes}</div><div class="l">Corrective actions</div></div>
<div class="kpi {'good' if n_fail == 0 else 'warn'}"><div class="v">{n_fail}</div><div class="l">Failed calls</div></div>
</div>""", unsafe_allow_html=True)

tab_main, tab_hitl, tab_live, tab_eff, tab_onto, tab_audit = st.tabs(
    ["Control tower", f"Human review ({n_esc})", "Live replay", "Efficiency & model", "Ontology", "Audit log & downloads"])

# =============================================================================== control tower
with tab_main:
    ids = list(traces)
    if "sel" not in st.session_state or st.session_state.sel not in ids:
        st.session_state.sel = ids[0]
    feed, detail = st.columns([2, 5], gap="large")

    with feed:
        st.markdown("##### Event feed")
        box = st.container(height=760, border=False)
        for eid in ids:
            t = traces[eid]
            label = " + ".join(CAT.get(c, (c,))[0] for c in t["category"])
            if box.button(f"{eid} · {label}", key=f"b_{eid}", type="primary" if eid == st.session_state.sel else "secondary"):
                st.session_state.sel = eid
                st.rerun()
            snippet = t["event"]["text"][:92].replace("\n", " ") + ("…" if len(t["event"]["text"]) > 92 else "")
            box.markdown(f'<div class="feedrow">{esc(t["event"].get("received_at", "")[11:16])} · '
                         f'{esc(t["event"].get("source", ""))} · <b>{sum(counts(t).values())}</b> actions<br>'
                         f'{esc(snippet)}</div>', unsafe_allow_html=True)

    with detail:
        eid = st.session_state.sel
        t = traces[eid]
        acted = bool(t["incidents"])
        st.markdown(f'<div style="margin-bottom:8px">{badges(t["category"])} '
                    f'<span style="color:#667085;font-size:13px">{esc(eid)} · {esc(t["event"].get("source", ""))} · '
                    f'{esc(t["event"].get("received_at", ""))}</span></div>', unsafe_allow_html=True)
        st.markdown(f'<div class="card msg"><h4>Incoming message</h4>{esc(t["event"]["text"])}</div>', unsafe_allow_html=True)
        tone = "act" if acted and not t["escalation"] else ("esc" if t["escalation"] else "none")
        head = {"act": "Decision · act within policy", "esc": "Decision · escalate to a human",
                "none": "Decision · no action"}[tone]
        st.markdown(f'<div class="card verdict {tone}"><h4>{head}</h4>{esc(t["summary"])}</div>', unsafe_allow_html=True)

        # ---- pipeline stepper
        v = t["verdict"]
        dr = t["dry_run"]
        st.markdown(stepper_html(pipeline_steps(t)), unsafe_allow_html=True)

        # ---- outcome cards
        c = counts(t)
        owners = c["crm.notify_customer"]
        cards = [("Orders moved", c["mes.reschedule"] + c["mes.transfer"]), ("Transferred", c["mes.transfer"]),
                 ("Dealer notices", c["dealer.notify"]), ("Loaners", c["crm.assign_loaner"]),
                 ("Stop-sales", c["dealer.stop_sale"]), ("Owners contacted", owners), ("Lots held", c["quality.hold_lot"])]
        cards = [(a, b) for a, b in cards if b]
        if cards:
            st.markdown('<div class="out">' + "".join(f"<div><b>{b}</b><span>{a}</span></div>" for a, b in cards) + "</div>",
                        unsafe_allow_html=True)

        has_loaners = any(d["entitled"] for d in t.get("loaners") or [])
        tab_names = ["Graph path", "Actions taken"] + (["Loaners"] if has_loaners else []) + ["Reasoning & audit"] \
            + (["Schedule impact"] if t["moves"] else []) + (["Proof"] if dr else []) + ["How it was read"]
        tabs = dict(zip(tab_names, st.tabs(tab_names)))

        # ---- graph path
        with tabs["Graph path"]:
            if not t["incidents"]:
                if t.get("human_review"):
                    st.markdown('<div class="card"><h4>No graph traversal: nothing in the message matched a record</h4>'
                                + review_html(t["human_review"]) + "</div>", unsafe_allow_html=True)
                else:
                    reason = (v["noise_reasons"] or v["escalations"] or ["no operational entity found"])[0]
                    st.info(f"No graph traversal. {reason.split('] ', 1)[-1][:1].upper() + reason.split('] ', 1)[-1][1:]}.")
            for inc in t["incidents"]:
                edges = inc["evidence_edges"]
                nodes_by_cls = defaultdict(set)
                for a, _, b in edges:
                    nodes_by_cls[a.split(":")[0]].add(a)
                    nodes_by_cls[b.split(":")[0]].add(b)
                st.markdown(f"**{esc(inc['id'])}** &nbsp; `{' → '.join(inc['path_schema'])}`", unsafe_allow_html=True)
                st.graphviz_chart(path_dot(inc), **STRETCH)
                st.caption("Every arrow is a relation in the ontology, pointing the way the relation runs. "
                           "The path branches where the records do.")
                m1, m2, m3 = st.columns(3)
                if inc["kind"] == "quality_hold":
                    m1.metric("Production orders built with the lot", len(nodes_by_cls.get("ProductionOrder", ())))
                    m2.metric("Vehicles affected", inc["vehicles"])
                    m3.metric("Dealers with a stop-sale", sum(r["tool"] == "dealer.stop_sale" and r.get("ok")
                                                              for r in t["executed"]))
                else:
                    m1.metric("Production orders", len(inc["affected_production_orders"]))
                    m2.metric("Customer orders", len(nodes_by_cls.get("CustomerOrder", ())))
                    m3.metric("Dealers reached", len(nodes_by_cls.get("Dealer", ())))
                with st.expander("Grounding checks and every evidence edge"):
                    st.markdown("\n".join(f"- {x}" for x in inc["grounding_checks"]))
                    st.dataframe(pd.DataFrame(edges, columns=["from", "relation", "to"]), hide_index=True, **STRETCH)

        # ---- actions
        with tabs["Actions taken"]:
            if not t["executed"]:
                st.info("No tool calls — " + ((v["noise_reasons"] or ["nothing to do"])[0]))
            else:
                by_agent = defaultdict(Counter)
                for r in t["executed"]:
                    if r.get("ok") and r["tool"] in WRITE:
                        by_agent[r.get("agent", "?")][r["tool"]] += 1
                cols = st.columns(max(1, len(by_agent)))
                for col, (ag, cn) in zip(cols, by_agent.items()):
                    col.markdown(f'<div class="card"><h4 style="color:{AGENT_COLOR.get(ag, "#667085")}">{esc(ag)}</h4>'
                                 + "".join(f"<div style='font-size:13px'><b>{n}</b> × {esc(tool)} "
                                           f"<span style='color:#98a2b3'>· {RULE.get(tool, '')}</span></div>"
                                           for tool, n in cn.items())
                                 + "</div>", unsafe_allow_html=True)
                # what the recipients actually received
                samples = []
                for tool, title in (("dealer.notify", "Dealer notice"), ("dealer.stop_sale", "Stop-sale order"),
                                    ("crm.notify_customer", "Owner notice"), ("escalate", "Hand-off to a human")):
                    r = next((r for r in t["executed"] if r["tool"] == tool and r.get("ok")), None)
                    if not r:
                        continue
                    a = r["args"]
                    to = a.get("dealer_id") or a.get("customer_id") or "operations reviewer"
                    body = a.get("message") or a.get("reason") or \
                        f"Stop sale of {len(a.get('vins', []))} vehicle(s): {', '.join(a.get('vins', [])[:8])}" \
                        + (" …" if len(a.get("vins", [])) > 8 else "")
                    n_same = sum(x["tool"] == tool and x.get("ok") for x in t["executed"])
                    samples.append((title, named(to), body, n_same))
                if samples:
                    with st.expander(f"What recipients received ({len(samples)} sample message"
                                     f"{'s' if len(samples) > 1 else ''})", expanded=not acted):
                        for title, to, body, n_same in samples:
                            st.markdown(f'<div class="card"><h4>{esc(title)} · to {esc(to)}'
                                        f'{f" · 1 of {n_same}" if n_same > 1 else ""}</h4>'
                                        f'<div style="font-size:13px;line-height:1.5">{esc(body)}</div></div>',
                                        unsafe_allow_html=True)
                acts = pd.DataFrame([{"agent": r.get("agent"), "tool": r["tool"], "rule": RULE.get(r["tool"], ""),
                                      "ok": "✔" if r.get("ok") else ("blocked" if r.get("blocked") else
                                                                     "skipped" if r.get("skipped") else "✖"),
                                      "target": named(str(next(iter(r["args"].values())))) if r["args"] else "",
                                      "why": r.get("why", "")}
                                     for r in t["executed"]])
                st.dataframe(acts, hide_index=True, height=360, **STRETCH)
            d1, d2, _ = st.columns([1, 1, 2])
            d1.download_button("⬇ Trace (JSON)", json.dumps(t, indent=1, default=str), file_name=f"{eid}_trace.json",
                               mime="application/json", key=f"dl_t_{eid}")
            rep = RUNS / run / "reports" / f"{eid}.md"
            if rep.exists():
                d2.download_button("⬇ Report (Markdown)", rep.read_text(encoding="utf-8"), file_name=f"{eid}.md",
                                   mime="text/markdown", key=f"dl_r_{eid}")

        # ---- loaners (rule 5): who was entitled, in what order, and who got one
        if has_loaners:
            with tabs["Loaners"]:
                al = t["loaners"]
                ent = [x for d in al for x in d["entitled"]]
                got = sum(x["loaner"] for x in ent)
                contested = [d for d in al if len(d["entitled"]) > d["pool"]]
                std = sum(d.get("standard_orders", 0) for d in al)
                st.markdown(f'<div class="out"><div><b>{got}</b><span>loaners assigned</span></div>'
                            f'<div><b>{len(ent)}</b><span>Platinum/Gold customers entitled</span></div>'
                            f'<div><b>{len(contested)}</b><span>dealers with more entitled customers than loaners</span></div>'
                            f'<div><b>{std}</b><span>Standard orders · dealer told, no loaner</span></div></div>',
                            unsafe_allow_html=True)
                st.caption("Rule 5: one loaner from the customer's dealer pool; when the pool is short, Platinum before Gold, "
                           "then the earlier promised date, then the lower customer order id.")
                rows = [{"dealer": named(d["dealer"]), "pool": d["pool"], "priority": x["rank"], "tier": x["tier"],
                         "promised": x["promised"], "customer order": x["customer_order"], "customer": x["customer"],
                         "loaner": "✔ assigned" if x["loaner"] else "✖ pool used up", "incident": x.get("incident") or ""}
                        for d in sorted(al, key=lambda d: (len(d["entitled"]) <= d["pool"], d["dealer"])) for x in d["entitled"]]
                if contested:
                    st.markdown("**Where the pool ran short**")
                    st.dataframe(pd.DataFrame([r for r in rows if any(named(d["dealer"]) == r["dealer"] for d in contested)]),
                                 hide_index=True, **STRETCH)
                with st.expander(f"All {len(rows)} entitled customers"):
                    st.dataframe(pd.DataFrame(rows), hide_index=True, height=380, **STRETCH)

        # ---- reasoning & audit
        with tabs["Reasoning & audit"]:
            items = "".join(
                f'<div class="it" style="--c:{AGENT_COLOR.get(a["agent"], "#98a2b3")}"><span class="ag">{esc(a["agent"])}</span>'
                f'<span class="st">{esc(a["step"])}</span>{esc(a["detail"][:600])}</div>' for a in t["audit"])
            st.markdown(f'<div class="card"><h4>Audit trail</h4><div class="tl">{items}</div></div>', unsafe_allow_html=True)

        # ---- schedule
        if t["moves"]:
            with tabs["Schedule impact"]:
                po = base_schedule()
                plants = sorted({m["from"][0] for m in t["moves"]} | {m["to"][0] for m in t["moves"]})
                before = po[po.plant_id.isin(plants)].groupby(["plant_id", "build_date"]).size().rename("used").reset_index()
                after_counts = Counter({k_: n for k_, n in zip(zip(before.plant_id, before.build_date), before.used)})
                for m in t["moves"]:
                    after_counts[tuple(m["from"])] -= 1
                    after_counts[tuple(m["to"])] += 1
                # an outage stops the whole plant ("down"); a late part only stops models that need it ("parts")
                blocked = {}
                for inc in t["incidents"]:
                    if inc["blocked"]:
                        b = inc["bindings"]
                        what = "plant down" if inc["kind"] == "plant_outage" else f"waiting for {b.get('part', 'a part')}"
                        for d in before.build_date.unique():
                            if inc["blocked"][0] <= d <= inc["blocked"][1]:
                                blocked[(b["plant"], d)] = what
                moved_in = Counter(tuple(m["to"]) for m in t["moves"])
                df = pd.DataFrame([{"plant": p, "date": d, "used": n, "state": "after", "moved_in": moved_in.get((p, d), 0),
                                    "blocked": blocked.get((p, d), "")} for (p, d), n in after_counts.items()] +
                                  [{"plant": p, "date": d, "used": n, "state": "before", "moved_in": 0,
                                    "blocked": blocked.get((p, d), "")} for p, d, n in before.itertuples(index=False)])
                which = st.radio("View", ["after", "before"], horizontal=True, label_visibility="collapsed")
                view = df[df.state == which]
                base = alt.Chart(view).encode(x=alt.X("date:O", title=None, axis=alt.Axis(labelAngle=-45)),
                                              y=alt.Y("plant:N", title=None))
                heat = base.mark_rect(cornerRadius=3, stroke="#ffffff", strokeWidth=2).encode(
                    color=alt.Color("used:Q", title="slots used (of 5)", scale=alt.Scale(domain=[0, 5], range=SEQ)),
                    tooltip=["plant", "date", alt.Tooltip("used", title="slots used"), alt.Tooltip("moved_in", title="orders moved in"),
                             alt.Tooltip("blocked", title="status")])
                txt = base.mark_text(fontSize=10).encode(
                    text=alt.Text("label:N"), color=alt.condition("datum.used >= 4", alt.value("#ffffff"), alt.value("#0b0b0b"))
                ).transform_calculate(label="datum.blocked == 'plant down' ? 'down' : (datum.blocked ? 'parts' : "
                                            "(datum.moved_in > 0 ? '+' + datum.moved_in : ''))")
                st.altair_chart((heat + txt).properties(height=40 * len(plants) + 60), **STRETCH)
                st.caption("Colour = slots used out of 5 per plant and day. 'down' = plant outage (nothing builds); "
                           "'parts' = only models that need the late part are blocked; '+n' = orders moved in.")
                st.dataframe(pd.DataFrame([{"order": m["po"], "model": m["model"], "from": " ".join(m["from"]),
                                            "to": " ".join(m["to"]), "type": m["type"], "why": m["why"]} for m in t["moves"]]),
                             hide_index=True, height=300, **STRETCH)

        # ---- proof
        if dr:
            with tabs["Proof"]:
                st.caption("The plan is replayed on a digital twin and checked against an independent, table-only oracle "
                           "before any live call. Any failing check withholds the plan.")
                st.dataframe(pd.DataFrame(dr["checks"]).assign(ok=lambda d: d.ok.map({True: "✔ pass", False: "✖ fail"})),
                             hide_index=True, **STRETCH)

        # ---- how it was read
        with tabs["How it was read"]:
            dp = t.get("dates_plain") or {}
            if dp.get("dates"):
                st.markdown("**Dates in the message**")
                st.dataframe(pd.DataFrame([{"written as": f"“{d['phrase']}”", "means": d["role"], "read as": d["date"],
                                            "other possible reading": ", ".join(d["alternative"]) or "—",
                                            "incident": CAT.get(d["incident"], (d["incident"],))[0]} for d in dp["dates"]]),
                             hide_index=True, **STRETCH)
                if dp.get("why"):
                    st.info(dp["why"])
            elif "same under every" not in v.get("convention_note", "same under every"):
                st.markdown(f"*Date reading:* {v['convention_note']}")
            st.markdown("**What each reader saw**")
            for r in v["readings"]:
                rd = r["reading"]
                found = [f"{lab}: " + ", ".join(named(i) for i in rd[k]) for k, lab in
                         (("plants", "Plant"), ("parts", "Part"), ("suppliers", "Supplier"), ("shipments", "Shipment"),
                          ("lots", "Lot")) if rd[k]]
                src = "LLM " + rd["source"].split(":", 1)[1] if rd["source"].startswith("llm:") else "Rule reader"
                st.markdown(f"- **{esc(src)}** read *{CAT.get(rd['kind'], (rd['kind'],))[0].split(' ·')[0].lower()}* "
                            f"→ **{STATUS_WORDS.get(r['status'], r['status'])}**  \n"
                            f"  {'; '.join(found) if found else 'no specific plant, part, supplier, shipment or lot'}")
            for g in v["incidents"]:
                st.markdown(f"**Matched {CAT.get(g['kind'], (g['kind'],))[0].lower()}** — " + "; ".join(g["checks"]))
            if t.get("human_review"):
                st.markdown('<div class="card"><h4>Why a human must review</h4>' + review_html(t["human_review"]) + "</div>",
                            unsafe_allow_html=True)
            elif v["escalations"]:
                for e in v["escalations"]:
                    st.markdown(f"**Escalation reason:** {e}")

# =============================================================================== human review
with tab_hitl:
    esc_ids = [e for e, t in traces.items() if t.get("escalation")]
    st.caption("Every report the system would not act on alone. The escalate tool has already handed each one to a "
               "person; the buttons record a reviewer's response for this session only.")
    if not esc_ids:
        st.success("Nothing needs human review in this run.")
    status = st.session_state.setdefault("review_status", {})
    for e in esc_ids:
        t = traces[e]
        items = t.get("human_review") or [{"headline": "Escalated", "why": [t["escalation"]], "need": ""}]
        s = status.get(e, "open")
        chip = {"open": ("Open", "#9b1c2c", "#fbe0e3"), "ack": ("Acknowledged", "#1d7a55", "#e3f3e8"),
                "ask": ("Asked sender for details", "#8a5200", "#fdf0d8")}[s]
        st.markdown(f'<div class="card"><div style="margin-bottom:6px">{badges(t["category"])}'
                    f'<span class="badge" style="color:{chip[1]};background:{chip[2]}">{chip[0]}</span>'
                    f'<span style="color:#667085;font-size:13px">{esc(e)} · {esc(t["event"].get("source", ""))} · '
                    f'{esc(t["event"].get("received_at", ""))}</span></div>'
                    f'<div style="font-size:14px;border-left:3px solid #c82132;padding-left:10px;margin:6px 0 10px">'
                    f'{esc(t["event"]["text"])}</div>{review_html(items)}</div>', unsafe_allow_html=True)
        b1, b2, _ = st.columns([1, 1.4, 3])
        if b1.button("Acknowledge", key=f"ack_{e}"):
            status[e] = "ack"
            st.rerun()
        if b2.button("Ask the sender for details", key=f"ask_{e}"):
            status[e] = "ask"
            st.rerun()

# =============================================================================== live replay (for the demo)
with tab_live:
    c1, c2 = st.columns([1, 3], gap="large")
    with c1:
        pace = {"slow": 1.4, "normal": 0.8, "fast": 0.3}[st.select_slider("Speed", ["slow", "normal", "fast"], value="normal")]
        go = st.button("▶ Replay the feed", type="primary", key="replay")
        st.caption("Events arrive one at a time and move through read → ground → plan → prove → execute, "
                   "exactly as recorded in this run.")
    with c2:
        stage = st.empty()
        done = st.container()
        if not go:
            stage.info("Press ▶ to replay the event feed.")
        else:
            for e, t in traces.items():
                steps = pipeline_steps(t)
                for k in range(6):
                    stage.markdown(
                        f'<div class="card msg"><h4>Incoming · {esc(t["event"].get("received_at", "")[11:16])} · '
                        f'{esc(t["event"].get("source", ""))} · {esc(e)}</h4>{esc(t["event"]["text"])}</div>'
                        + stepper_html(steps, upto=k)
                        + (f'<div class="card verdict {"act" if t["incidents"] and not t["escalation"] else "esc" if t["escalation"] else "none"}">'
                           f'<h4>Decision</h4>{esc(t["summary"])}</div>' if k == 5 else ""),
                        unsafe_allow_html=True)
                    time.sleep(pace * (1.6 if k == 0 else 0.45 if k < 5 else 1.4))
                done.markdown(f'<div class="feedrow">{badges(t["category"])} <b>{esc(e)}</b> · '
                              f'{sum(counts(t).values())} actions{" · escalated" if t["escalation"] else ""}</div>',
                              unsafe_allow_html=True)
            stage.success(f"Feed replayed: {len(traces)} events.")

# =============================================================================== efficiency & model
with tab_eff:
    tool_n = Counter(r["tool"] for r in log)
    n_reads = sum(n for k, n in tool_n.items() if k in READS)
    n_esc_calls = tool_n.get("escalate", 0)
    n_blocked = sum(t.get("stats", {}).get("blocked_by_catalog", 0) for t in traces.values())
    model_name = llm["model"] if llm else "None in this run"
    st.markdown(f"""
<div class="kpis">
<div class="kpi"><div class="v" style="font-size:20px">{esc(model_name)}</div><div class="l">Model used ({esc(llm['backend'] if llm else 'rule reader only')})</div></div>
<div class="kpi"><div class="v">{llm['calls'] if llm else 0}</div><div class="l">LLM calls</div></div>
<div class="kpi"><div class="v">{f"{llm['tokens']:,}" if llm else 0}</div><div class="l">LLM tokens</div></div>
<div class="kpi"><div class="v">{f"{llm['seconds']}s" if llm else "—"}</div><div class="l">LLM time</div></div>
<div class="kpi"><div class="v">{summary.get('seconds', '—')}s</div><div class="l">End-to-end run</div></div>
<div class="kpi {'good' if n_fail == 0 else 'warn'}"><div class="v">{n_fail}</div><div class="l">Failed tool calls</div></div>
</div>
<div class="kpis">
<div class="kpi"><div class="v">{len(log)}</div><div class="l">Tool calls in total</div></div>
<div class="kpi"><div class="v">{n_writes}</div><div class="l">Writes (corrective actions)</div></div>
<div class="kpi mute"><div class="v">{n_reads}</div><div class="l">Reads (free, never penalised)</div></div>
<div class="kpi warn"><div class="v">{n_esc_calls}</div><div class="l">Escalations</div></div>
<div class="kpi {'good' if n_blocked == 0 else 'warn'}"><div class="v">{n_blocked}</div><div class="l">Calls blocked by catalog check</div></div>
<div class="kpi"><div class="v">{sum(len(t.get('plan', [])) for t in traces.values())}</div><div class="l">Calls proven on the twin</div></div>
</div>""", unsafe_allow_html=True)
    if not llm:
        st.warning("This run used the rule reader only, so there is no model to cite here. Runs made with "
                   "CT_LLM set show the model, its calls, tokens and time.")
    tc = summary.get("tool_catalog")
    st.markdown(f"**Tool discovery (MCP-style).** " + (
        f"{len(tc['tools'])} tools discovered via `{tc['source']}`. Every call is checked against the catalog "
        f"(tool name, argument names, argument types) before it is sent; {n_blocked} were blocked." if tc else
        "This run predates tool discovery; re-run to record it."))
    st.markdown("**Where the LLM and plain code share the work.** The LLM only reads the message (kind of incident, names "
                "and date phrases as written). Code turns names into ids and phrases into dates, the knowledge graph decides "
                "which reading is real, and the handbook is applied as code, so the model never chooses an action.")
    rows = [{"event": e, "category": " + ".join(CAT.get(c, (c,))[0] for c in t["category"]),
             "reads": t.get("stats", {}).get("reads", sum(r["tool"] in READS and r.get("ok") for r in t["executed"])),
             "writes": t["stats"]["writes"], "escalations": sum(r["tool"] == "escalate" for r in t["executed"]),
             "failed": t["stats"]["failed_live_calls"], "seconds": t["stats"]["seconds"]} for e, t in traces.items()]
    st.dataframe(pd.DataFrame(rows), hide_index=True, **STRETCH)
    mach = summary.get("machine", {})
    st.caption(f"Run on Python {mach.get('python', '?')} · {mach.get('platform', '?')} · "
               f"transfer anchor: {summary.get('settings', {}).get('transfer_anchor', '?')}")

# =============================================================================== ontology
with tab_onto:
    if onto:
        dot = ['digraph O { rankdir=LR; bgcolor="transparent"; nodesep=0.4; ranksep=0.7; node [shape=box, style="rounded,filled", '
               'color="#b8c2cc", fontname=Helvetica, margin="0.16,0.09"]; edge [color="#7b8794", fontname=Helvetica, fontsize=9];']
        for cls in onto.get("classes", {}):
            dot.append(f'"{cls}" [fillcolor="{CLASS_COLOR.get(cls, "#eee")}"];')
        for a, r, b in onto["relations"]:
            style = ", style=dashed, color=\"#c82132\", fontcolor=\"#c82132\"" if r in onto.get("inferred", {}) else ""
            dot.append(f'"{a}" -> "{b}" [label="{r}"{style}];')
        dot.append("}")
        st.graphviz_chart("\n".join(dot), **STRETCH)
        st.caption("Dashed red = inferred relation. " + "; ".join(f"{k_}: {v_}" for k_, v_ in onto.get("inferred", {}).items()))
        s = onto.get("stats", {})
        st.markdown(f"**{s.get('nodes'):,} nodes · {s.get('edges'):,} edges**")
        c1, c2 = st.columns(2)
        c1.dataframe(pd.DataFrame(s.get("nodes_by_class", {}).items(), columns=["class", "nodes"]), hide_index=True, **STRETCH)
        c2.dataframe(pd.DataFrame(s.get("edges_by_relation", {}).items(), columns=["relation", "edges"]), hide_index=True, **STRETCH)

# =============================================================================== audit log
with tab_audit:
    d1, d2, _ = st.columns([1, 1.3, 2])
    if (RUNS / run / "action_log.jsonl").exists():
        d1.download_button("⬇ action_log.jsonl", (RUNS / run / "action_log.jsonl").read_bytes(),
                           file_name="action_log.jsonl", mime="application/x-ndjson")
    d2.download_button("⬇ Run bundle (.zip)", bundle(run), file_name=f"{run}_bundle.zip", mime="application/zip",
                       help="action log, every trace, per-event reports, ontology export and run summary")
    if log:
        df = pd.DataFrame(log)
        f1, f2 = st.columns(2)
        ev = f1.multiselect("Event", sorted(df.event_id.unique()))
        tl = f2.multiselect("Tool", sorted(df.tool.unique()))
        if ev:
            df = df[df.event_id.isin(ev)]
        if tl:
            df = df[df.tool.isin(tl)]
        df["args"] = df["args"].map(lambda a: json.dumps(a)[:160])
        st.dataframe(df, hide_index=True, height=560, **STRETCH)
        st.caption(f"{len(df)} rows from {run}/action_log.jsonl — the file submitted for replay.")
