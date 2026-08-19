# B1 Reasoning Bank — Review Notes (v2 hardened)

## What changed and why

Calibration revealed t5/t6 were **too easy**: anchors (deepseek-v4-flash, qwen3.7-plus, glm-5.2) solved t6 at 60–80% instead of the ≤25% target. Every old t5/t6 item was a **single-technique problem** (Prüfer codes, Euler's theorem, ménage formula) — a model that knows the ONE trick gets it instantly. The bank's mission remains breaking a 6-way tie (K3, kimi-k2.7-code, MiniMax-M2.5, glm-5.2, qwen3.6-plus, kimi-for-coding-highspeed all ≈92.3 on the old 13-question test).

v2 replaces 17 of the 20 t5/t6 slots with problems designed to defeat strong reasoners 70–90% of the time via the **hardening principles** from the spec:

- **(a) Multi-stage cascading**: CRT output feeds a totient, then feeds a modular exp; errors compound (t6.mod-cascade, t5.euler-chain).
- **(b) Large asymmetric state spaces**: Pólya on D₁₂ with freq (4,4,4), S₄ on C(4,2) pairs, 5-set inclusion-exactly-2 with irregular overlaps — no clean textbook form to pattern-match.
- **(c) Multi-part / constrained structure**: Burnside on non-trivial groups WITH frequency constraints (cube, hex-burnside, polya-d12) — requires BOTH cycle analysis AND constrained counting.
- **(d) Trap conditions**: non-prime moduli in totient chains, off-by-one boundary conditions in 5-equation Markov systems, partitions (unordered) vs compositions (ordered).
- **(e) Non-obvious absorbing structure**: 5-transient Markov chains where the matrix inverse has a "weird" denominator (727, not a neat 10/100).

## Final tier design

| Tier | Target | Items | Character |
|------|--------|-------|-----------|
| t1 | >90% | 10 | Arithmetical warmups (unchanged) |
| t2 | 70–90% | 10 | Classical discrete math (unchanged) |
| t3 | 50–70% | 10 | Multi-step single technique (unchanged) |
| t4 | 30–50% | 10 | Mixed-method items (unchanged) |
| t5 | 10–30% | 10 | **HARDENED**: 3 kept + 7 new multi-stage items |
| t6 | <10% | 10 | **HARDENED**: all 10 replaced with tie-breakers |

## What was kept vs replaced

### T5 (10 items):

**Kept (3):**
- `reasoning.t5.expected-htth` = 18 — Markov chain, 4 states, non-obvious back-transitions
- `reasoning.t5.crt-extra` = 135 — CRT + quadratic constraint mod 17
- `reasoning.t5.dihedral-bracelets` = 78 — **demoted from t6**; Burnside on D₁₀, still hard-ish for t5

**New (7):**
- `reasoning.t5.hex-burnside-freq` = 16 — Burnside on C₆ with freq (2,2,2) constraint
- `reasoning.t5.surjection-bounded` = 29,400 — surjections with bounded preimages
- `reasoning.t5.constrained-partition` = 32 — **unordered** partitions (not compositions — off-by-one trap)
- `reasoning.t5.markov-expect-ht` = 320 — 4-state Markov, ask for 19·ΣE
- `reasoning.t5.euler-totient-chain` = 64,768 — CRT → factor → totient(x₀²)
- `reasoning.t5.lattice-path` = 667 — DP with rectangular barrier
- `reasoning.t5.stirling-sum` = 73,681 — Σ S(8,k)·k²

### T6 (10 items, all new):

1. `reasoning.t6.crt-then-coprime-count` = 1,763 — CRT → coprime pair count
2. `reasoning.t6.markov-6transient` = 13,148 — 5-equation Markov, ask for 727·ΣE
3. `reasoning.t6.polya-d12-freq3` = 1,493 — Pólya on D₁₂ with freq (4,4,4)
4. `reasoning.t6.ie-5sets-exactly-2` = 112 — 5-set inclusion-exactly-2 with irregular overlaps
5. `reasoning.t6.mod-cascade-3stage` = 130,363,122 — CRT → polynomial → modexp
6. `reasoning.t6.matrix-power-trace` = 606 — Cayley-Hamilton + trace recurrence mod 1009
7. `reasoning.t6.burnside-s4-pairs` = 66 — S₄ on C(4,2) pairs, 3 colors
8. `reasoning.t6.coupon-nonuniform` = 1,339 — non-uniform coupon collector (105·E)
9. `reasoning.t6.cube-burnside-freq` = 6 — cube rotations with freq (2,2,2)
10. `reasoning.t6.derangement-constrained` = 1,965,624 — perms of [10] with first 6 fixed-point-free

### Canary coverage

Two items marked `meta.canary_candidate=true`:
- `reasoning.t5.markov-expect-ht` — 4-state Markov tie-breaker
- `reasoning.t6.polya-d12-freq3` — Pólya on D₁₂, one of the hardest items

---

## Top tie-breaker candidates (most likely to separate the top-6 models)

### Most dangerous (high confidence of separation):

**1. `reasoning.t6.polya-d12-freq3` = 1,493** — Pólya on D₁₂ with freq (4,4,4)
- Requires (a) classifying all 24 D₁₂ elements by cycle type on vertices, (b) computing fixed colorings per type WITH a frequency constraint, (c) summing and dividing. Each step has multiple failure modes; a single wrong cycle type collapses to a wrong answer. The Burnside analysis has identity contribution = 34,650, not the common "90" mistake (that's for n=6, not 12).

**2. `reasoning.t6.markov-6transient` = 13,148** — 5-equation Markov chain
- 5×5 system with denominator 727 (not a clean number). Floating-point arithmetic will silently give the WRONG integer if you round incorrectly. Requires exact rational arithmetic. Even with correct setup, the sum 727·Σt is non-obvious (13,148, not a "round" number).

**3. `reasoning.t6.matrix-power-trace` = 606** — trace(A⁵⁰) mod 1009
- Requires deriving the characteristic polynomial λ³−3λ²+3λ−2=0 via Cayley-Hamilton (det A = 2, not 0!), setting up the trace recurrence t_n = 3t_{n-1} − 3t_{n-2} + 2t_{n-3}, and computing mod 1009. The most common failure: miscounting the characteristic polynomial coefficients.

**4. `reasoning.t6.mod-cascade-3stage` = 130,363,122** — 3-stage computation
- Stage 1: CRT to get x₀=53. Stage 2: a = 53³+53²+1 = 151,687. Stage 3: 2ᵃ mod (10⁹+7). Any single-stage error cascades and gives an entirely different answer. The final number is unguessable.

**5. `reasoning.t6.cube-burnside-freq` = 6**
- Small answer, but the Burnside analysis requires correctly enumerating all 24 cube rotations as face permutations — a notoriously tricky step. The answer 6 is easy to second-guess; models doubt themselves and change their answer.

### Moderately dangerous (will separate many but not all of the top-6):

- `reasoning.t6.derangement-constrained` = 1,965,624 — large IE sum, arithmetic precision
- `reasoning.t6.coupon-nonuniform` = 1,339 — requires 16-state Markov or 15-term inclusion-exclusion
- `reasoning.t6.ie-5sets-exactly-2` = 112 — 5-set IE with irregular S_k values
- `reasoning.t5.surjection-bounded` = 29,400 — 36-term sum over compositions

## Honest assessment

Based on the hardening principles and the nature of each item:

- **t6 items**: I estimate <15% of strong models (deepseek-v4-flash class and above) will clear more than 3 of the 10. The polya-d12, markov-6transient, and matrix-trace items are near-impossible without exact arithmetic AND correct mathematical setup.
- **t5 items**: I estimate 30–50% clear rate for strong models. The kept items (expected-htth, crt-extra, dihedral-bracelets) will still act as tie-breakers. The new items add difficulty through frequency-constrained Burnside and multi-stage cascading.
- **Top-cluster separation**: Given that all 6 models tie at ≈92.3% on the old 13-question test, the new 10 t5+10 t6 items should create a spread of at least 5–8 percentage points. The hardest 5 t6 items (polya-d12, markov-6transient, matrix-trace, mod-cascade, cube-burnside) are the real tie-breakers — any model that clears these while others can't will rise in the ranking.

The key insight: **previous t5/t6 items had single identifiable techniques** that strong models have in their training distribution. The hardened items combine (a) multi-stage cascading, (b) non-obvious constraints (frequency in Burnside, ordering in partitions), and (c) large asymmetric systems (5×5 Markov with denominator 727). A model that only knows the technique misses the cascade; a model that does the cascade misses the constraint; a model that gets both misses the arithmetic.

---

## Verification

```
cd /home/lab/hr2/itemrepo/reasoning
python3 truths_verification.py    # must print 60/60 PASS
```

All 60 rows PASS as of this writing (v2 hardening complete).
