"""The fixture must be honest: raw text untouched, parsers reading real fields."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_fixture  # noqa: E402
import retrieval  # noqa: E402

DATA = ROOT / "data"


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads((DATA / "listings.json").read_text(encoding="utf-8"))


def test_raw_text_is_byte_identical_to_the_source_files(fixture):
    listings = {r["sku"]: r for r in fixture["records"] if r["has_listing"]}
    assert len(listings) == 3
    for sku, rec in listings.items():
        path = ROOT / rec["source_file"]
        assert path.read_text(encoding="utf-8") == rec["raw_text"], f"{sku} raw_text drifted from {path}"


def test_committed_fixture_matches_a_fresh_build(fixture):
    """If this fails, someone edited listings.json by hand or forgot to rebuild."""
    assert build_fixture.build() == fixture


def test_timing_table_is_copied_verbatim():
    csv_text = (DATA / "toolfinder.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == "Make,Eng,ToolNumber,AltToolNumber,RentalCost,Length,Width,Height,Weight,ROWID"
    assert len(csv_text.strip().splitlines()) == 6


def test_messy_source_features_survive_parsing(fixture):
    index = {r["sku"]: r for r in fixture["records"]}

    # Isuzu codes start with a digit, which a naive [A-Z] regex drops silently.
    assert "4JJ1" in index["DM11-A1261"]["engine_codes"]
    assert "4JX1" in index["DM11-A1261"]["engine_codes"]

    # Four spellings of one seal number must collapse to one key, and the one that
    # starts with 8 is genuinely a different digit string - kept, not corrected.
    published = {s["normalised"] for s in index["DM11-A1261"]["seal_numbers"]}
    assert "397071561" in published
    assert "897071561" in published
    assert len(published) == 3  # the BZ code, the 3-prefixed group, the 8-prefixed one

    # The double-blank-line continuation block is parsed, not skipped.
    assert index["DM11-C1594"]["engine_codes"]["DADA"]["listed_under"] == "Other Engine codes"

    # Displacement labels attach to the codes on their own line.
    assert index["DM11-C1594"]["engine_codes"]["CXSA"]["displacement"] == "1.4"

    # Negative constraint and seat depth both survive.
    assert index["DM11-A1261"]["negative_constraints"][0]["excludes"] == "4BE"
    assert index["DM11-A1261"]["seat_depth_mm"]["min"] == 12.2

    # A typo'd companion resolves through the alias table instead of being scrubbed.
    assert fixture["aliases"]["DM1-A1872"]["alias_of"] == "DM11-A1872"

    # The listing really does advertise a 1.0L it has no codes for.
    assert "1.0L" in index["DM11-C1594"]["title"]


def test_every_record_sku_appears_in_a_source(fixture):
    """No SKU may exist only because someone typed it into the fixture."""
    sources = "".join((DATA / "raw" / f).read_text(encoding="utf-8") for f in ("DM11-C1594.txt", "DM11-A1261.txt", "DM11-B1008.txt"))
    sources += (DATA / "toolfinder.csv").read_text(encoding="utf-8")
    for rec in fixture["records"]:
        assert rec["sku"] in sources, f"{rec['sku']} is not mentioned in any source file"
    for alias in fixture["aliases"]:
        assert alias in sources, f"alias {alias} is not a real spelling in the source text"


def test_record_count_and_families(fixture):
    families = {r["family"] for r in fixture["records"]}
    assert families == {"timing", "seal", "bearing"}
    # 3 full listings, 2 timing-table tools, 3 cross-referenced companions.
    assert len(fixture["records"]) == 8


def test_family_makes_come_from_the_text(fixture):
    assert retrieval.family_makes("timing", fixture) >= {"VW", "AUDI", "SEAT", "SKODA"}
    assert retrieval.family_makes("seal", fixture) >= {"ISUZU", "GM/Vauxhall/Opel"}
    # The bearing kit claims no make, because it is matched on measurement.
    assert retrieval.family_makes("bearing", fixture) == set()
