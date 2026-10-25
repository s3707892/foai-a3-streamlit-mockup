"""Build data/listings.json from the pristine raw sources.

The files in data/raw/ and data/toolfinder.csv are the source of truth and are
never edited. This script is the only thing that writes listings.json.

Why a build step at all: the project's whole argument is that the catalogue text
is messy, so the retriever has to parse around real defects rather than read
fields someone typed in by hand. Running this script proves the fields below
actually come out of the raw text.

    python build_fixture.py          # rebuild data/listings.json
    python build_fixture.py --check  # verify the committed file is current
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"

# DM11-C1594, DM11-C1095-5H, and the typo DM1-A1872 all have to match.
SKU_RE = re.compile(r"\bDM\d{1,2}-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
CODE_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{3,6}$")

# Words that look like engine codes but are not.
CODE_STOP = {
    "TSI", "TFSI", "TGI", "GST", "OEM", "NOTE", "CODS", "VVTI",
    "ENGINES", "MODELS", "CODES", "INC", "SKU", "CV", "IRS", "DMAX", "OD", "DATA",
}

FAMILY_BY_SKU = {
    "DM11-C1594": "timing",
    "DM11-C2276": "timing",
    "DM13-8900": "timing",
    "DM11-A1872": "timing",
    "DM11-A1261": "seal",
    "DM11-B1726": "seal",
    "DM11-B1008": "bearing",
    "DM11-C1095-5H": "bearing",
}


def _lines(raw: str) -> list[str]:
    return raw.splitlines()


def _sku(raw: str) -> str:
    return _lines(raw)[0].split(":", 1)[1].strip()


def _title(raw: str) -> str:
    return _lines(raw)[1].strip()


def _price(raw: str) -> float | None:
    m = re.search(r"\$([\d,]+\.\d{2})", raw)
    return float(m.group(1).replace(",", "")) if m else None


def _engine_block(raw: str) -> list[str]:
    """The 'Engines:' lines. Continuation lines are indented; section headers stop us."""
    out: list[str] = []
    started = False
    for line in _lines(raw):
        s = line.strip()
        if not started:
            if s.lower().startswith("engines:"):
                started = True
                out.append(s)
            continue
        if not s:
            # A blank line inside the block is not the end of it: listing 1 puts the
            # 'Other Engine codes' continuation after two blank lines.
            continue
        if s.startswith(("NOTE", "OEM", "Equivalent", "Important", "Sizes", "Models")):
            break
        if not (line[:1].isspace() or s.lower().startswith("engines:")):
            break
        out.append(s)
    return out


def _engine_codes(raw: str) -> dict[str, dict]:
    """code -> {displacement, fuel}. Listing 1 groups codes under 1.2L TFSI etc."""
    found: dict[str, dict] = {}
    for line in _engine_block(raw):
        line = re.sub(r"\([^)]*\)", " ", line)
        # The block header ('Engines: 1.2L TFSI: CJZA,...') carries a displacement
        # label of its own, so drop the header word before looking for the label.
        line = re.sub(r"^Engines:\s*", "", line, flags=re.IGNORECASE)
        if ":" in line:
            label, _, rest = line.partition(":")
        else:
            # '1.4L TSI Multi Fuel CPVA' has no colon - the whole line is the label.
            label, rest = line, line
        disp = re.search(r"(\d\.\d)\s*L", label)
        fuel = re.search(r"\b(TSI|TFSI|TGI)\b", label)
        detail = {
            "displacement": disp.group(1) if disp else None,
            "fuel": fuel.group(1) if fuel else None,
            "listed_under": label.strip() or None,
        }
        for tok in re.split(r"[,/\s]+", rest):
            tok = tok.strip(" .,")
            # 4JH1-TC is a variant of the 4JH1 family; the fitment key is the base code.
            base = tok.split("-")[0]
            if not base or base in CODE_STOP or not CODE_RE.match(base):
                continue
            found.setdefault(base, detail)
    return found


def _seal_numbers(raw: str) -> list[dict]:
    m = re.search(r"OEM Seal Number:\s*(.+)", raw)
    if not m:
        return []
    out = []
    for part in m.group(1).split("/"):
        part = part.strip()
        if part:
            out.append({"as_published": part, "normalised": re.sub(r"[^0-9A-Z]", "", part.upper())})
    return out


def _depth(raw: str) -> dict | None:
    m = re.search(r"[Dd]epth for the (DM\d+-\w+) is ([\d.]+)-([\d.]+)\s*mm", raw)
    if not m:
        return None
    return {"sku": m.group(1), "min": float(m.group(2)), "max": float(m.group(3))}


def _diameters(raw: str) -> list[dict]:
    out = []
    for m in re.finditer(r"#(\d+)\s+(INSIDE|OUTSIDE)\s+DIAMETER:\s*([\d.]+)\s*mm", raw):
        out.append({"adapter": f"#{m.group(1)}", "side": m.group(2).lower(), "mm": float(m.group(3))})
    return out


def _model_lines(raw: str) -> list[str]:
    out: list[str] = []
    started = False
    for line in _lines(raw):
        s = line.strip()
        if not started:
            if s.lower().startswith("models"):
                started = True
                # Listing 3 puts the models on the same line as the label.
                if ":" in s and s.partition(":")[2].strip():
                    out.append(s.partition(":")[2].strip())
            continue
        if not s:
            # 'Models Suits:' is followed by a blank line before the first entry.
            if out:
                break
            continue
        if s.startswith(("Engines", "NOTE", "OEM", "Important")):
            break
        out.append(s)
    return out


def _negative_constraints(raw: str) -> list[dict]:
    out = []
    for m in re.finditer(r"Will NOT fit ([A-Z0-9]+)", raw):
        out.append({"excludes": m.group(1), "verbatim": m.group(0)})
    return out


def _related_skus(raw: str, self_sku: str) -> list[str]:
    seen = []
    for m in SKU_RE.finditer(raw):
        s = m.group(0)
        if s != self_sku and s not in seen:
            seen.append(s)
    return seen


QUALITY_FLAGS = {
    "DM11-C1594": [
        "title advertises 1.0L / 1.2L / 1.4L but the engine list has no 1.0L codes",
        "companion tool written as 'DM1-A1872' in one note and 'DM11-A1872' in another - one digit, one typo",
        "'Other Engine codes' block gives codes with no displacement, so displacement cannot narrow them",
    ],
    "DM11-A1261": [
        "'Izuzu' misspelt in the models block",
        "a Colorado is filed under Isuzu",
        "model year embedded in a model name: 'Colorado 3.0L 4JX1 2008'",
        "one seal number published four ways: 3-97071561 / 397071561 / 39707 1561 / 8-97071561",
        "carries a negative constraint (Will NOT fit 4BE) that naive text search would read as a fit",
        "alternative kit DM11-B1726 is named but its seat depth is never published",
    ],
    "DM11-B1008": [
        "inside and outside diameters are both listed with no rule for which the customer should measure",
        "generic model list, but the tool is matched on measurement, not on vehicle",
        "VITO / Sprinter front bearings are carved out as an exception",
        "typos in source: 'replaced wheels bearings', 'Spinters', 'and many other others'",
    ],
}


def _makes(raw: str, model_lines: list[str]) -> list[str]:
    """Surface forms of every make this listing claims. Not normalised here.

    Two real sources: the brand prefixes in the models block ('VW: Up / Golf'),
    and the title line ('Audi / VW 1.0L / 1.2L ...'). Listing 3 has neither, so
    it claims no specific make - which is correct, it is matched on measurement.
    """
    found: list[str] = []
    for line in model_lines:
        m = re.match(r"^([A-Za-z][A-Za-z0-9 \-/]{1,30}):", line)
        if m:
            for part in m.group(1).split("/"):
                part = part.strip()
                if part and part not in found:
                    found.append(part)
    title = _lines(raw)[1]
    for part in re.split(r"\s+/\s+", title):
        part = part.strip()
        # Walk the leading run of plain alpha segments only: 'Audi / VW 1.0L / 1.2L'
        # gives 'Audi', then stops at 'VW 1.0L' - so VW is caught by the models block.
        if re.search(r"\d", part) or len(part) > 20:
            break
        if re.match(r"^[A-Za-z][A-Za-z ]+$", part) and part not in found:
            found.append(part)
    for extra in ("GM", "Isuzu"):
        if re.search(r"\b" + extra + r"\b", title) and extra not in found:
            found.append(extra)
    return found


def _raw_listing_record(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    sku = _sku(raw)
    model_lines = _model_lines(raw)
    return {
        "sku": sku,
        "title": _title(raw),
        "family": FAMILY_BY_SKU[sku],
        "source": "listing",
        "source_file": f"data/raw/{path.name}",
        "has_listing": True,
        "price_inc_gst": _price(raw),
        "makes": _makes(raw, model_lines),
        "engine_codes": _engine_codes(raw),
        "seal_numbers": _seal_numbers(raw),
        "seat_depth_mm": _depth(raw),
        "adapter_diameters": _diameters(raw),
        "model_lines": model_lines,
        "negative_constraints": _negative_constraints(raw),
        "related_skus": _related_skus(raw, sku),
        "quality_flags": QUALITY_FLAGS.get(sku, []),
        "raw_text": raw,
    }


def _timing_table() -> list[dict]:
    with (DATA / "toolfinder.csv").open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _table_records(rows: list[dict]) -> list[dict]:
    by_tool: dict[str, dict] = {}
    for row in rows:
        tool = row["ToolNumber"]
        rec = by_tool.setdefault(
            tool,
            {
                "sku": tool,
                "title": "Timing tool (no website listing held)",
                "family": "timing",
                "source": "timing_table",
                "source_file": "data/toolfinder.csv",
                "has_listing": False,
                "price_inc_gst": None,
                "rental_cost": float(row["RentalCost"]),
                "dimensions_mm": {
                    "length": float(row["Length"]),
                    "width": float(row["Width"]),
                    "height": float(row["Height"]),
                },
                "weight_kg": float(row["Weight"]),
                "table_rows": [],
                "quality_flags": [
                    "the table is keyed on make + engine code only: no model, no year, no displacement",
                ],
                "raw_text": "",
            },
        )
        rec["table_rows"].append(
            {"make": row["Make"], "eng": row["Eng"], "alt_tool": row["AltToolNumber"] or None, "rowid": row["ROWID"]}
        )
    return list(by_tool.values())


def _cross_reference_records(listings: list[dict]) -> list[dict]:
    """Tools named by the listings that we hold no listing for. No fields are invented."""
    out = []
    seen = set()
    for listing in listings:
        for sku in listing["related_skus"]:
            # A misspelt companion is an alias of a real SKU, not a separate product.
            if sku in seen or sku in ALIASES or sku in {l["sku"] for l in listings}:
                continue
            seen.add(sku)
            verbatim = ""
            for line in _lines(listing["raw_text"]):
                if sku in line:
                    verbatim = line.strip()
                    break
            out.append(
                {
                    "sku": sku,
                    "title": f"Cross-referenced by {listing['sku']} - no listing held in this fixture",
                    "family": FAMILY_BY_SKU.get(sku, listing["family"]),
                    "source": "cross_reference",
                    "source_file": listing["source_file"],
                    "has_listing": False,
                    "price_inc_gst": None,
                    "referred_to_by": listing["sku"],
                    "referring_line_verbatim": verbatim,
                    "quality_flags": [],
                    "raw_text": "",
                }
            )
    return out


ALIASES = {
    # Listing 1 spells the companion tool two ways. Neither is corrected; both resolve.
    "DM1-A1872": {"alias_of": "DM11-A1872", "reason": "one-digit typo in a note on DM11-C1594"},
}


def build() -> dict:
    listings = [_raw_listing_record(p) for p in sorted(RAW.glob("*.txt"))]
    rows = _timing_table()
    records = listings + _table_records(rows) + _cross_reference_records(listings)

    fixtures = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16] for p in sorted(RAW.glob("*.txt"))}
    fixtures["toolfinder.csv"] = hashlib.sha256((DATA / "toolfinder.csv").read_bytes()).hexdigest()[:16]

    return {
        "meta": {
            "description": "Ozwide Tools fitment assistant fixture. Two sources: website listings and the timing lookup table.",
            "authority": "Where the timing table has a (make, engine code) row it outranks every listing.",
            "raw_sources_sha256_16": fixtures,
            "listing_count": len(listings),
            "record_count": len(records),
            "timing_table_rows": len(rows),
            "not_included": "The live Ozwide catalogue. This is a fixed fixture so the demo runs offline.",
        },
        "aliases": ALIASES,
        "records": records,
    }


def main(argv: list[str]) -> int:
    payload = build()
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    out = DATA / "listings.json"
    if "--check" in argv:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current != text:
            print("listings.json is stale - rerun build_fixture.py", file=sys.stderr)
            return 1
        print("listings.json is current")
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({payload['meta']['record_count']} records, {payload['meta']['listing_count']} with full listings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
