# B4 vision-lite item bank — review notes (hardened)

Stage 0 discriminator battery for the multimodal seats
(`multimodal_looker`, `visual_engineering`, `circuit_engineer`, `artistry`).

Per spec §5.4: vision-lite targets multimodal_looker / visual_engineering / artistry.
**Hardened revision**: expanded from 15 → 19 items. Five items retained as easy
baselines (tiers 2–3); 14 new items added at tiers 4–5 that require density,
fine discrimination, multi-step extraction, cross-region correlation, and
complex topology traversal.

All answers derived from generator parameters; committed answers are
recomputed by `regenerate_verify.py` on every invocation.

---

## Calibration finding (pre-hardening)

qwen3.7-plus scored **100 % on all 15 original items**. Root cause: every item
was single-look, low-density (5 sidebar items, 5 bars, 4 fields, 5 nodes,
5 resistors). A competent vision model reads them trivially. This hardened
revision introduces the difficulty mechanisms below.

## Difficulty mechanisms

| Mechanism | What it tests | Example items |
|---|---|---|
| **(a) Density** | scanning accuracy with more elements | UI06 (9 sidebar items), SCH10 (8 resistors), CRT06 (8 bars) |
| **(b) Fine discrimination** | near-identical labels differ by one character | UI08 ("Rollback" vs "Roll back"), CRT06 (values within 7 units) |
| **(c) Multi-step extraction** | 2–3 reads chained to reach the answer | UI07 (find disabled field → identify field below it), CRT09 (find min → double → find match) |
| **(d) Cross-region** | correlate sidebar highlight with table row | UI09 (Service Monitor dashboard), UI10 (CJK management panel) |
| **(e) Topology** | path tracing in complex graphs | SCH06 (trace path via Recorder), SCH07 (bypass path yes/no), SCH08/SCH09 (edge counting, odd-one-out) |

---

## Item list

| # | item_key | kind | tier | answer | hardness mechanism |
|---|---|---|---|---|---|
| 1 | `vision.ui_read.sidebar-count` | ui_read | 2 | `5` | easy baseline |
| 2 | `vision.ui_read.window-title-cta` | ui_read | 2 | `Export Report` | easy baseline |
| 3 | `vision.ui_read.dense-sidebar` | ui_read | **4** | `Billing` | density (9 items) |
| 4 | `vision.ui_read.multi-state-form` | ui_read | **5** | `Confirm Password` | multi-step (disabled → below) |
| 5 | `vision.ui_read.near-label-buttons` | ui_read | **4** | `Roll back` | fine discrimination |
| 6 | `vision.ui_read.dashboard-cross-region` | ui_read | **5** | `WARNING` | cross-region correlation |
| 7 | `vision.ui_read.cjk-dense-table` | ui_read | **4** | `32` | CJK + cross-region |
| 8 | `vision.schematic.signal-flow` | schematic | 3 | `yes` | easy baseline |
| 9 | `vision.schematic.dense-path` | schematic | **5** | `Limiter, Splitter, Recorder, Output` | topology path trace |
| 10 | `vision.schematic.bypass-path` | schematic | **5** | `yes` | topology (bypass detection) |
| 11 | `vision.schematic.node-degree` | schematic | **4** | `4` | density (count touching edges) |
| 12 | `vision.schematic.dual-destination` | schematic | **4** | `TaskD` | topology odd-one-out |
| 13 | `vision.schematic.dense-resistor-net` | schematic | **5** | `4` | density (8 resistors, GND count) |
| 14 | `vision.chart_extract.bar-max` | chart_extract | 2 | `B, 72` | easy baseline |
| 15 | `vision.chart_extract.trend-direction` | chart_extract | 3 | `decreasing` | easy baseline |
| 16 | `vision.chart_extract.eight-near-bars` | chart_extract | **4** | `48` | density + precision read |
| 17 | `vision.chart_extract.smallest-gap` | chart_extract | **5** | `Q5` | 12 bars, find min gap |
| 18 | `vision.chart_extract.three-lines-middle` | chart_extract | **5** | `Gamma` | 3 lines, median at x=5 |
| 19 | `vision.chart_extract.exact-double` | chart_extract | **4** | `Beta` | multi-step (find min → double) |

### Tier distribution

| Tier | Count | Target pass-rate (spec §5.2) |
|---|---|---|
| 2 (baseline) | 3 | 70–90 % |
| 3 (distinction) | 2 | 50–70 % |
| 4 (upper distinction) | 6 | 30–50 % |
| 5 (head discriminator) | 8 | 10–30 % |

### Kind distribution

| Kind | Count |
|---|---|
| ui_read | 7 |
| schematic | 6 |
| chart_extract | 6 |

Grading strategy: every item uses `exact_match@1.0`. Answers are short
unambiguous strings or numbers.

---

## Why the old 100 % anchor will now drop to ~30–60 % on the hard tier

The old bank had max density of 5 elements per scene. The hardened tier 4–5
items introduce:

1. **Counting in dense scenes** — UI06 (9 sidebar items, find 7th), SCH10
   (8 resistors touching GND), SCH08 (edges touching Router among 9 total).
   The model must scan every element, not just the nearest few.

2. **Multi-step extraction** — UI07 requires finding the disabled field THEN
   identifying the field directly below it (2-step). CRT09 requires finding
   the minimum value THEN doubling it THEN matching which bar equals that.

3. **Cross-region correlation** — UI09/UI10 require reading the sidebar
   highlight AND finding the matching row in the data table. The answer lives
   at the intersection of two visual regions.

4. **Fine discrimination** — UI08 has "Rollback" and "Roll back" as separate
   buttons. CRT06 has 8 bars within 7 units of each other. The model cannot
   rely on visual dominance — it must read every label precisely.

5. **Topology traversal** — SCH06 asks for the specific path that passes
   through a middle node (must enumerate and filter paths). SCH07 asks whether
   a bypass path exists that avoids specified nodes. SCH09 requires checking
   which node is the connectivity odd-one-out among 4 candidates.

---

## Verification table (full run)

```
[PASS] vision.ui_read.sidebar-count           kind=ui_read        derived='5'                            image=11693 B  hash=True
[PASS] vision.ui_read.window-title-cta        kind=ui_read        derived='Export Report'                image=9016 B   hash=True
[PASS] vision.ui_read.dense-sidebar           kind=ui_read        derived='Billing'                      image=17397 B  hash=True
[PASS] vision.ui_read.multi-state-form        kind=ui_read        derived='Confirm Password'             image=18014 B  hash=True
[PASS] vision.ui_read.near-label-buttons      kind=ui_read        derived='Roll back'                    image=14354 B  hash=True
[PASS] vision.ui_read.dashboard-cross-region  kind=ui_read        derived='WARNING'                      image=36962 B  hash=True
[PASS] vision.ui_read.cjk-dense-table         kind=ui_read        derived='32'                           image=32793 B  hash=True
[PASS] vision.schematic.signal-flow           kind=schematic      derived='yes'                          image=12333 B  hash=True
[PASS] vision.schematic.dense-path            kind=schematic      derived='Limiter, Splitter, Recorder, Output' image=15352 B  hash=True
[PASS] vision.schematic.bypass-path           kind=schematic      derived='yes'                          image=15542 B  hash=True
[PASS] vision.schematic.node-degree           kind=schematic      derived='4'                            image=16653 B  hash=True
[PASS] vision.schematic.dual-destination      kind=schematic      derived='TaskD'                        image=16457 B  hash=True
[PASS] vision.schematic.dense-resistor-net    kind=schematic      derived='4'                            image=17075 B  hash=True
[PASS] vision.chart_extract.bar-max           kind=chart_extract  derived='B, 72'                        image=14011 B  hash=True
[PASS] vision.chart_extract.trend-direction   kind=chart_extract  derived='decreasing'                   image=14744 B  hash=True
[PASS] vision.chart_extract.eight-near-bars   kind=chart_extract  derived='48'                           image=14739 B  hash=True
[PASS] vision.chart_extract.smallest-gap      kind=chart_extract  derived='Q5'                           image=19913 B  hash=True
[PASS] vision.chart_extract.three-lines-middle kind=chart_extract derived='Gamma'                         image=23178 B  hash=True
[PASS] vision.chart_extract.exact-double      kind=chart_extract  derived='Beta'                         image=15746 B  hash=True

All 19 items PASS
```

---

## Design notes

- **Answers from generator parameters only.** No hand-typed ground truths.
  `regenerate_verify.py` re-runs every generator and recomputes answers; a
  mismatch between JSON `payload.answer` and freshly-derived answer causes FAIL.

- **Image quality.** All PNGs rendered at 800×600 via PIL with `NotoSansCJK`
  for CJK items and `NimbusSans-Bold`/`NimbusSans-Regular` for English.
  Font sizes ≥12 px; text contrast verified via visual spot-check.

- **Zero model-API calls.** Verification is local, deterministic, pure PIL.

- **Legibility verified.** Spot-checked: UI07 disabled/error fields clearly
  distinct; UI09 sidebar+table cross-region fully readable with color-coded
  statuses; CRT07 grouped bars with all 12 value-labels legible; SCH07 bypass
  edges drawn in blue distinct from forward (black) and feedback (red);
  CRT08 value annotations at x=5 staggered to avoid overlap.

- **Seats mapping**
  - `ui_read` → `multimodal_looker`, `visual_engineering`
  - `schematic` → `multimodal_looker`, `circuit_engineer`
  - `chart_extract` → `multimodal_looker`, `artistry`
