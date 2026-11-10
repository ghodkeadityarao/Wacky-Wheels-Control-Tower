"""LLM reader. The model reads; code computes.

The LLM returns, per incident, the kind plus the *surface strings* it saw for plant/part/supplier and
the raw date phrases. Ids come from the resolver and dates from the date parser, so the model never
does calendar arithmetic or invents an id. Backends: Ollama (/api/chat) or any OpenAI-compatible
server (llama.cpp, LM Studio, vLLM). Stdlib HTTP only.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from .nlp import Reading, Resolver, find_dates

SYSTEM = """You read operational messages for an automaker's control tower. Today is Monday 2027-03-08.
Split the message into separate incidents and classify each as exactly one of:
- supplier_delay: an inbound shipment of a part will arrive later than planned
- plant_outage: a plant cannot build for some days
- quality_hold: a part lot is defective / suspect for vehicles built in a window
- noise: not about our plants, parts, suppliers or shipments (brochures, prices, far-away weather)
- unconfirmed: a rumor or vague report (no specifics, hedged, or explicitly not confirmed)
Copy names and date phrases EXACTLY as written in the message; do not convert them to ids or ISO dates.
Return JSON only:
{"incidents":[{"kind":"...","evidence":"<the sentence(s) for this incident>","plant":null,"part":null,
"supplier":null,"shipment_id":null,"lot_id":null,"dates":{"start":null,"end":null,"original_eta":null,
"new_eta":null},"confirmed":true,"reason":"<one sentence>"}]}"""

SCHEMA = {
    "type": "object",
    "properties": {"incidents": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["supplier_delay", "plant_outage", "quality_hold", "noise", "unconfirmed"]},
            "evidence": {"type": "string"}, "plant": {"type": ["string", "null"]}, "part": {"type": ["string", "null"]},
            "supplier": {"type": ["string", "null"]}, "shipment_id": {"type": ["string", "null"]},
            "lot_id": {"type": ["string", "null"]},
            "dates": {"type": "object", "properties": {k: {"type": ["string", "null"]} for k in
                                                       ("start", "end", "original_eta", "new_eta")}},
            "confirmed": {"type": "boolean"}, "reason": {"type": "string"}},
        "required": ["kind", "evidence", "dates", "confirmed", "reason"]}}},
    "required": ["incidents"],
}


class LLMError(RuntimeError):
    pass


class Backend:
    def __init__(self, kind: str, model: str, url: str | None = None, timeout: float = 120):
        self.kind, self.model, self.timeout = kind, model, timeout
        self.url = url or {"ollama": "http://127.0.0.1:11434", "openai": "http://127.0.0.1:8080/v1"}[kind]
        self.calls, self.seconds, self.tokens = 0, 0.0, 0

    @classmethod
    def from_env(cls):
        kind = os.environ.get("CT_LLM", "none")
        if kind == "none":
            return None
        return cls(kind, os.environ.get("CT_LLM_MODEL", "qwen2.5:7b-instruct"), os.environ.get("CT_LLM_URL"))

    def _post(self, path, payload, headers=None):
        req = urllib.request.Request(self.url.rstrip("/") + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def complete_json(self, user: str) -> dict:
        t0 = time.time()
        try:
            if self.kind == "ollama":
                res = self._post("/api/chat", {"model": self.model, "stream": False, "format": SCHEMA,
                                               "options": {"temperature": 0},
                                               "messages": [{"role": "system", "content": SYSTEM},
                                                            {"role": "user", "content": user}]})
                text = res["message"]["content"]
                self.tokens += res.get("prompt_eval_count", 0) + res.get("eval_count", 0)
            else:
                key = os.environ.get("CT_LLM_API_KEY")
                res = self._post("/chat/completions", {"model": self.model, "temperature": 0,
                                                       "response_format": {"type": "json_object"},
                                                       "messages": [{"role": "system", "content": SYSTEM},
                                                                    {"role": "user", "content": user}]},
                                 {"Authorization": f"Bearer {key}"} if key else None)
                text = res["choices"][0]["message"]["content"]
                self.tokens += res.get("usage", {}).get("total_tokens", 0)
        except Exception as e:  # network, HTTP, malformed payload
            raise LLMError(f"{self.kind} call failed: {e}") from e
        finally:
            self.calls += 1
            self.seconds += time.time() - t0
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError(f"no JSON in model output: {text[:200]!r}")
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"bad JSON from model: {e}") from e


class LLMReader:
    def __init__(self, backend: Backend, resolver: Resolver):
        self.backend, self.resolver = backend, resolver
        self.name = f"llm:{backend.model}"

    def read(self, text: str) -> list[Reading]:
        data = self.backend.complete_json(f"Message:\n{text}")
        out = []
        for i, inc in enumerate(data.get("incidents") or []):
            kind = inc.get("kind")
            if kind not in ("supplier_delay", "plant_outage", "quality_hold", "noise", "unconfirmed"):
                continue
            ev = inc.get("evidence") or text
            rv = self.resolver
            r = Reading(self.name, i, ev, kind, rationale=inc.get("reason", ""))
            r.plants = rv.resolve_surface(inc.get("plant"), "Plant")
            r.parts = rv.resolve_surface(inc.get("part"), "Part")
            r.suppliers = rv.resolve_surface(inc.get("supplier"), "Supplier")
            r.shipments = rv.resolve_surface(inc.get("shipment_id"), "Shipment")
            r.lots = rv.resolve_surface(inc.get("lot_id"), "Lot")
            if len(r.parts) > 1:                      # vague part word: keep as a hint, not a binding
                r.vague_parts.append({"surface": inc.get("part"), "could_be": sorted(r.parts)})
                r.parts = set()
            for role, phrase in (inc.get("dates") or {}).items():
                if phrase:
                    found = find_dates(str(phrase))
                    if found:
                        r.dates[role] = found[0]
            if not inc.get("confirmed", True) and kind != "noise":
                r.unconfirmed.append("llm: confirmed=false")
            out.append(r)
        return out
