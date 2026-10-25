"""Answer composition for the Ozwide Tools fitment assistant.

The default path is deterministic: the prose is assembled from fields retrieval
actually returned. Two rules are enforced here, not merely intended:

1. Every SKU that appears in an answer must exist in the fixture. `assert_skus_known`
   checks it, and `compose` runs that check on every answer it returns.
2. Abstaining means abstaining. When retrieval could not answer, no SKU is offered.

An optional LLM path exists for the written report's discussion, but it is off by
default and it cannot bypass rule 1 - if the model emits an unknown part number the
answer is rejected and the template answer is used instead.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import retrieval

# TODO(Cory): put the real public sales line here once you have it from the site.
# Nothing is invented in this file, so until you fill this in the handoff refers to
# the desk rather than quoting a number that does not exist.
STAFF_HANDOFF = "our sales desk"
STAFF_HANDOFF_DETAIL = "the phone number is on the contact page of the catalogue site"

SKU_RE = re.compile(r"\bDM\d{1,2}-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")

CUSTOMER = "customer"
STAFF = "staff"


class UnknownSkuError(AssertionError):
    """An answer tried to say a part number that is not in the fixture."""


def known_skus(fixture: dict) -> set[str]:
    return {r["sku"] for r in fixture["records"]} | set(fixture.get("aliases", {}))


def skus_in(text: str) -> list[str]:
    seen: list[str] = []
    for m in SKU_RE.finditer(text):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


def assert_skus_known(text: str, fixture: dict) -> None:
    """Fail loudly if any SKU in the text is not in the fixture. Nothing is invented."""
    allowed = known_skus(fixture)
    for sku in skus_in(text):
        if sku not in allowed:
            raise UnknownSkuError(
                f"answer mentions {sku}, which is not in the fixture. "
                f"Known SKUs: {sorted(allowed)}"
            )


@dataclass
class Answer:
    text: str
    mode: str
    status: str
    skus: list[str] = field(default_factory=list)
    route: str = "template"
    fallback_reason: str | None = None
    handoff: bool = False


# --- Shared helpers -------------------------------------------------------------


def _money(value: float | None) -> str:
    return f"${value:,.2f} inc GST" if value is not None else "price on application"


def _price_phrase(rec: dict) -> str:
    price = rec.get("price_inc_gst")
    if price is not None:
        return _money(price)
    if rec.get("rental_cost") is not None:
        return f"Hire rate ${rec['rental_cost']:.2f} )"
    return "price on application"


def _handle(fixture: dict) -> dict[str, dict]:
    return {r["sku"]: r for r in fixture["records"]}


def _resolve(sku: str, fixture: dict) -> str:
    """Follow the alias map, so a misspelt companion resolves to a real SKU."""
    alias = fixture.get("aliases", {}).get(sku)
    return alias["alias_of"] if alias else sku


def _family_label(family: str | None) -> str:
    return {
        "timing": "engine timing",
        "seal": "seal installation",
        "bearing": "wheel bearing",
    }.get(family or "", "tooling")


def _make_label(make: str | None) -> str:
    """VW keeps its capitals; everything else reads better in title case."""
    if not make:
        return "that"
    if make.upper() == "VW":
        return "VW"
    if "/" in make:  # GM/Vauxhall/Opel is one group, not one marque
        return make.split("/")[0]
    return make.title()


def _closing_line(family: str | None) -> str:
    tail = {
        "timing": "check it against the engine code before it leaves",
        "seal": "confirm the seal number against the old seal before it leaves",
        "bearing": "check the measurement against the old bearing before it leaves",
    }.get(family or "", "confirm the fitment before it leaves")
    return f"Bring the vehicle and the tool number to the counter and we'll {tail}."


def _slots_summary(slots: dict) -> str:
    """Readable key=value summary, without the internal plumbing fields."""
    items = {
        k: v
        for k, v in slots.items()
        if v not in (None, False) and not k.endswith(("_reason", "_known", "_source_sku"))
    }
    if not items:
        return "none"
    return ", ".join(f"{k}={v}" for k, v in items.items())


def _ask_for(family: str | None, slots: dict, fixture: dict) -> tuple[str, str]:
    """Return (the ask, the why) for the field this family is missing."""
    if family == "timing":
        disp = slots.get("displacement")
        codes: list[str] = []
        if disp:
            for rec in fixture["records"]:
                for code, detail in rec.get("engine_codes", {}).items():
                    if detail.get("displacement") == disp:
                        codes.append(code)
        ask = "the engine code"
        why = (
            "Timing kits are matched on the engine code, not on the model or year, "
            "because one engine size can carry several different codes."
        )
        if codes:
            why += f" A {disp}L carries {len(codes)} codes in our catalogue: {', '.join(codes)}."
        why += " The code is stamped on the engine or on the compliance plate."
        return ask, why
    if family == "seal":
        return (
            "the seal number, or the depth the seal sits at",
            "Rear main seal tools are matched on the OEM seal number. Where the number is not "
            "readable, the depth the seal is seated separates the two kits we hold, because "
            "that is the only difference between them.",
        )
    if family == "bearing":
        return (
            "the bearing or hub diameter in millimetres",
            "The universal bearing kit is matched on measurement, not on the vehicle. It will "
            "work on any stub assembly with a CV shaft through it, so the make and model do not "
            "narrow it down at all.",
        )
    return (
        "what the job is",
        "Our catalogue covers engine timing tooling, seal installation tooling and wheel bearing "
        "kits. Each is matched on a different field, so I need to know which family you are in.",
    )


# --- Notes attached to a retrieved record ---------------------------------------


def _notes(rec: dict, slots: dict, fixture: dict) -> list[dict]:
    """Customer-relevant and internal notes, tagged so each mode can filter them.

    A note may carry `customer_text` when the customer-safe phrasing differs from
    the internal one. Customer mode never sees `data_quality`, `fitment_detail` or
    `table_row` notes.
    """
    out: list[dict] = []
    handle = _handle(fixture)
    covered: set[str] = set()

    if rec.get("seat_depth_mm"):
        dr = rec["seat_depth_mm"]
        out.append(
            {
                "kind": "measurement",
                "text": f"The seat depth for this tool is {dr['min']}-{dr['max']}mm.",
            }
        )

    if rec.get("adapter_diameters") and rec.get("source") == "listing":
        measured = {
            k: slots.get(k)
            for k in ("diameter_mm", "inside_diameter_mm", "outside_diameter_mm")
            if slots.get(k) is not None
        }
        for field_name, value in measured.items():
            for ad in rec["adapter_diameters"]:
                if abs(ad["mm"] - value) <= retrieval.DIAMETER_TOLERANCE_MM:
                    out.append(
                        {
                            "kind": "measurement",
                            "text": (
                                f"{value}mm corresponds to adapter {ad['adapter']} "
                                f"({ad['side']} diameter {ad['mm']}mm)."
                            ),
                        }
                    )
                    measured_side = {
                        "inside_diameter_mm": "inside",
                        "outside_diameter_mm": "outside",
                    }.get(field_name)
                    if measured_side and measured_side != ad["side"]:
                        out.append(
                            {
                                "kind": "ambiguity",
                                "text": (
                                    f"The customer gave an {measured_side} diameter, but {value}mm appears in "
                                    f"this kit only as an {ad['side']} diameter ({ad['adapter']}). The listing "
                                    "lists both sides without saying which the fitter should measure."
                                ),
                                "customer_text": (
                                    f"You measured an {measured_side} diameter, but {value}mm only appears in "
                                    f"this kit as an {ad['side']} measurement ({ad['adapter']}). The catalogue "
                                    "lists both without saying which one to measure, so please confirm that "
                                    "before ordering."
                                ),
                            }
                        )
        if len(measured) == 1:
            out.append(
                {
                    "kind": "ask_next",
                    "text": "Confirm the other diameter as well before fitting: the catalogue lists both sides with no rule for which one to measure.",
                }
            )

    for con in rec.get("negative_constraints", []):
        out.append(
            {
                "kind": "constraint",
                "text": f"Not for the {con['excludes']} engine - the listing states \"{con['verbatim']}\".",
            }
        )

    # The pre-2017 conditional only applies when we actually know the year is earlier.
    year = slots.get("year")
    if rec["sku"] == "DM11-C1594" and year and year < 2017:
        companion = _resolve("DM11-A1872", fixture)
        covered.add(companion)
        out.append(
            {
                "kind": "companion",
                "customer_text": (
                    f"As a {year} build it falls under the listing's pre-2017 note: if the VVTi "
                    f"sprockets have to come off, the camshaft holding set {companion} is needed too."
                ),
                "text": (
                    f"This vehicle is a {year}, so it falls under the listing's pre-2017 note: if the VVTi "
                    f"sprockets have to come off, the camshaft holding set {companion} is needed as well. "
                    
                ),
            }
        )

    for related in rec.get("related_skus", []):
        resolved = _resolve(related, fixture)
        other = handle.get(resolved)
        if not other or resolved == rec["sku"] or resolved in covered:
            continue
        covered.add(resolved)
        customer_text = f"The listing also names {resolved} as a related tool."
        text = f"Companion tool named in the listing: {resolved}."
        if other.get("family") == "seal" and rec.get("seat_depth_mm"):
            customer_text = (
                f"{resolved} is a near-identical alternative. The only difference the listing gives is "
                f"the depth the seal seats at."
            )
            text = (
                f"{resolved} is the near-identical alternative. The listing says the only difference is "
                f"the depth the seal seats at, but it publishes the depth for {rec['sku']} only "
                f"({rec['seat_depth_mm']['min']}-{rec['seat_depth_mm']['max']}mm) and not for {resolved}."
            )
        elif not other.get("has_listing"):
            text += " We hold no full listing for it in this catalogue extract."
        out.append({"kind": "alternative", "text": text, "customer_text": customer_text})

    for flag in rec.get("quality_flags", []):
        out.append({"kind": "data_quality", "text": flag})

    if rec.get("table_rows"):
        rows = ", ".join(f"{r['make']} / {r['eng']} (row {r['rowid']})" for r in rec["table_rows"])
        out.append({"kind": "table_row", "text": f"Timing table rows: {rows}."})

    if rec.get("engine_codes") and slots.get("engine_code"):
        detail = rec["engine_codes"].get(slots["engine_code"].upper())
        if detail and detail.get("listed_under"):
            out.append(
                {
                    "kind": "fitment_detail",
                    "text": f"The listing carries {slots['engine_code']} under '{detail['listed_under']}'.",
                }
            )
    return out


_CUSTOMER_KINDS = {"measurement", "constraint", "companion", "alternative", "ambiguity"}


def _format_notes(notes: list[dict], mode: str) -> list[str]:
    if mode == CUSTOMER:
        return [n.get("customer_text") or n["text"] for n in notes if n["kind"] in _CUSTOMER_KINDS]
    return [n["text"] for n in notes]


# --- Template answers -----------------------------------------------------------


def _abstain_text(turn: retrieval.Turn, mode: str, pushed: bool = False, basis: retrieval.Turn | None = None) -> str:
    """The refusal. `basis` lets a push-back keep refusing the original question."""
    basis = basis or turn
    family = _family_label(basis.family)
    lines: list[str] = []

    if pushed:
        lines.append("I still can't confirm that one.")

    if basis.reason == "non_fitment_intent":
        asked = ", ".join(basis.non_fitment)
        if pushed:
            lines.append(
                f"Same answer: {asked} is not in either source, so there is nothing to retrieve and nothing to cite."
            )
        else:
            lines.append(f"I can't answer that one from the catalogue. We index product listings and a timing lookup table; {asked} is not in either.")
            if basis.family:
                lines.append(
                    f"I can tell you which {family} tool fits once the vehicle details are in, but not the {asked} figure."
                )
    elif basis.reason == "make_not_in_catalogue":
        lines.append(
            f"I can't confirm {family} tooling for a {_make_label(basis.slots.get('make'))} vehicle. "
            f"That make isn't covered by the listings we hold, and I'm not going to suggest a part number on a guess."
        )
    elif basis.reason == "engine_code_not_in_catalogue":
        code = basis.slots.get("engine_code")
        lines.append(
            f"I don't have a record of engine code {code} in the catalogue, so I can't tell you which kit suits it. "
            "Rather than guess at a fitment, this one should go to someone who can check the source."
        )
    elif basis.reason == "depth_outside_published_range":
        depth = basis.slots.get("depth_mm")
        lines.append(
            f"A seat depth of {depth}mm is outside the range published for the seal tool I hold, so this isn't a clean match."
        )
    elif basis.reason == "no_family":
        lines.append(
            "I can't match that to anything in the catalogue. Our listings cover engine timing tooling, "
            "seal installation tooling and wheel bearing kits."
        )
    else:
        lines.append(
            "I can't confirm a fitment from the catalogue for that. "
            "No record matches closely enough to stand behind."
        )

    if pushed:
        lines.append(
            "Guessing here would be worse than useless: a wrong timing or seal tool is a stripped engine or a leak, "
            "and the price of the phone call is nothing next to that. This one needs a person."
        )
        if mode == STAFF:
            lines.append(
                f"**Internal:** customer pushed after an abstain. Reason unchanged (`{basis.reason}`). "
                "Do not offer a SKU to close the conversation - escalate instead."
            )
        else:
            lines.append(f"Please ring {STAFF_HANDOFF} - {STAFF_HANDOFF_DETAIL}.")
    else:
        lines.append(
            "I'd rather hand this over than invent a part number. "
            f"{STAFF_HANDOFF.capitalize()} can check it against the full catalogue"
            + (f" - {STAFF_HANDOFF_DETAIL}." if mode == CUSTOMER else ".")
        )

    if mode == STAFF:
        lines.append(
            f"**Internal:** abstained. reason=`{basis.reason}`, family=`{basis.family}`, "
            f"slots collected: {_slots_summary(basis.slots)}. "
            "No SKU was returned by retrieval, so none is offered."
        )
    return "\n\n".join(lines)


def _clarify_text(turn: retrieval.Turn, mode: str, fixture: dict) -> str:
    ask, why = _ask_for(turn.family, turn.slots, fixture)
    lines = [f"I can find that, but I need one more detail: {ask}.", why]

    if turn.family == "seal" and turn.hits:
        tool = turn.hits[0].sku
        rec = _handle(fixture).get(tool, {})
        dr = rec.get("seat_depth_mm")
        if dr:
            lines.append(
                f"There are two kits that may fit: **{tool}** and **DM11-B1726**. They are almost identical. "
                f"The published seat depth for {tool} is {dr['min']}-{dr['max']}mm, and that is what separates them."
            )
        lines.append(
            "Give me the seal number and I'll confirm which one, or the depth and I'll check that instead."
        )
    else:
        lines.append(f"Send that through and I'll match it against the catalogue.")

    if mode == STAFF:
        lines.append(
            f"**Internal:** family=`{turn.family}`, fields_missing=`{turn.missing}`, "
            f"slots so far: {_slots_summary(turn.slots)}. "
            "Retrieval is deliberately held until the key field arrives."
        )
    return "\n\n".join(lines)


def _answered_text(turn: retrieval.Turn, mode: str, fixture: dict) -> str:
    handle = _handle(fixture)
    primary = next((h for h in turn.hits if not h.is_candidate), turn.hits[0])
    rec = handle.get(primary.sku, {})
    notes = _notes(rec, turn.slots, fixture)

    if mode == CUSTOMER:
        lines = [f"**{rec['sku']}** - {rec['title']}", _price_phrase(rec)]
        reason = _customer_reason(turn, primary, rec)
        if reason:
            lines.append(reason)
        for note in _format_notes(notes, mode):
            lines.append(f"- {note}")
        lines.append(_closing_line(turn.family))
        return "\n\n".join(lines)

    lines = [
        f"**{rec['sku']}** - {rec['title']}",
        f"{_price_phrase(rec)} | match type=`{primary.match_type}` | "
        + ", ".join(f"{k}={v}" for k, v in primary.matched_fields.items() if v is not None),
    ]
    if len(turn.hits) > 1:
        others = [h.sku for h in turn.hits[1:]]
        lines.append(f"Other records returned: {', '.join(others)}.")
    for note in _format_notes(notes, mode):
        lines.append(f"- {note}")
    lines.append(f"**Ask the customer next:** {_next_question(turn, rec)}")
    return "\n\n".join(lines)


def _customer_reason(turn: retrieval.Turn, hit: retrieval.Hit, rec: dict) -> str:
    slots = turn.slots
    if hit.match_type == "table_authoritative":
        return (
            f"This comes from our timing lookup table rather than the website, matched on "
            f"{hit.matched_fields.get('make')} with engine code {hit.matched_fields.get('engine_code')}. "
            "The table is the authority for timing fits."
        )
    if hit.match_type == "listing_engine_code":
        return (
            f"Matched on the engine code {hit.matched_fields.get('engine_code')}, which this listing's "
            f"fitment list carries{' under ' + str(hit.matched_fields.get('displacement')) + 'L' if hit.matched_fields.get('displacement') else ''}."
        )
    if hit.match_type == "listing_seal_number":
        return (
            f"Matched on the OEM seal number {hit.matched_fields.get('seal_number_as_published')}, "
            f"which this tool is published against."
        )
    if hit.match_type == "listing_seat_depth":
        return (
            f"Matched on the installed depth you gave me ({hit.matched_fields.get('depth_mm')}mm), "
            f"which falls inside the {hit.matched_fields.get('published_range')} published for this tool."
        )
    if hit.match_type == "measurement":
        return (
            "Matched on the measurement, not on the vehicle. This kit is a universal set, so the size "
            "you measured is what selects the adapter."
        )
    return ""


def _next_question(turn: retrieval.Turn, rec: dict) -> str:
    if rec.get("table_rows"):
        return (
            "Confirm the engine code from the compliance plate - the table is keyed on make plus code "
            "only, with no model, year or displacement, so a code mismatch is the main failure mode."
        )
    if rec.get("seat_depth_mm"):
        return "Ask for the seal number off the old seal. If it is unreadable, ask for the installed depth measured with a straight edge."
    if rec.get("adapter_diameters"):
        return "Ask which side they measured and get the other one too - the listing gives inside and outside diameters with no rule for which to use."
    if turn.family == "timing":
        return "Confirm the engine code, and check the year against the pre-2017 note on this listing."
    return "Confirm the vehicle details and the field this family is keyed on."


# --- Optional LLM path ----------------------------------------------------------

_PROMPT_TEMPLATE = """You are writing the customer-facing reply for a trade tool supplier.

Answer ONLY from the catalogue records below. Do not add part numbers, prices or
fitment claims that are not present in them. If a field is missing from the records,
say so rather than filling it in. Keep it to three short sentences, plain English,
Australian trade tone, no marketing language.

Retrieved records (JSON):
{records}

Customer question: {question}

Reply:"""


def _call_deepseek(prompt: str, key: str) -> str:
    """Network call. Only reached when FOAI_LLM_MODE=deepseek and a key is set."""
    import urllib.request

    body = json.dumps(
        {
            "model": os.environ.get("FOAI_LLM_MODEL", "deepseek-chat"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 300,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"].strip()


def llm_enabled() -> bool:
    return os.environ.get("FOAI_LLM_MODE", "off").strip().lower() == "deepseek"


def llm_compose(turn: retrieval.Turn, mode: str, fixture: dict, transport=None) -> Answer | None:
    """Generate prose from the retrieved records only. None means 'use the template'.

    The generated text is put through the same SKU assertion as the template path,
    so a hallucinated part number cannot reach the user even here.
    """
    if not llm_enabled() or mode != CUSTOMER or turn.status != "answered":
        return None
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None

    handle = _handle(fixture)
    payload = [
        {
            key_name: handle[h.sku].get(key_name)
            for key_name in ("sku", "title", "price_inc_gst", "seat_depth_mm", "engine_codes", "table_rows", "negative_constraints", "model_lines")
            if h.sku in handle
        }
        for h in turn.hits
    ]
    prompt = _PROMPT_TEMPLATE.format(records=json.dumps(payload, indent=2), question=turn.question)
    call = transport or _call_deepseek
    try:
        text = call(prompt, key)
    except Exception as exc:  # network down at demo time is not a crash
        return Answer(text="", mode=mode, status=turn.status, route="template", fallback_reason=f"llm call failed: {exc}")

    try:
        assert_skus_known(text, fixture)
    except UnknownSkuError as exc:
        return Answer(text="", mode=mode, status=turn.status, route="template", fallback_reason=str(exc))

    return Answer(text=text, mode=mode, status=turn.status, skus=skus_in(text), route="llm")


# --- Entry point ----------------------------------------------------------------


def compose(
    turn: retrieval.Turn,
    mode: str = CUSTOMER,
    fixture: dict | None = None,
    pushed: bool = False,
    prior_turn: retrieval.Turn | None = None,
) -> Answer:
    """Turn a retrieval result into the reply the user sees. Never invents a SKU.

    `pushed` is set when the customer is pressing after a refusal. A push-back can
    only ever produce an answer if it actually supplied the missing key field; if it
    did not, the refusal holds at the original reason. Pressure must not re-open a
    question retrieval could not answer.
    """
    fixture = fixture or retrieval.load_fixture()

    escalating = bool(pushed and prior_turn and prior_turn.status == "abstain" and turn.status != "answered")

    if escalating:
        text = _abstain_text(prior_turn, mode, pushed=True)
        status = "abstain"
    elif turn.status == "abstain":
        text = _abstain_text(turn, mode, pushed=pushed)
        status = "abstain"
    elif turn.status == "clarify":
        text = _clarify_text(turn, mode, fixture)
        status = "clarify"
    else:
        text = _answered_text(turn, mode, fixture)
        status = "answered"

    # Belt and braces: this runs on every answer, template or LLM.
    assert_skus_known(text, fixture)
    return Answer(
        text=text,
        mode=mode,
        status=status,
        skus=skus_in(text),
        route="template",
        handoff=status == "abstain",
    )
