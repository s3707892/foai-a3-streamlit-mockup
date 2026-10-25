"""Retrieval behaviour: family classification, key collection, matching, abstention."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import retrieval  # noqa: E402


@pytest.fixture(scope="module")
def fixture() -> dict:
    return retrieval.load_fixture()


# --- Classification -------------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected",
    [
        ("2015 Golf 1.4 TSI, doing the timing belt.", "timing"),
        ("Rear main seal on a 4JJ1.", "seal"),
        ("Need a wheel bearing tool for a 2012 Hilux.", "bearing"),
        ("timing chain rattle on a VW", "timing"),
        ("front crank oil seal is leaking", "seal"),
        ("pressing in a wheel hub", "bearing"),
    ],
)
def test_classify_picks_the_family(question, expected):
    assert retrieval.classify(question) == expected


def test_classify_refuses_to_guess_when_two_families_tie():
    assert retrieval.classify("do you sell a seal or a bearing") is None
    assert retrieval.classify("hello, are you open saturday") is None


def test_non_fitment_intent_is_detected():
    assert retrieval.non_fitment_terms("what torque setting for the bolts") == ["torque"]
    assert retrieval.non_fitment_terms("how tight should it be") == ["how tight"]
    assert retrieval.non_fitment_terms("rear main seal on a 4JJ1") == []


# --- Slot extraction ------------------------------------------------------------


def test_engine_size_is_read_from_a_tsi_badge(fixture):
    slots = retrieval.extract_slots("2015 Golf 1.4 TSI timing belt", "timing", fixture)
    assert slots["displacement"] == "1.4"
    assert slots["year"] == 2015
    assert slots["make"] == "VW"  # inferred from the model name


def test_measurement_is_not_mistaken_for_an_engine_size(fixture):
    slots = retrieval.extract_slots("the diameter is 12.5mm", "bearing", fixture)
    assert slots["displacement"] is None
    assert slots["diameter_mm"] == 12.5


def test_known_and_unknown_engine_codes_are_distinguished(fixture):
    known = retrieval.extract_slots("engine code CXSA", "timing", fixture)
    assert known["engine_code"] == "CXSA" and known["engine_code_known"] is True

    unknown = retrieval.extract_slots("engine code BKD", "timing", fixture)
    assert unknown["engine_code"] == "BKD" and unknown["engine_code_known"] is False


@pytest.mark.parametrize("written", ["3-97071561", "397071561", "39707 1561"])
def test_seal_number_is_matched_however_it_is_punctuated(written, fixture):
    slots = retrieval.extract_slots(f"seal number {written}", "seal", fixture)
    assert slots["seal_number"] == "397071561"


# --- Missing key fields ---------------------------------------------------------


def test_missing_fields_per_family(fixture):
    empty = retrieval.extract_slots("", None, fixture)
    assert retrieval.missing_fields("timing", empty) == ["engine_code"]
    assert retrieval.missing_fields("seal", empty) == ["seal_number_or_depth"]
    assert retrieval.missing_fields("bearing", empty) == ["inside_diameter_mm", "outside_diameter_mm"]
    assert retrieval.missing_fields(None, empty) == ["problem_description"]

    with_code = {**empty, "engine_code": "CXSA"}
    assert retrieval.missing_fields("timing", with_code) == []


# --- Matching -------------------------------------------------------------------


def test_timing_table_outranks_the_listing(fixture):
    t = retrieval.turn("VW 2.0 TDI, engine code DFGA, timing belt", fixture=fixture)
    assert t.status == "answered"
    assert t.hits[0].sku == "DM11-C2276"
    assert t.hits[0].match_type == "table_authoritative"


def test_timing_falls_back_to_the_listing_engine_list(fixture):
    t = retrieval.turn("2015 Golf 1.4 TSI timing belt, engine code CXSA", fixture=fixture)
    assert t.status == "answered"
    assert t.hits[0].sku == "DM11-C1594"
    assert t.hits[0].match_type == "listing_engine_code"


def test_seal_engine_alone_returns_candidates_not_an_answer(fixture):
    t = retrieval.turn("Rear main seal on a 4JJ1", fixture=fixture)
    assert t.status == "clarify"
    assert t.reason == "ambiguous_needs_separator"
    assert {h.sku for h in t.hits} == {"DM11-A1261", "DM11-B1726"}
    assert all(h.is_candidate for h in t.hits)


def test_seal_number_resolves_the_ambiguity(fixture):
    t = retrieval.turn("rear main seal 4JJ1, seal number 3-97071561", fixture=fixture)
    assert t.status == "answered"
    assert t.hits[0].sku == "DM11-A1261"
    assert t.hits[0].match_type == "listing_seal_number"


def test_seal_depth_inside_the_range_matches(fixture):
    t = retrieval.turn("rear main seal on a 4JJ1, measured 12.5mm", fixture=fixture)
    assert t.status == "answered"
    assert t.hits[0].sku == "DM11-A1261"


def test_seal_depth_outside_the_range_abstains(fixture):
    t = retrieval.turn("rear main seal on a 4JJ1, measured 15mm", fixture=fixture)
    assert t.status == "abstain"
    assert t.reason == "depth_outside_published_range"
    assert t.hits == []


def test_bearing_matches_on_measurement(fixture):
    t = retrieval.turn("wheel bearing kit, bearing outside diameter 91.2mm", fixture=fixture)
    assert t.status == "answered"
    assert t.hits[0].sku == "DM11-B1008"
    assert "91.2" in t.hits[0].matched_fields["adapter"] or t.hits[0].matched_fields["adapter"]


def test_bearing_vehicle_alone_never_claims_fitment(fixture):
    t = retrieval.turn("wheel bearing tool for a 2012 Hilux", fixture=fixture)
    assert t.hits == []
    assert t.status == "clarify"
    assert "inside_diameter_mm" in t.missing


# --- Abstention -----------------------------------------------------------------


def test_torque_question_abstains_with_no_sku(fixture):
    t = retrieval.turn("what torque setting for the rear main seal bolts", fixture=fixture)
    assert t.status == "abstain"
    assert t.reason == "non_fitment_intent"
    assert t.hits == []


def test_unstocked_make_abstains_without_asking_for_a_code(fixture):
    t = retrieval.turn("timing kit for a 2019 Hilux 2.8 diesel", fixture=fixture)
    assert t.status == "abstain"
    assert t.reason == "make_not_in_catalogue"
    assert t.hits == []


def test_unknown_engine_code_abstains(fixture):
    prior = retrieval.extract_slots("2015 Golf 1.4 TSI timing belt", "timing", fixture)
    t = retrieval.turn("BKD", fixture=fixture, force_family="timing", prior_slots=prior)
    assert t.status == "abstain"
    assert t.reason == "engine_code_not_in_catalogue"


def test_no_family_offers_no_retrieval(fixture):
    t = retrieval.turn("hello, are you open saturday", fixture=fixture)
    assert t.status == "abstain"
    assert t.reason == "no_family"
    assert t.hits == []


# --- The invariant that matters -------------------------------------------------


def test_no_question_can_return_a_sku_outside_the_fixture(fixture):
    """Whatever the input, retrieval only ever returns records that exist."""
    allowed = retrieval.known_skus()
    questions = [
        "timing belt for a 2015 Golf 1.4 TSI",
        "rear main seal 4JJ1",
        "wheel bearing tool",
        "torque specs for head bolts",
        "what tool do I need",
        "",
        "DM11-ZZZZ-999",
        "give me a random part number",
        "timing kit for a Ferrari 488",
        "seal tool for a John Deere tractor",
        "VW DFGA", "GM LTG", "Opel A20NFT", "Holden LHU",
    ]
    for question in questions:
        for force in (None, "timing", "seal", "bearing", "nonsense"):
            t = retrieval.turn(question, fixture=fixture, force_family=force)
            for hit in t.hits:
                assert hit.sku in allowed, f"{question!r} returned unknown SKU {hit.sku}"
