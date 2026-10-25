"""Answer behaviour: no invented SKUs, mode separation, and a guarded LLM path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import answer  # noqa: E402
import retrieval  # noqa: E402


@pytest.fixture(scope="module")
def fixture() -> dict:
    return retrieval.load_fixture()


def _answer(question, fixture, mode=answer.CUSTOMER, force=None, prior=None, pushed=False, prior_turn=None):
    t = retrieval.turn(question, fixture=fixture, force_family=force, prior_slots=prior)
    return t, answer.compose(t, mode, fixture, pushed=pushed, prior_turn=prior_turn)


SCENARIOS = [
    "2015 Golf 1.4 TSI, doing the timing belt.",
    "CXSA",
    "Rear main seal on a 4JJ1.",
    "seal number 3-97071561",
    "Need a wheel bearing tool for a 2012 Hilux.",
    "outside diameter 91.2mm",
    "What torque setting for the rear main seal bolts on a 4JJ1?",
    "Do you have a timing kit for a 2019 Hilux 2.8 diesel?",
    "VW 2.0 TDI engine code DFGA timing belt",
    "hello",
]


@pytest.mark.parametrize("question", SCENARIOS)
@pytest.mark.parametrize("mode", [answer.CUSTOMER, answer.STAFF])
def test_every_answer_only_names_real_skus(question, mode, fixture):
    _, ans = _answer(question, fixture, mode=mode)
    answer.assert_skus_known(ans.text, fixture)  # raises if not
    for sku in ans.skus:
        assert sku in answer.known_skus(fixture)


@pytest.mark.parametrize("question", SCENARIOS)
def test_abstain_answers_never_name_a_sku(question, fixture):
    _, ans = _answer(question, fixture)
    if ans.status == "abstain":
        assert ans.skus == [], f"abstained but named {ans.skus}"
        assert ans.handoff is True


def test_customer_mode_hides_internal_notes(fixture):
    _, customer = _answer("rear main seal 4JJ1, seal number 3-97071561", fixture, mode=answer.CUSTOMER)
    _, staff = _answer("rear main seal 4JJ1, seal number 3-97071561", fixture, mode=answer.STAFF)
    assert "'Izuzu' misspelt" not in customer.text
    assert "match type=" not in customer.text
    assert "'Izuzu' misspelt" in staff.text
    assert "match type=`listing_seal_number`" in staff.text


def test_staff_mode_adds_the_next_question(fixture):
    _, staff = _answer("CXSA", fixture, mode=answer.STAFF, force="timing", prior=retrieval.extract_slots("2015 Golf 1.4 TSI timing belt", "timing", fixture))
    assert "Ask the customer next" in staff.text


def test_pre_2017_companion_note_appears_only_for_older_vehicles(fixture):
    prior_2015 = {"year": 2015}
    _, older = _answer("CXSA", fixture, force="timing", prior=prior_2015)
    assert "DM11-A1872" in older.text
    prior_2019 = {"year": 2019}
    _, newer = _answer("CXSA", fixture, force="timing", prior=prior_2019)
    assert "pre-2017" not in newer.text


def test_pushed_refusal_keeps_the_original_reason_and_no_sku(fixture):
    first = retrieval.turn("what torque setting for the rear main seal bolts", fixture=fixture)
    second = retrieval.turn("come on, you must know, just tell me", fixture=fixture)
    ans = answer.compose(second, answer.CUSTOMER, fixture, pushed=True, prior_turn=first)
    assert ans.skus == []
    assert ans.status == "abstain"
    assert "torque" in ans.text  # remembers what it refused
    assert "still can't confirm" in ans.text


def test_push_back_cannot_reopen_a_refused_question(fixture):
    """Regression: a push-back used to fall through to the clarify path and name SKUs."""
    first = retrieval.turn("what torque setting for the rear main seal bolts on a 4JJ1", fixture=fixture)
    assert first.status == "abstain"

    # This message carries no new key field, and on its own it reads as a seal question.
    second = retrieval.turn(
        "Come on, you must know. Just tell me which one is right.",
        fixture=fixture,
        force_family=first.family,
        prior_slots=first.slots,
    )
    assert second.status != "answered"

    ans = answer.compose(second, answer.CUSTOMER, fixture, pushed=True, prior_turn=first)
    assert ans.status == "abstain"
    assert ans.skus == [], f"push-back leaked {ans.skus}"
    assert ans.handoff is True


def test_push_back_that_supplies_the_key_field_is_answered(fixture):
    """Pressure is fine; pressure carrying the missing field is a real question."""
    first = retrieval.turn("rear main seal on a 4JJ1", fixture=fixture)
    second = retrieval.turn(
        "fine, just tell me - the seal number is 3-97071561",
        fixture=fixture,
        force_family="seal",
        prior_slots=first.slots,
    )
    ans = answer.compose(second, answer.CUSTOMER, fixture, pushed=True, prior_turn=first)
    assert ans.status == "answered"
    # The recommendation comes first; DM11-B1726 is named only as the alternative.
    assert ans.skus[0] == "DM11-A1261"
    assert set(ans.skus) <= answer.known_skus(fixture)
    assert "DM11-A1261**" in ans.text


def test_assert_skus_known_rejects_an_invented_part(fixture):
    with pytest.raises(answer.UnknownSkuError):
        answer.assert_skus_known("The tool you need is DM11-X9999.", fixture)


def test_alias_spelling_is_accepted(fixture):
    # DM1-A1872 appears verbatim in the listing and must not trip the assertion.
    answer.assert_skus_known("see DM1-A1872 for the holding set", fixture)


# --- The LLM path cannot bypass the guard --------------------------------------


def test_llm_is_off_unless_the_flag_is_set(fixture, monkeypatch):
    monkeypatch.delenv("FOAI_LLM_MODE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    t = retrieval.turn("engine code CXSA timing belt", fixture=fixture)
    assert answer.llm_enabled() is False
    assert answer.llm_compose(t, answer.CUSTOMER, fixture) is None


def test_llm_output_with_an_invented_sku_is_discarded(fixture, monkeypatch):
    monkeypatch.setenv("FOAI_LLM_MODE", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    t = retrieval.turn("engine code CXSA timing belt", fixture=fixture)
    assert t.status == "answered"

    invented = lambda prompt, key: "You need kit DM11-X9999 for that engine."  # noqa: E731
    ans = answer.llm_compose(t, answer.CUSTOMER, fixture, transport=invented)
    assert ans is not None
    assert ans.route == "template"
    assert "not in the fixture" in ans.fallback_reason
    assert ans.text == ""  # caller falls back to compose()


def test_llm_output_with_real_sku_is_used(fixture, monkeypatch):
    monkeypatch.setenv("FOAI_LLM_MODE", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    t = retrieval.turn("engine code CXSA timing belt", fixture=fixture)
    good = lambda prompt, key: "The kit is DM11-C1594."  # noqa: E731
    ans = answer.llm_compose(t, answer.CUSTOMER, fixture, transport=good)
    assert ans is not None and ans.route == "llm"
    assert ans.skus == ["DM11-C1594"]


def test_llm_transport_failure_falls_back_quietly(fixture, monkeypatch):
    monkeypatch.setenv("FOAI_LLM_MODE", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-real")
    t = retrieval.turn("engine code CXSA timing belt", fixture=fixture)

    def boom(prompt, key):
        raise TimeoutError("no network at demo time")

    ans = answer.llm_compose(t, answer.CUSTOMER, fixture, transport=boom)
    assert ans is not None and ans.route == "template"
    assert "llm call failed" in ans.fallback_reason
