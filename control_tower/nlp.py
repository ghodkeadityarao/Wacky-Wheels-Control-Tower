"""Language layer: date phrases -> candidate dates, names -> ids, and a lexical reader.

Nothing here decides an action. Every ambiguous phrase is kept as a *set of candidates*
("next Friday" -> {next_week: 2027-03-19, upcoming: 2027-03-12}); grounding against the
knowledge graph picks among them later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .kg import KG, START, key_of, nid

# --------------------------------------------------------------------------- dates
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
          "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_WD = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tues|thurs|thur|mon|tue|wed|thu|fri|sat|sun)"
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
DATE_PATTERNS = [  # (kind, regex) in priority order; earlier kinds win overlaps
    ("iso", re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")),
    ("month_day", re.compile(rf"\b(?:{_WD}\.?,?\s+)?{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d\d))?\b", re.I)),
    ("relative", re.compile(rf"\b(next|last|this|coming|past|previous)\s+{_WD}\b", re.I)),
    ("word", re.compile(r"\b(today|tomorrow|yesterday)\b", re.I)),
    ("bare", re.compile(rf"\b{_WD}\b", re.I)),
]

# Conventions for relative weekday phrases. "next_week" = that weekday in the following Mon-Fri
# week; "upcoming" = the first such weekday after today. They differ for "next Tue..Fri" said on a Monday.
CONVENTIONS = ("next_week", "upcoming")


@dataclass
class DateMention:
    phrase: str
    start: int                      # char offset (for ordering)
    candidates: dict                # convention/label -> date
    note: str = ""

    def resolve(self, convention: str) -> list[tuple[str, date]]:
        """Dates this mention can mean under a message-level convention."""
        c = self.candidates
        if "explicit" in c:
            return [("explicit", c["explicit"])]
        if convention in c:
            return [(convention, c[convention])]
        return list(c.items())      # e.g. bare weekday: both upcoming and recent stay open

    def to_json(self):
        return {"phrase": self.phrase, "candidates": {k: v.isoformat() for k, v in self.candidates.items()},
                "note": self.note}


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _relative(word: str, wd: int, today: date = START) -> dict:
    word = word.lower()
    mon = _week_monday(today)
    if word == "next":
        nxt = mon + timedelta(days=7 + wd)
        up = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
        return {"next_week": nxt, "upcoming": up}
    if word in ("this", "coming"):
        d = mon + timedelta(days=wd)
        if d <= today and word == "coming":
            d += timedelta(days=7)
        return {"explicit": d}
    # last / past / previous
    prev = mon - timedelta(days=7) + timedelta(days=wd)
    recent = today - timedelta(days=(today.weekday() - wd) % 7 or 7)
    return {"explicit": prev} if prev == recent else {"prev_week": prev, "recent": recent}


def find_dates(text: str, today: date = START) -> list[DateMention]:
    taken: list[tuple[int, int]] = []
    out: list[DateMention] = []
    for kind, rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            g = m.groups()
            note = ""
            try:
                if kind == "iso":
                    cand = {"explicit": date(int(g[0]), int(g[1]), int(g[2]))}
                elif kind == "month_day":
                    d = date(int(g[3] or today.year), MONTHS[g[1][:3].lower()], int(g[2]))
                    cand = {"explicit": d}
                    if g[0] and WEEKDAYS[g[0].lower()] != d.weekday():
                        note = f"weekday '{g[0]}' does not match {d.isoformat()} ({d:%A})"
                elif kind == "relative":
                    cand = _relative(g[0], WEEKDAYS[g[1].lower()], today)
                elif kind == "word":
                    cand = {"explicit": today + timedelta(days={"today": 0, "tomorrow": 1, "yesterday": -1}[g[0].lower()])}
                else:
                    wd = WEEKDAYS[g[0].lower()]
                    up = today + timedelta(days=(wd - today.weekday()) % 7)
                    rec = today - timedelta(days=(today.weekday() - wd) % 7 or 7)
                    cand = {"upcoming": up, "recent": rec}
            except ValueError:
                continue
            taken.append((m.start(), m.end()))
            out.append(DateMention(m.group(0), m.start(), cand, note))
    return sorted(out, key=lambda d: d.start)


# --------------------------------------------------------------------------- names
@dataclass
class Mention:
    cls: str
    ids: frozenset
    surface: str
    start: int

    @property
    def ambiguous(self) -> bool:
        return len(self.ids) > 1


CATEGORY_WORDS = {  # vague words that point at a *set* of parts (never resolved on their own)
    "seat": "seating", "seats": "seating", "seating": "seating", "seat material": "seating",
    "electronic": "electronics", "electronics": "electronics", "chip": "electronics", "chips": "electronics",
    "tire": "tires", "tires": "tires", "rubber": "tires", "wheel": "wheels", "wheels": "wheels", "rims": "wheels",
    "harness": "electrical", "harnesses": "electrical", "wiring": "electrical",
    "battery": "electrical", "batteries": "electrical",
}
OPERATIONAL = {"Plant", "Part", "Supplier", "Shipment", "Lot"}


class Resolver:
    """Maps surface text to entity ids using aliases.csv plus names and ids from the tables."""

    def __init__(self, kg: KG):
        self.kg = kg
        table: dict[str, set] = {}

        def add(surface, cls, key):
            s = surface.strip().lower()
            if len(s) >= 3:
                table.setdefault(s, set()).add((cls, key))

        alias_cls = {"plant": "Plant", "part": "Part", "supplier": "Supplier", "model": "Model", "dealer": "Dealer"}
        for r in kg.tables["aliases"]:
            add(r["alias"], alias_cls.get(r["entity_type"], r["entity_type"].title()), r["entity_id"])
        for r in kg.tables["plants"]:
            add(r["plant_id"], "Plant", r["plant_id"])
            add(r["name"], "Plant", r["plant_id"])
            add(r["name"].replace("Wacky Wheels ", ""), "Plant", r["plant_id"])
        for r in kg.tables["suppliers"]:
            add(r["supplier_id"], "Supplier", r["supplier_id"])
            add(r["name"], "Supplier", r["supplier_id"])
            add(r["name"].split()[0], "Supplier", r["supplier_id"])       # "Lumen Cells" -> "Lumen"
        for r in kg.tables["models"]:
            add(r["model"], "Model", r["model"])
        for r in kg.tables["dealers"]:
            add(r["dealer_id"], "Dealer", r["dealer_id"])
            add(r["name"], "Dealer", r["dealer_id"])
            add(r["city"], "Dealer", r["dealer_id"])                      # "Amarillo, TX"
        parts = kg.tables["parts"]
        suffix_owner: dict[str, set] = {}
        for r in parts:
            add(r["part_id"], "Part", r["part_id"])
            for name in (r["name"], r["name"] + "s"):
                add(name, "Part", r["part_id"])
            words = r["name"].split()
            for k in range(1, len(words)):
                suffix_owner.setdefault(" ".join(words[k:]), set()).add(r["part_id"])
        for suf, owners in suffix_owner.items():   # "battery pack" is unique; "seat set" is not
            if len(owners) == 1 and (" " in suf or len(suf) >= 6):
                for s in (suf, suf + "s"):
                    add(s, "Part", next(iter(owners)))
        by_cat: dict[str, set] = {}
        for r in parts:
            by_cat.setdefault(r["category"], set()).add(r["part_id"])
        for word, cat in CATEGORY_WORDS.items():
            if word not in table:
                ids = by_cat.get(cat, set())
                if word in ("harness", "harnesses", "wiring"):
                    ids = {p for p in ids if p.startswith("HARN")}
                if word in ("chip", "chips"):
                    ids = {p for p in ids if p.startswith("CHIP")}
                if word in ("battery", "batteries"):
                    ids = {p for p in ids if p.startswith("BATT")}
                for p in ids:
                    add(word, "Part", p)
        self.table = table
        alts = sorted(table, key=len, reverse=True)
        self.rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(a) for a in alts) + r")(?![\w-])", re.I)
        self.id_rx = re.compile(r"\b(SH-\d{4}|LOT-\d{4})\b", re.I)

    def find(self, text: str) -> list[Mention]:
        out = []
        for m in self.id_rx.finditer(text):
            key = m.group(1).upper()
            cls = "Shipment" if key.startswith("SH") else "Lot"
            out.append(Mention(cls, frozenset({key}), m.group(0), m.start()))
        for m in self.rx.finditer(text):
            ents = self.table[m.group(1).lower()]
            for cls in sorted({c for c, _ in ents}):
                out.append(Mention(cls, frozenset(k for c, k in ents if c == cls), m.group(0), m.start()))
        return sorted(out, key=lambda x: x.start)

    def resolve_surface(self, surface: str | None, cls: str) -> set:
        """Resolve a short surface string (e.g. from the LLM) to ids of one class."""
        if not surface:
            return set()
        s = surface.strip()
        exact = self.table.get(s.lower(), set())
        ids = {k for c, k in exact if c == cls}
        if ids:
            return ids
        upper = s.upper().replace(" ", "")
        if cls in ("Shipment", "Lot") and self.kg.has(nid(cls, upper)):
            return {upper}
        return {k for m in self.find(s) if m.cls == cls for k in m.ids}


# --------------------------------------------------------------------------- reading
KIND_CUES = {
    "plant_outage": [r"\boutage\b", r"\bdark\b", r"power failure", r"production is stopped", r"\bstopped\b",
                     r"shut ?down", r"lost production", r"can(?:no|')t build", r"\boffline\b", r"\brestarts?\b",
                     r"\bflood", r"\bfire\b", r"\bstrike\b", r"\bdown\b", r"\bhalted\b", r"\bidle\b",
                     r"not running", r"\bunavailable\b"],
    "supplier_delay": [r"\bdelay", r"\blate\b", r"can'?t make it", r"cannot make it", r"waiting on", r"new eta",
                       r"will now arrive", r"\bcarrier\b", r"\bshipment\b", r"original drop", r"\bslips?\b",
                       r"\bpushed\b", r"\barrive"],
    "quality_hold": [r"\bquality\b", r"\bdefect", r"\bfaulty\b", r"contaminat", r"\bsuspect\b", r"\brecall",
                     r"\bon hold\b", r"\bbulletin\b", r"\bweld\b", r"\bcrack"],
}
UNCONFIRMED_CUES = [r"\brumou?rs?\b", r"\bunconfirmed\b", r"not confirmed", r"nothing confirmed", r"\bunverified\b",
                    r"\bmay be\b", r"\bmight\b", r"\bpossibl[ey]\b", r"\bsomeone\b", r"\bheard\b", r"no details",
                    r"no dates", r"\bspeculat", r"\bhearsay\b", r"\bmaybe\b", r"sometime soon", r"\bsomething\b"]
# generic words that make a message *about operations* without naming anything specific
GENERIC_OPS = re.compile(r"\b(plants?|factory|factories|assembly|the line|our line|shipments?|lots?|suppliers?|"
                         r"production|builds?)\b", re.I)
SEGMENT_RX = re.compile(r"\(\s*\d+\s*\)|^\s*\d+[.)]\s+", re.M)
CLAUSE_RX = re.compile(r"(?<=[.!?;])\s+|,\s+and\s+(?:also\s+)?|\s+and\s+also\s+", re.I)


def _cue_kind(text: str):
    low = text.lower()
    score = {k: sum(bool(re.search(c, low)) for c in pats) for k, pats in KIND_CUES.items()}
    best = max(score, key=score.get)
    return best if score[best] else None


def _split_by_kind(seg: str) -> list[str]:
    """Unnumbered compound ("X is offline, and the Y shipment is delayed"): split into clause groups,
    starting a new group whenever a clause carries a different incident kind. Clauses without cues
    (dates, details) stay with the group before them."""
    clauses = [c for c in CLAUSE_RX.split(seg) if c and c.strip()]
    groups: list[list] = []
    for c in clauses:
        k = _cue_kind(c)
        if groups and (k is None or groups[-1][0] in (None, k)):
            groups[-1][1].append(c)
            groups[-1][0] = groups[-1][0] or k
        else:
            groups.append([k, [c]])
    kinds = {g[0] for g in groups if g[0]}
    return [" ".join(g[1]).strip() for g in groups] if len(kinds) >= 2 else [seg]


def split_segments(text: str) -> list[str]:
    marks = list(SEGMENT_RX.finditer(text))
    if len(marks) < 2:
        segs = [text]
    else:
        segs = [text[a.end():b.start()] for a, b in zip(marks, marks[1:])] + [text[marks[-1].end():]]
    return [p for s in segs if s.strip() for p in _split_by_kind(s.strip())]


@dataclass
class Reading:
    """One reader's structured view of one incident segment (before grounding)."""
    source: str
    segment: int
    text: str
    kind: str                       # supplier_delay | plant_outage | quality_hold | noise | unconfirmed
    plants: set = field(default_factory=set)
    parts: set = field(default_factory=set)
    suppliers: set = field(default_factory=set)
    shipments: set = field(default_factory=set)
    lots: set = field(default_factory=set)
    dates: dict = field(default_factory=dict)      # role -> DateMention
    vague_parts: list = field(default_factory=list)
    cues: dict = field(default_factory=dict)
    unconfirmed: list = field(default_factory=list)
    rationale: str = ""

    def to_json(self):
        return {"source": self.source, "segment": self.segment, "text": self.text, "kind": self.kind,
                "plants": sorted(self.plants), "parts": sorted(self.parts), "suppliers": sorted(self.suppliers),
                "shipments": sorted(self.shipments), "lots": sorted(self.lots),
                "dates": {k: v.to_json() for k, v in self.dates.items()}, "vague_parts": self.vague_parts,
                "kind_cues": self.cues, "unconfirmed_cues": self.unconfirmed, "rationale": self.rationale}


def assign_date_roles(kind: str, mentions: list[DateMention], text: str) -> dict:
    if not mentions:
        return {}
    if kind == "supplier_delay":
        if len(mentions) >= 2:
            return {"original_eta": mentions[0], "new_eta": mentions[-1]}
        m = mentions[0]
        tail = text[max(0, m.start - 30):m.start].lower()
        new_cue = r"new eta|now|arrive|until|(?:pushed|moved|slips?|slipped|delayed|rescheduled)(?: back| out)? (?:to|until)"
        return {"new_eta": m} if re.search(new_cue, tail) else {"original_eta": m}
    if kind == "plant_outage" and len(mentions) == 1:
        m = mentions[0]
        head = text[max(0, m.start - 25):m.start].lower()
        if re.search(r"\b(through|thru|until|till|to)\s+(?:next\s+|this\s+|last\s+)?$", head + " "):
            # "down through Wednesday": open start means it is already happening -> today
            return {"start": DateMention("today (implied: no start given)", m.start - 1, {"explicit": START}), "end": m}
    if kind in ("plant_outage", "quality_hold"):
        return {"start": mentions[0], "end": mentions[-1] if len(mentions) > 1 else mentions[0]}
    return {}


class RuleReader:
    """Deterministic lexical reader. Transparent, free, and used as the second opinion to the LLM."""
    name = "rules"

    def __init__(self, resolver: Resolver):
        self.resolver = resolver

    def read(self, text: str) -> list[Reading]:
        return [self.read_segment(i, seg) for i, seg in enumerate(split_segments(text))]

    def read_segment(self, i: int, seg: str) -> Reading:
        low = seg.lower()
        hits = lambda pats: [m.group(0) for m in (re.search(c, low) for c in pats) if m]
        cues = {k: hits(pats) for k, pats in KIND_CUES.items()}
        unconf = hits(UNCONFIRMED_CUES)
        r = Reading(self.name, i, seg, "noise", cues={k: v for k, v in cues.items() if v}, unconfirmed=unconf)
        for m in self.resolver.find(seg):
            if m.cls == "Part" and m.ambiguous:
                r.vague_parts.append({"surface": m.surface, "could_be": sorted(m.ids)})
                continue
            bucket = {"Plant": r.plants, "Part": r.parts, "Supplier": r.suppliers,
                      "Shipment": r.shipments, "Lot": r.lots}.get(m.cls)
            if bucket is not None:
                bucket.update(m.ids)
        has_ops = bool(r.plants or r.parts or r.suppliers or r.shipments or r.lots)
        generic = sorted({m.group(0).lower() for m in GENERIC_OPS.finditer(seg)})
        vague = bool(r.vague_parts or generic)
        score = {k: len(v) for k, v in cues.items()}
        if r.shipments:
            score["supplier_delay"] += 2
        if r.lots:
            score["quality_hold"] += 2
        best = max(score, key=lambda k: score[k])
        if unconf and (has_ops or vague or score[best]):
            r.kind, r.rationale = "unconfirmed", f"hedged/unconfirmed wording: {unconf}"
        elif not has_ops and not (vague and score[best]):
            r.kind = "noise"
            r.rationale = ("no plant, part, supplier, shipment or lot is mentioned" +
                           (f" (vague words only: {r.vague_parts or generic})" if vague else ""))
        elif not has_ops:
            # about operations ("the plant is down") but names nothing: grounding will fail -> escalate
            r.kind, r.rationale = best, f"cues {cues[best]} but only generic words {generic + [v['surface'] for v in r.vague_parts]}"
        elif score[best] == 0:
            r.kind, r.rationale = "unconfirmed", "operational names present but no incident described"
        else:
            r.kind, r.rationale = best, f"cues {cues[best]}"
        r.dates = assign_date_roles(r.kind, find_dates(seg), seg)
        return r
