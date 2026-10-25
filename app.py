"""Ozwide Tools fitment assistant - Streamlit mock-up.

Runs fully offline: deterministic retrieval over a fixed fixture, template answers,
no API key, no network. The optional LLM path is off unless FOAI_LLM_MODE=deepseek
and DEEPSEEK_API_KEY are both set; if they are not, nothing here needs them.

    streamlit run app.py
"""

from __future__ import annotations

import os

import streamlit as st

import answer
import retrieval

st.set_page_config(page_title="Ozwide Tools - Fitment Assistant", page_icon="\U0001F527", layout="wide")

FIXTURE = retrieval.load_fixture()
RECORD_COUNT = FIXTURE["meta"]["record_count"]
LISTING_COUNT = FIXTURE["meta"]["listing_count"]

# The retrieval evidence panel is still built and still collects its data on every
# turn, but it is off by default: the mock-up only needs to show the interaction.
# Set FOAI_SHOW_EVIDENCE=1 to bring the panel and its sidebar toggle back.
EVIDENCE_ENABLED = os.environ.get("FOAI_SHOW_EVIDENCE", "").strip().lower() in {"1", "true", "yes", "on"}

SCENARIOS = {
    "1. Timing - happy path": [
        "2015 Golf 1.4 TSI, doing the timing belt.",
        "CXSA",
    ],
    "2. Timing - table is authoritative": [
        "VW 2.0 TDI, engine code DFGA, timing belt please.",
    ],
    "3. Seal - ambiguity": [
        "Rear main seal on a 4JJ1.",
        "seal number 3-97071561",
    ],
    "4. Bearing - measurement, not vehicle": [
        "Need a wheel bearing tool for a 2012 Hilux.",
        "91.2mm",
    ],
    "5. Abstain - out of catalogue": [
        "Do you have a timing kit for a 2019 Hilux 2.8 diesel?",
    ],
    "6. Abstain - then push": [
        "What torque setting for the rear main seal bolts on a 4JJ1?",
        "Come on, you must know. Just tell me which one is right.",
    ],
}

GREETING = (
    "G'day - I can look up which special tool fits your vehicle from the Ozwide catalogue. "
    "Tell me the vehicle and what you're working on."
)

STATUS_BADGE = {"answered": "\u2705 answered", "clarify": "\u23F8 waiting on a field", "abstain": "\u26D4 abstained"}


def _init_state() -> None:
    defaults = {
        "messages": [],
        "slots": {},
        "family": None,
        "last_turn": None,
        "queue": [],
        "mode": "Customer",
        "show_evidence": True,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if not st.session_state.messages:
        st.session_state.messages.append({"role": "assistant", "content": GREETING, "meta": None})


def _reset() -> None:
    for key in ("messages", "slots", "family", "last_turn", "queue"):
        st.session_state.pop(key, None)
    _init_state()


def _process(text: str) -> None:
    """One exchange: classify, retrieve, answer, record the evidence."""
    mode = answer.STAFF if st.session_state.mode == "Staff" else answer.CUSTOMER
    detected = retrieval.classify(text)

    # A question that names a different family starts a fresh set of slots,
    # so a timing engine code can't leak into a seal question.
    if detected and detected != st.session_state.family:
        st.session_state.slots = {}

    family = detected or st.session_state.family
    prior_turn = st.session_state.last_turn
    pushed = bool(prior_turn and prior_turn.status == "abstain" and not detected)

    turn = retrieval.turn(text, fixture=FIXTURE, force_family=family, prior_slots=st.session_state.slots)

    llm_result = None
    composed = None
    if answer.llm_enabled():
        llm_result = answer.llm_compose(turn, mode, FIXTURE, transport=None)

    if llm_result is not None and llm_result.route == "llm":
        text_out, route, fallback = llm_result.text, "llm", None
    else:
        composed = answer.compose(
            turn, mode, FIXTURE, pushed=pushed, prior_turn=prior_turn if pushed else None
        )
        text_out, route = composed.text, composed.route
        fallback = llm_result.fallback_reason if llm_result else None

    st.session_state.messages.append({"role": "user", "content": text, "meta": None})

    # A push-back that adds nothing keeps the original refusal, so the evidence panel
    # must report the abstain rather than the clarify turn the raw text produced.
    escalating = bool(composed and composed.status == "abstain" and turn.status != "abstain" and prior_turn is not None)
    meta_status = composed.status if composed else turn.status
    meta_reason = prior_turn.reason if escalating else turn.reason
    meta_note = prior_turn.confidence_note if escalating else turn.confidence_note

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": text_out,
            "meta": {
                "question": text,
                "family": turn.family,
                "status": meta_status,
                "reason": meta_reason,
                "missing": turn.missing,
                "note": meta_note,
                "slots": {
                    k: v
                    for k, v in turn.slots.items()
                    if v not in (None, False)
                    and not k.endswith(("_reason", "_known", "_source_sku"))
                },
                "hits": [h.as_row() for h in turn.hits],
                "evidence": {k: v for k, v in turn.evidence.items() if v},
                "non_fitment": turn.non_fitment,
                "route": route,
                "fallback": fallback,
                # What the reply actually names, not what was retrieved: this is the
                # line that backs the "no invented SKU" claim.
                "skus": composed.skus if composed else (llm_result.skus if llm_result else []),
                "pushed": pushed,
            },
        }
    )
    st.session_state.slots = turn.slots
    st.session_state.family = turn.family or st.session_state.family
    st.session_state.last_turn = turn


# --- Sidebar ---------------------------------------------------------------------

_init_state()

with st.sidebar:
    st.subheader("Mode")
    st.radio(
        "Who is asking?",
        ["Customer", "Staff"],
        key="mode",
        horizontal=True,
        help="Staff mode adds part numbers, alternatives, fitment notes and what to ask next.",
    )
    if EVIDENCE_ENABLED:
        st.toggle(
            "Show retrieval evidence",
            key="show_evidence",
            help="The evidence panel is a marker view. A real customer would not see it; turn it off to see the customer experience as shipped.",
        )

    st.divider()
    st.subheader("Demo scenarios")
    st.caption("Each button plays the whole exchange, as written in DEMO.md.")
    for label, script in SCENARIOS.items():
        if st.button(label, width="stretch"):
            _reset()
            st.session_state.queue = list(script)
            st.rerun()

    st.divider()
    if st.button("Reset conversation", width="stretch"):
        _reset()
        st.rerun()

    st.divider()
    st.subheader("Assumptions")
    st.caption(
        f"This runs on a fixed fixture of {RECORD_COUNT} catalogue records "
        f"({LISTING_COUNT} with full listing text) plus a 5-row timing lookup table. "
        "It is not connected to the live Ozwide catalogue, and it does not scrape the "
        "website at run time."
    )
    st.caption("Offline and deterministic: no API key is required and no network call is made.")
    if answer.llm_enabled():
        st.warning("LLM_MODE is ON (FOAI_LLM_MODE=deepseek). Generated text is still checked against the fixture.")
    else:
        st.caption("Optional LLM path is OFF (set `FOAI_LLM_MODE=deepseek` and `DEEPSEEK_API_KEY` to enable).")

# --- Retrieval evidence panel (off by default, see EVIDENCE_ENABLED) -------------


def render_evidence(last_meta: dict | None) -> None:
    """The marker view: what retrieval returned and why. Only drawn when enabled."""
    if last_meta is None:
        st.info("Ask a question to see what retrieval found.")
        return

    heading = "Retrieval evidence"
    if st.session_state.mode == "Customer":
        heading += " (marker view - not shown to the customer)"
    st.subheader(heading)

    st.markdown(f"**{STATUS_BADGE.get(last_meta['status'], last_meta['status'])}** - `{last_meta['reason'] or 'complete'}`")

    steps = [
        ("1. classify", last_meta["family"] or "unclassified"),
        ("2. collect key", ", ".join(last_meta["missing"]) if last_meta["missing"] else "key satisfied"),
        ("3. retrieve", f"{len(last_meta['hits'])} record(s)" if last_meta["hits"] else "no record matched"),
    ]
    st.markdown("\n".join(f"- **{name}** \u2192 {value}" for name, value in steps))

    st.markdown("**Fields extracted**")
    if last_meta["slots"]:
        st.markdown("\n".join(f"- `{k}` = {v}" for k, v in last_meta["slots"].items()))
    else:
        st.caption("None.")

    st.markdown("**Records retrieved**")
    if last_meta["hits"]:
        # Match type is the field that proves this is retrieval rather than a
        # model talking, so it goes in the table where it cannot be clipped.
        # The long values wrap as captions underneath.
        compact = [{"SKU": row["SKU"], "Match type": row["Match type"]} for row in last_meta["hits"]]
        st.dataframe(compact, width="stretch", hide_index=True)
        for row in last_meta["hits"]:
            st.caption(f"{row['SKU']} - {row['Record']}")
            st.caption(f"matched on: {row['Matched on']} | source: {row['Source']} | {row['Status']}")
    else:
        st.caption("No record matched the key. Nothing was offered as a substitute.")

    st.markdown("**Why**")
    st.caption(last_meta["note"])

    if last_meta["evidence"]:
        st.markdown("**Classification cues**")
        st.caption(
            " | ".join(f"{family}: {', '.join(words)}" for family, words in last_meta["evidence"].items())
        )

    if last_meta["skus"]:
        st.caption(f"SKUs named: {', '.join(last_meta['skus'])} - all present in the fixture.")
    else:
        st.caption("No SKU named in this exchange.")

    if last_meta["fallback"]:
        st.warning(f"LLM path fell back to the template: {last_meta['fallback']}")

    with st.expander("Answer route"):
        st.caption(
            f"Generated by: **{last_meta['route']}**. "
            "The template path is deterministic; the LLM path is optional and its output is "
            "rejected if it names a SKU that is not in the fixture."
        )


# --- Main ------------------------------------------------------------------------

st.title("Ozwide Tools - Fitment Assistant")
st.caption(
    "Mock-up for COSC2960 Assignment 3. Retrieval-augmented over the product listings "
    "and the timing lookup table. Answers are generated from retrieved records only."
)

show_panel = EVIDENCE_ENABLED and st.session_state.show_evidence

if show_panel:
    chat_col, evidence_col = st.columns([3, 2], gap="large")
else:
    # No side panel, so keep the conversation to a readable measure instead of
    # letting it stretch the full browser width.
    chat_col, evidence_col = st.container(width=900), None

with chat_col:
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if st.session_state.queue:
        while st.session_state.queue:
            _process(st.session_state.queue.pop(0))
        st.rerun()

    typed = st.chat_input("Describe the vehicle and the job, e.g. '2015 Golf 1.4 TSI timing belt'")
    if typed:
        _process(typed)
        st.rerun()

if evidence_col is not None:
    with evidence_col:
        if not st.session_state.show_evidence:
            st.info("Retrieval evidence is hidden. Turn on **Show retrieval evidence** in the sidebar to inspect the pipeline.")
        else:
            last_meta = next((m["meta"] for m in reversed(st.session_state.messages) if m["meta"]), None)
            render_evidence(last_meta)
