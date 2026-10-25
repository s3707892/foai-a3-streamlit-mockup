"""Deterministic retrieval for the Ozwide Tools fitment assistant.

Pure functions, no network, no model, no Streamlit. The same question always
produces the same answer, which is what lets the demo run offline on a lab
machine with no API key.

The two jobs the design commits to:

1. Work out which product *family* the question is about, because the match key
   differs per family (engine code / seal number / measurement).
2. Collect that family's key before answering, and refuse to answer without it.

Nothing in this module invents a SKU. The only SKUs it can return are the ones
loaded from data/listings.json.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "listings.json"

FAMILIES = ("timing", "seal", "bearing")

# --- Vocabulary -----------------------------------------------------------------

# Words that name the product family. Primary terms weigh more because they are
# unambiguous; secondary terms appear in more than one family's listing text.
_PRIMARY_TERMS = {
    "timing": ("timing", "cam belt", "cambelt", "camshaft", "timing chain"),
    "seal": ("seal", "rear main", "crank seal", "oil seal"),
    "bearing": ("bearing", "wheel hub"),
}
_SECONDARY_TERMS = {
    "timing": ("belt", "sprocket", "tdc", "locking kit", "timing kit"),
    "seal": ("installer", "installation tool", "crankshaft"),
    "bearing": ("extractor", "stub", "knuckle", "hub"),
}

# Questions retrieval cannot answer no matter how well it indexes. These go to a
# human. Torque figures are the example the design document commits to.
_NON_FITMENT_TERMS = (
    "torque",
    "how tight",
    "tightening",
    "newton",
    "warranty",
    "invoice",
    "receipt",
    "freight",
    "delivery",
    "stock level",
    "in stock",
    "do you have any",
)

# Surface form -> the make string used by the timing table and the listings.
_MAKE_ALIASES = {
    "vw": "VW",
    "v w": "VW",
    "volkswagen": "VW",
    "audi": "AUDI",
    "seat": "SEAT",
    "skoda": "SKODA",
    "gm": "GM/Vauxhall/Opel",
    "vauxhall": "GM/Vauxhall/Opel",
    "opel": "GM/Vauxhall/Opel",
    "holden": "GM/Vauxhall/Opel",
    "chevrolet": "GM/Vauxhall/Opel",
    "chevy": "GM/Vauxhall/Opel",
    "isuzu": "ISUZU",
    "izuzu": "ISUZU",
    "toyota": "TOYOTA",
    "mazda": "MAZDA",
    "ford": "FORD",
    "bmw": "BMW",
    "mercedes": "MERCEDES-BENZ",
    "mercedes benz": "MERCEDES-BENZ",
    "merc": "MERCEDES-BENZ",
    "volvo": "VOLVO",
    "nissan": "NISSAN",
    "subaru": "SUBARU",
    "hyundai": "HYUNDAI",
    "kia": "KIA",
    "mitsubishi": "MITSUBISHI",
    "honda": "HONDA",
    "jeep": "JEEP",
    "land rover": "LAND ROVER",
    "tesla": "TESLA",
}

# Models named in the fixture, mapped to the make its listing files them under.
_MODEL_MAKES = {
    "golf": "VW", "jetta": "VW", "up": "VW", "polo": "VW", "passat": "VW",
    "tiguan": "VW", "amarok": "VW", "arteon": "VW",
    "mii": "SEAT", "ibiza": "SEAT", "leon": "SEAT", "toledo": "SEAT",
    "citigo": "SKODA", "octavia": "SKODA", "kamiq": "SKODA", "karoq": "SKODA",
    "yeti": "SKODA", "fabia": "SKODA", "superb": "SKODA",
    # The listing files a Colorado under Isuzu. That is kept, and flagged.
    "dmax": "ISUZU", "d-max": "ISUZU", "rodeo": "GM/Vauxhall/Opel",
    "jackaroo": "GM/Vauxhall/Opel", "colorado": "ISUZU",
    "nkr69": "ISUZU", "nkr77": "ISUZU", "nlr85": "ISUZU", "nnr85": "ISUZU",
    "mu": "ISUZU", "wizard": "ISUZU",
    "hilux": "TOYOTA", "ranger": "FORD", "navara": "NISSAN",
}

# All-caps tokens that are not engine codes, so generic extraction ignores them.
_GENERIC_CODE_STOP = {
    "TSI", "TFSI", "TGI", "TDI", "GTI", "GST", "OEM", "ABS", "ECU", "OBD", "VIN",
    "SUV", "UTE", "CV", "IRS", "MM", "KM", "REGO", "NSW", "VIC", "QLD", "WA", "SA",
    "TAS", "ACT", "NT", "AUD", "DIY", "FWD", "AWD", "RWD", "NOW", "ANY", "SAME",
    "HELP", "NEED", "WANT", "LOOK", "HAVE", "GOOD", "MUCH", "WHAT", "WITH", "THIS",
    "THAT", "FROM", "THEY", "YOUR", "JUST", "SURE", "BEEN", "WILL", "FINE", "ONLY",
}

_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
# '1.4L', '1.4 TSI', '1.4 litre' all name a displacement; '12.5mm' is a measurement.
_DISPLACEMENT_RE = re.compile(r"\b(\d\.\d)\s*(?:l\b|litre|liter|tsi\b|tfsi\b|tdi\b|tgi\b)")
_DISPLACEMENT_BARE_RE = re.compile(r"(?<![\d.])([1-9]\.\d)(?![\d.]|\s*mm)")
_MM_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*mm\b")

DEPTH_TOLERANCE_MM = 0.05
DIAMETER_TOLERANCE_MM = 0.15


# --- Fixture --------------------------------------------------------------------


@lru_cache(maxsize=4)
def load_fixture(path: str | None = None) -> dict:
    p = Path(path) if path else DATA_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def records(path: str | None = None) -> list[dict]:
    return load_fixture(path)["records"]


def known_skus(path: str | None = None) -> set[str]:
    return {r["sku"] for r in records(path)}


def _code_universe(fixture: dict) -> set[str]:
    codes: set[str] = set()
    for rec in fixture["records"]:
        codes.update(rec.get("engine_codes", {}).keys())
        for row in rec.get("table_rows", []):
            codes.add(row["eng"])
    return codes


# --- Classification -------------------------------------------------------------


def _hits(text: str, terms: tuple[str, ...]) -> list[str]:
    return [t for t in terms if re.search(r"\b" + re.escape(t) + r"\b", text)]


def classify(question: str) -> str | None:
    """Which product family is this question about? None when it is unclear."""
    text = question.lower()
    scores: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    for family in FAMILIES:
        primary = _hits(text, _PRIMARY_TERMS[family])
        secondary = _hits(text, _SECONDARY_TERMS[family])
        scores[family] = 3 * len(primary) + len(secondary)
        evidence[family] = primary + secondary
    best = max(scores, key=lambda f: scores[f])
    if scores[best] == 0:
        return None
    # A tie means the question mentions two families equally - do not guess.
    if list(scores.values()).count(scores[best]) > 1:
        return None
    return best


def classification_evidence(question: str) -> dict:
    text = question.lower()
    out = {}
    for family in FAMILIES:
        out[family] = _hits(text, _PRIMARY_TERMS[family]) + _hits(text, _SECONDARY_TERMS[family])
    return out


def non_fitment_terms(question: str) -> list[str]:
    return _hits(question.lower(), _NON_FITMENT_TERMS)


# --- Slot extraction ------------------------------------------------------------


def _find_make(text: str) -> tuple[str | None, str | None]:
    """Return (canonical make, how it was found)."""
    lowered = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "
    for surface in sorted(_MAKE_ALIASES, key=len, reverse=True):
        if " " + surface + " " in lowered:
            return _MAKE_ALIASES[surface], f"make named: '{surface}'"
    for model in sorted(_MODEL_MAKES, key=len, reverse=True):
        if " " + model + " " in lowered:
            return _MODEL_MAKES[model], f"model named: '{model}'"
    return None, None


def _find_engine_code(text: str, fixture: dict) -> tuple[str | None, bool]:
    """Known catalogue code wins. Otherwise take a code only when it is unambiguous.

    A loose "four capital letters" rule turns GOLF or WAGON into an engine code, so
    a generic candidate is accepted only when the message is essentially just the
    code, or when it is explicitly labelled as one. A false positive here costs an
    unnecessary abstain, never a wrong part number.
    """
    universe = _code_universe(fixture)
    for token in re.findall(r"\b[A-Za-z0-9]{2,8}\b", text):
        if token.upper() in universe:
            return token.upper(), True

    labelled = re.search(r"\bcode\s*(?:is|:)?\s*([A-Za-z0-9]{3,6})\b", text, re.IGNORECASE)
    if labelled:
        cand = labelled.group(1).upper()
        if cand not in _GENERIC_CODE_STOP:
            return cand, False

    filler = re.sub(r"[^A-Za-z0-9 ]", " ", text).strip().lower()
    filler = re.sub(
        r"\b(it|its|is|the|engine|code|number|no|for|my|a|about|think|maybe|sure|um|yes|ok|okay|hi|hello)\b",
        " ",
        filler,
    )
    bare = filler.split()
    if len(bare) == 1 and re.fullmatch(r"[A-Za-z0-9]{3,6}", bare[0]):
        cand = bare[0].upper()
        if cand not in _GENERIC_CODE_STOP and re.search(r"[A-Z]{3}", cand):
            return cand, False
    return None, False


def _normalise_seal(value: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", value.upper())


def _seal_regex(normalised: str) -> re.Pattern:
    """Match a seal number however the customer punctuates it."""
    return re.compile(r"[\s\-./]?".join(re.escape(c) for c in normalised), re.IGNORECASE)


def _find_seal_number(text: str, fixture: dict) -> tuple[str | None, dict | None]:
    """Return (matched normalised number, source record) for a published seal number."""
    for rec in fixture["records"]:
        for seal in rec.get("seal_numbers", []):
            norm = seal["normalised"]
            if len(norm) < 5:
                continue
            if _seal_regex(norm).search(text):
                return norm, rec
    return None, None


def extract_slots(text: str, family: str | None = None, fixture: dict | None = None) -> dict:
    """Pull vehicle and measurement fields out of free text.

    Millimetre values are ambiguous between a seal seat depth and a bearing
    diameter, so the family decides what a bare '12.5mm' means.
    """
    fixture = fixture or load_fixture()
    make, make_reason = _find_make(text)
    engine, engine_known = _find_engine_code(text, fixture)
    year = _YEAR_RE.search(text)
    disp = _DISPLACEMENT_RE.search(text.lower()) or _DISPLACEMENT_BARE_RE.search(text)
    seal_norm, seal_rec = _find_seal_number(text, fixture)

    slots: dict = {
        "make": make,
        "make_reason": make_reason,
        "engine_code": engine,
        "engine_code_known": engine_known,
        "year": int(year.group(1)) if year else None,
        "displacement": disp.group(1) if disp else None,
        "seal_number": seal_norm,
        "seal_number_source_sku": seal_rec["sku"] if seal_rec else None,
        "depth_mm": None,
        "inside_diameter_mm": None,
        "outside_diameter_mm": None,
        "diameter_mm": None,
    }

    for mm in _MM_RE.finditer(text.lower()):
        value = float(mm.group(1))
        window = text.lower()[max(0, mm.start() - 40) : mm.end()]
        if family == "seal":
            slots["depth_mm"] = value
        elif family == "bearing":
            if re.search(r"\binside\b|\binner\b|\bid\b", window):
                slots["inside_diameter_mm"] = value
            elif re.search(r"\boutside\b|\bouter\b|\bod\b", window):
                slots["outside_diameter_mm"] = value
            else:
                slots["diameter_mm"] = value
    return slots


def missing_fields(family: str | None, slots: dict) -> list[str]:
    """Which fields the assistant must still collect before it can answer."""
    if family == "timing":
        if not slots.get("engine_code"):
            return ["engine_code"]
        return []
    if family == "seal":
        if slots.get("seal_number") or slots.get("depth_mm") is not None:
            return []
        return ["seal_number_or_depth"]
    if family == "bearing":
        if slots.get("diameter_mm") is not None:
            return []
        if slots.get("inside_diameter_mm") is None and slots.get("outside_diameter_mm") is None:
            return ["inside_diameter_mm", "outside_diameter_mm"]
        return []
    return ["problem_description"]


# --- Retrieval ------------------------------------------------------------------


@dataclass
class Hit:
    """One catalogue record that came back, and why."""

    sku: str
    title: str
    family: str
    source: str
    match_type: str
    matched_fields: dict = field(default_factory=dict)
    is_candidate: bool = False
    note: str = ""

    def as_row(self) -> dict:
        return {
            "SKU": self.sku,
            "Record": self.title,
            "Source": self.source,
            "Match type": self.match_type,
            "Matched on": ", ".join(f"{k}={v}" for k, v in self.matched_fields.items() if v is not None) or "-",
            "Status": "candidate" if self.is_candidate else "match",
        }


def _record_index(fixture: dict) -> dict[str, dict]:
    return {r["sku"]: r for r in fixture["records"]}


def normalise_make(surface: str) -> str:
    key = re.sub(r"[^a-z0-9 ]", " ", surface.lower()).strip()
    key = re.sub(r"\s+", " ", key)
    return _MAKE_ALIASES.get(key, surface.upper())


def family_makes(family: str, fixture: dict) -> set[str]:
    """Every make this family holds a record for, normalised."""
    out: set[str] = set()
    for rec in fixture["records"]:
        if rec.get("family") != family:
            continue
        for surface in rec.get("makes", []):
            out.add(normalise_make(surface))
        for row in rec.get("table_rows", []):
            for part in row["make"].split("/"):
                out.add(normalise_make(part))
    return out


def make_is_covered(family: str, make: str | None, fixture: dict) -> bool:
    """Does this family hold anything at all for this make?

    Only timing and seal have make-level coverage worth checking. The bearing
    kit is matched on measurement, so the make tells us nothing either way.
    """
    if not make or family not in ("timing", "seal"):
        return True
    return normalise_make(make) in family_makes(family, fixture)


def _make_matches(csv_make: str, wanted: str) -> bool:
    parts = {p.strip().upper() for p in csv_make.split("/")}
    return wanted.upper() in parts or wanted.upper() == csv_make.upper()


def _hit_from_record(rec: dict, match_type: str, matched_fields: dict, **kw) -> Hit:
    return Hit(
        sku=rec["sku"],
        title=rec["title"],
        family=rec["family"],
        source=rec["source"],
        match_type=match_type,
        matched_fields=matched_fields,
        **kw,
    )


def lookup(family: str | None, slots: dict, fixture: dict | None = None) -> list[Hit]:
    """Retrieve catalogue records for these slots. Empty list means no match."""
    fixture = fixture or load_fixture()
    index = _record_index(fixture)
    if family is None:
        return []
    if family == "timing":
        return _lookup_timing(slots, fixture, index)
    if family == "seal":
        return _lookup_seal(slots, fixture, index)
    if family == "bearing":
        return _lookup_bearing(slots, fixture, index)
    return []


def _lookup_timing(slots: dict, fixture: dict, index: dict) -> list[Hit]:
    make = slots.get("make")
    code = slots.get("engine_code")
    if not code:
        return []

    # 1. The timing table outranks every listing wherever it has a (make, engine) row.
    if make:
        table_hits: list[Hit] = []
        for rec in fixture["records"]:
            for row in rec.get("table_rows", []):
                if row["eng"].upper() == code.upper() and _make_matches(row["make"], make):
                    table_hits.append(
                        _hit_from_record(
                            rec,
                            "table_authoritative",
                            {"make": row["make"], "engine_code": row["eng"], "rowid": row["rowid"]},
                            note="Timing lookup table row. The table is keyed on make + engine code only.",
                        )
                    )
                    break
        if table_hits:
            return table_hits

    # 2. Otherwise the website listing's engine list, but only for a code we hold.
    if not slots.get("engine_code_known"):
        return []
    out = []
    for rec in fixture["records"]:
        detail = rec.get("engine_codes", {}).get(code.upper())
        if detail:
            out.append(
                _hit_from_record(
                    rec,
                    "listing_engine_code",
                    {
                        "engine_code": code.upper(),
                        "displacement": detail.get("displacement"),
                        "listed_under": detail.get("listed_under"),
                    },
                )
            )
    return out


def _lookup_seal(slots: dict, fixture: dict, index: dict) -> list[Hit]:
    seal = slots.get("seal_number")
    depth = slots.get("depth_mm")

    # 1. Seal number is the definitive key.
    if seal:
        out = []
        for rec in fixture["records"]:
            for published in rec.get("seal_numbers", []):
                if published["normalised"] == seal:
                    out.append(
                        _hit_from_record(
                            rec,
                            "listing_seal_number",
                            {"seal_number_as_published": published["as_published"]},
                        )
                    )
                    break
        if out:
            return out

    # 2. Installed depth separates the two near-identical kits.
    if depth is not None:
        for rec in fixture["records"]:
            dr = rec.get("seat_depth_mm")
            if dr and dr["min"] - DEPTH_TOLERANCE_MM <= depth <= dr["max"] + DEPTH_TOLERANCE_MM:
                return [
                    _hit_from_record(
                        rec,
                        "listing_seat_depth",
                        {"depth_mm": depth, "published_range": f"{dr['min']}-{dr['max']}mm"},
                    )
                ]
        return []

    # 3. Engine code alone is not enough: two kits fit, so return candidates only.
    code = slots.get("engine_code")
    if code and slots.get("engine_code_known"):
        cands = []
        for rec in fixture["records"]:
            if code.upper() in rec.get("engine_codes", {}):
                cands.append(
                    _hit_from_record(
                        rec,
                        "listing_engine_code_candidate",
                        {"engine_code": code.upper()},
                        is_candidate=True,
                        note="This kit lists that engine, but two kits fit it. The seal number or installed depth decides which.",
                    )
                )
                for related in rec.get("related_skus", []):
                    alt = index.get(related)
                    if alt and alt["family"] == "seal":
                        cands.append(
                            _hit_from_record(
                                alt,
                                "cross_reference_candidate",
                                {"referred_to_by": rec["sku"]},
                                is_candidate=True,
                                note="Named by the other listing as the near-identical alternative. No listing held in this fixture.",
                            )
                        )
        return cands
    return []


def _lookup_bearing(slots: dict, fixture: dict, index: dict) -> list[Hit]:
    measured = {
        "diameter_mm": slots.get("diameter_mm"),
        "inside_diameter_mm": slots.get("inside_diameter_mm"),
        "outside_diameter_mm": slots.get("outside_diameter_mm"),
    }
    measured = {k: v for k, v in measured.items() if v is not None}
    if not measured:
        return []

    out = []
    for rec in fixture["records"]:
        adapters = rec.get("adapter_diameters", [])
        if not adapters:
            continue
        matched = []
        for field_name, value in measured.items():
            for ad in adapters:
                if abs(ad["mm"] - value) <= DIAMETER_TOLERANCE_MM:
                    matched.append(
                        {
                            "field": field_name,
                            "adapter": ad["adapter"],
                            "side": ad["side"],
                            "mm": ad["mm"],
                        }
                    )
        if matched:
            fields = {m["field"]: m["mm"] for m in matched}
            fields["adapter"] = ", ".join(f"{m['adapter']} ({m['side']})" for m in matched)
            out.append(_hit_from_record(rec, "measurement", fields))
    return out


# --- Whole-turn resolution ------------------------------------------------------


@dataclass
class Turn:
    """Everything one exchange produced, for the answerer and the evidence panel."""

    question: str
    family: str | None
    evidence: dict
    slots: dict
    missing: list[str]
    hits: list[Hit]
    status: str  # answered | clarify | abstain
    reason: str | None = None
    confidence_note: str = ""
    non_fitment: list[str] = field(default_factory=list)

    @property
    def top_sku(self) -> str | None:
        for hit in self.hits:
            if not hit.is_candidate:
                return hit.sku
        return None


def _confidence_note(family: str | None, slots: dict, hits: list[Hit], fixture: dict) -> str:
    if not hits:
        return "No catalogue record satisfied the match key."
    best = hits[0]
    if best.match_type == "table_authoritative":
        return "Authoritative: the timing table has a row for this make and engine code, so it outranks the website listings."
    if best.match_type == "listing_engine_code":
        detail = best.matched_fields
        bits = [f"engine code {detail.get('engine_code')} appears in the listing"]
        if detail.get("displacement"):
            bits.append(f"listed under {detail.get('displacement')}L")
        return "Website listing match: " + ", ".join(bits) + ". The timing table has no row for this make and code."
    if best.match_type == "listing_seal_number":
        return "Website listing match on the published OEM seal number. The seal number is the definitive key for this family."
    if best.match_type == "listing_seat_depth":
        return "Website listing match on installed depth: the measured depth falls inside the range published for this tool."
    if best.match_type == "measurement":
        return "Matched on the measured diameter, not on the vehicle. Confirm the measurement before ordering."
    if best.is_candidate:
        return "Candidates only. The key field that separates these tools has not been supplied yet."
    return "Website listing match."


def _clarify_note(family: str | None, slots: dict, fixture: dict) -> str:
    """What retrieval is waiting on, in terms a marker can check."""
    if family == "timing":
        disp = slots.get("displacement")
        codes = [
            code
            for rec in fixture["records"]
            for code, detail in rec.get("engine_codes", {}).items()
            if detail.get("displacement") == disp
        ] if disp else []
        if codes:
            return (
                f"This family is keyed on engine code, which has not been supplied. "
                f"{disp}L covers {len(codes)} codes in the catalogue, so displacement alone cannot select a tool."
            )
        return "This family is keyed on engine code, which has not been supplied yet."
    if family == "seal":
        return "This family is keyed on the OEM seal number, or failing that the installed seal depth. Neither has been supplied."
    if family == "bearing":
        return "This kit is matched on the measured bearing or hub diameter, not on the vehicle. No measurement has been supplied."
    return "The question does not name a product family this catalogue covers."


def turn(question: str, fixture: dict | None = None, *, force_family: str | None = None, prior_slots: dict | None = None) -> Turn:
    """Classify, extract, retrieve and decide whether to answer, ask or abstain.

    prior_slots carries the fields collected on earlier turns, so a follow-up of
    just 'CXSA' still knows the make and year from two messages ago.
    """
    fixture = fixture or load_fixture()
    family = force_family or classify(question)
    fresh = extract_slots(question, family, fixture)
    slots = dict(prior_slots or {})
    for key, value in fresh.items():
        if value is None:
            continue
        # engine_code_known defaults to False on every extraction; only trust it
        # when this message actually contained a code.
        if key == "engine_code_known" and not fresh.get("engine_code"):
            continue
        slots[key] = value
    evidence = classification_evidence(question)

    # 1. Non-fitment questions are handed off whatever else we found.
    nf = non_fitment_terms(question)
    if nf:
        return Turn(
            question=question,
            family=family,
            evidence=evidence,
            slots=slots,
            missing=[],
            hits=[],
            status="abstain",
            reason="non_fitment_intent",
            confidence_note=f"Question asks about {', '.join(nf)}, which is not in the catalogue. No tool recommendation is possible from these sources.",
            non_fitment=nf,
        )

    # 2. No family, no retrieval. Ask what the job is rather than guess.
    if family is None:
        return Turn(
            question=question,
            family=None,
            evidence=evidence,
            slots=slots,
            missing=["problem_description"],
            hits=[],
            status="abstain",
            reason="no_family",
            confidence_note="The question does not name a product family this catalogue covers.",
        )

    # 3. A make we recognise but hold nothing for: refuse now, do not ask for
    #    an engine code that could never resolve.
    if not make_is_covered(family, slots.get("make"), fixture):
        return Turn(
            question=question,
            family=family,
            evidence=evidence,
            slots=slots,
            missing=[],
            hits=[],
            status="abstain",
            reason="make_not_in_catalogue",
            confidence_note=f"No {family} record in this fixture covers {slots.get('make')}. Catalogue scope, not a fitment judgement.",
        )

    missing = missing_fields(family, slots)
    hits = lookup(family, slots, fixture)

    # 4. A code we do not hold: abstain rather than fall back to the engine size.
    if family == "timing" and slots.get("engine_code") and not slots.get("engine_code_known") and not hits:
        return Turn(
            question=question,
            family=family,
            evidence=evidence,
            slots=slots,
            missing=[],
            hits=hits,
            status="abstain",
            reason="engine_code_not_in_catalogue",
            confidence_note=f"Engine code {slots.get('engine_code')} is not in the timing table or in any listing we hold.",
        )

    # 5. A key field is missing and nothing was retrievable: ask for it.
    if missing and not hits:
        return Turn(
            question=question,
            family=family,
            evidence=evidence,
            slots=slots,
            missing=missing,
            hits=hits,
            status="clarify",
            reason="missing_key_field",
            confidence_note=_clarify_note(family, slots, fixture),
        )

    # 6. Candidates only: ambiguity the customer has to break.
    if hits and all(h.is_candidate for h in hits):
        return Turn(
            question=question,
            family=family,
            evidence=evidence,
            slots=slots,
            missing=missing_fields(family, slots),
            hits=hits,
            status="clarify",
            reason="ambiguous_needs_separator",
            confidence_note=_confidence_note(family, slots, hits, fixture),
        )

    # 7. Something matched.
    if hits:
        return Turn(
            question=question,
            family=family,
            evidence=evidence,
            slots=slots,
            missing=missing,
            hits=hits,
            status="answered",
            confidence_note=_confidence_note(family, slots, hits, fixture),
        )

    # 8. Key fields present, nothing matched: abstain, never guess.
    reason = "no_match"
    if family == "seal" and slots.get("depth_mm") is not None:
        reason = "depth_outside_published_range"
    if family == "timing" and not slots.get("engine_code"):
        reason = "missing_key_field"
    return Turn(
        question=question,
        family=family,
        evidence=evidence,
        slots=slots,
        missing=missing,
        hits=hits,
        status="clarify" if reason == "missing_key_field" else "abstain",
        reason=reason,
        confidence_note=_confidence_note(family, slots, hits, fixture),
    )
