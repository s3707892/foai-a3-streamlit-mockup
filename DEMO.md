# Demo script

Two ways to run each scenario: press the button under **Demo scenarios** in the sidebar,
which plays the whole exchange, or type the inputs below. The buttons exist so the demo is
repeatable and cannot drift during a recording.

All inputs are verbatim. Every SKU, price and fitment claim in the output comes from the
fixture in `data/`.

---

## Scenario 1 - Timing, happy path

Button: **1. Timing - happy path**

| Turn | Input |
|---|---|
| 1 | `2015 Golf 1.4 TSI, doing the timing belt.` |
| 2 | `CXSA` |

**What it demonstrates.** The classifier identifies the timing family and knows its key is
the engine code, so it *refuses to answer* even though it has a make, a model, a year and a
displacement. It names the ten 1.4L codes in the catalogue to explain why displacement alone
cannot select a tool. Once the code arrives, retrieval matches `DM11-C1594` on
`listing_engine_code`, and because the year is 2015 the pre-2017 companion note fires.

The reply states its own provenance ("Matched on the engine code CXSA, which this listing's
fitment list carries under 1.4L"), so the workflow is visible without a debug panel.

---

## Scenario 2 - Timing, the table is authoritative

Button: **2. Timing - table uses provided lookup table first**

| Turn | Input |
|---|---|
| 1 | `VW 2.0 TDI, engine code DFGA, timing belt please.` |

**What it demonstrates.** The second source. `VW / DFGA` is a row in the timing lookup table,
so the table outranks every listing and returns `DM11-C2276` with `match_type=table_authoritative`.
This is the precedence rule in the design claim: *the table is authoritative wherever it has
a row*.

---

## Scenario 3 - Seal, the ambiguity path

Button: **3. Seal tool check**

| Turn | Input |
|---|---|
| 1 | `Rear main seal on a 4JJ1.` |
| 2 | `seal number 3-97071561` |

**What it demonstrates.** Engine code alone is **not** enough for this family. Retrieval
returns two candidates - `DM11-A1261` and `DM11-B1726` - and states the field that separates
them. It does not pick. The seal number then resolves to `DM11-A1261`, and the answer carries
the published 12.2-12.8mm seat depth and the negative constraint from the listing.

Worth saying out loud in the pitch: the depth for `DM11-B1726` **is not published**, so the
system says so instead of implying a figure.

Try `I measured 12.5mm` or `I measured 15mm` as the second turn instead. 12.5 matches on
depth; 15mm falls outside the published range and abstains.

---

## Scenario 4 - Bearing, matched on a measurement

Button: **4. Bearing tool check**

| Turn | Input |
|---|---|
| 1 | `Need a wheel bearing tool for a 2012 Hilux.` |
| 2 | `91.2mm` |

**What it demonstrates.** The strongest anti-hallucination case after the abstain. The
universal kit is matched on diameter, so a make and model cannot narrow it at all. The answer
says so explicitly, matches adapter `#4`, and never claims the kit fits a Hilux.

Variation worth showing if you have time: `the bearing outside diameter is 91.2mm`. The value
appears in the listing only as an **inside** diameter, so the answer flags the mismatch
rather than quietly using it.

---

## Scenario 5 - Abstain, out of catalogue

Button: **5. Unknown answer**

| Turn | Input |
|---|---|
| 1 | `Do you have a timing kit for a 2019 Hilux 2.8 diesel?` |

**What it demonstrates.** A recognised make with nothing held for it. The system abstains
immediately rather than asking for an engine code it could never resolve, invents no part
number, and hands off to a person.

**This is the most valuable scenario in the demo. Do not cut it.**



---

# 60-90 second recording script

Screen-record the browser window only, at full screen, with the app already loaded and the
conversation reset. The narration leans on the replies themselves: each answer states what it
matched on, which is what separates this from a chat box.

| Clock | Action | Narration (one line) |
|---|---|---|
| 0:00 | App loaded, Customer mode, conversation reset | "Ozwide Tools sells specialty automotive tooling. Staff answer the same fitment questions all day, so we built a retrieval assistant that answers from the catalogue instead of from model memory." |
| 0:08 | Click **1. Timing - happy path** | "A 2015 Golf, 1.4 TSI, timing belt. Watch what it does *not* do - it has the make, the model, the year and the size, and it still won't answer." |
| 0:16 | Point at the ten engine codes | "It says why: timing tooling is keyed on the engine code, and that displacement covers ten different codes in the catalogue." |
| 0:22 | The `CXSA` answer lands | "The code arrives, retrieval matches the listing on that code, and the pre-2017 companion note fires because we know the year." |
| 0:32 | Point at the "Matched on the engine code CXSA" line | "The reply states its own provenance - which field it matched on and which record it came from. Nothing in that answer came from the model." |
| 0:40 | Click **3. Seal - ambiguity** | "Seals behave differently. This family is keyed on the seal number, so an engine code gets you candidates only." |
| 0:47 | Point at the two candidates | "Two near-identical kits, and it names the field that separates them instead of picking one." |
| 0:52 | Seal number lands | "Seal number resolves it. Note it also tells us the other kit's depth isn't published, rather than guessing at it." |
| 1:00 | Click **4. Bearing - measurement, not vehicle** | "The universal bearing kit is matched on a measurement. So a Hilux tells it nothing." |
| 1:06 | `91.2mm` answer | "It asks for the diameter, matches the adapter, and never claims the kit fits the vehicle." |
| 1:14 | Click **5. Abstain - out of catalogue** | "Now the case that matters. A 2019 Hilux timing kit - a make we recognise, and nothing held for it." |
| 1:20 | Hold on the refusal | "It abstains. No part number, no guess, and a handoff to a person. A wrong timing tool costs an engine, so refusing is the correct behaviour, not a failure." |
| 1:28 | Click **6. Abstain - then push** | "And when the customer pushes - the refusal holds." |
| 1:33 | End | "Same catalogue, two audiences, and it never says anything it cannot point at a record for." |

## If the pitch runs short

- Toggle **Staff mode** on scenario 1 and re-run it: match type, data-quality flags on the
  listing, and "ask the customer next" appear.
- Scenario 2, the timing table outranking the listing: the two-source precedence rule.
- Type `the bearing outside diameter is 91.2mm` for scenario 4: the inside/outside diameter
  defect surfacing live.

## Recording notes

- Reset the conversation between takes (sidebar button) so the screen only shows the exchange
  you are talking about.
- If a marker asks how you know it is retrieval and not a model talking, the honest answer is
  in the code: `answer.assert_skus_known()` rejects any part number that is not in the fixture.
  There is also an optional pipeline panel - `FOAI_SHOW_EVIDENCE=1 streamlit run app.py` - that
  shows the retrieved record and match type per turn. It is off in the shipped UI.
- Nothing in the demo needs the network. Turn Wi-Fi off if you want that to be visible on
  camera, though the app will not notice either way.
