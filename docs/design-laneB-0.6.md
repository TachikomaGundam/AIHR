# Design lane B (0.6 candidates) — close the last two picture gaps
Owner sign-off required per item. Prepared 2026-10-07 after 0.5.1 field close.

## B1  vision capability by probe (config no longer gates alone)
Today: supports_vision comes from the user's provider config (opencode.jsonc
overlay). Clean installs therefore skip vision although the endpoint can SEE
(proven twice on 191: the model described the test image correctly, score 85).

Design: extend the dialect contract probe with one tiny call — attach the
existing 1×1 test PNG, ask "one word: dominant color?", 20 max_tokens.
- probe sees images (200 + non-empty answer) => capability=probe-granted
- 400 / empty => keep config verdict
- config says true but probe fails => keep true (config wins; user may have
  a reason), with a ⚠ line (mirror of starved-knobs visibility)
Battery provenance records WHICH source fed the capability (config|probe)
inside the contract row — honest audit trail, no silent elevation.
Cost: +1 request per model per contract lifetime. Risk: image MIME quirks on
exotic gateways => failure modes collapse to today's behavior (skip), never
a false score.

## B2  factuality battery (feeds the starved coverage knob)
Open-domain truthfulness is a corpus minefield (language/culture/version
bias). Design instead: **context-grounded factuality**, self-contained items,
2 per item card × 8 cards = 16 calls, ~10 min:

Each item = a 2-3 line invented fact sheet (names/numbers, unambiguous) +
two questions:
  Q_ans    answerable from the sheet verbatim (key fact = a number/name)
  Q_unans  deliberately NOT in the sheet; correct behavior = refusal/
           "not stated"; any concrete fabricated value = hallucination
Scoring (zero-LLM, regex+containment, anti-gaming by design):
  item_score = 100 * (hit(Q_ans) AND clean(Q_unans))
  always-refuse model  -> fails every Q_ans   (no free pass)
  always-hallucinate   -> fails every Q_unans
battery score = balanced mean over 8 items; half_width threshold in
thresholds.yaml like the rest; knob coverage -> livebench_factuality makes
the starved-knobs banner empty on fresh installs (visible closure).
Item cards live in-repo (deterministic, seeded variants for repetition), no
external corpus, no licensing exposure, bilingual-ready (zh/en card sets).

## Cost & surface
B1: contract schema +1 fact, probe +1 call, engine gate reads union.
B2: +1 BenchmarkCategory, prompts+scorer+registration, thresholds entry,
knob default flip, ~10 tests. Both ship 0.6.0 (feature train), 0.5.x stays
patch-only. Neither touches scoring honesty invariants (no-score-on-doubt
rule holds for both).

## Addendum 2026-10-08 (v4 grant fix, ships 0.6.1)
Round-7 field truth on 191: qwen3.8-flash-next answered the B1 probe with
`content: null` and the correct image description in `reasoning` — the v3
content-only grant SKIPped a proven-seeing endpoint (manual reproduction:
HTTP 200, "four colored squares (red, …)"). Grant widened to any non-empty
answer channel, status-first refusal unchanged; PROBE_VERSION 3→4 retires
v3 rows for one re-probe per model. B2 shipped green: 8/8 truly graded,
starved-knob line gone from verdict.
