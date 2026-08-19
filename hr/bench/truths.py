"""Runtime-computed ground truths for the reasoning and long_horizon benches.

Both truth sets are pure CPU, computed lazily on FIRST USE (never at import):
- :func:`reasoning_truths` — 13 math/number-theory answers; the *result* of
  each fixed formula/loop/sieve is the truth (no hardcoded answer literals,
  so the scorer cannot drift from a hand-verified value).
- :func:`long_horizon_truths` — Critical Path Method over the 6-task project
  graph (forward + backward passes). Formerly an import-time module constant
  (v1 benchmark.py:349-423); made lazy so importing the bench package never
  pays for the computation and never touches the DB.
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def reasoning_truths() -> dict[int, int]:
    """Ground truths for the 13 reasoning problems, computed in-process (lazy)."""
    truths: dict[int, int] = {}

    # Q1: count n in 1..1_000_000 with n≡1 (mod 3), n≡2 (mod 5), n≡3 (mod 7), n%11 != 0.
    truths[1] = sum(
        1 for n in range(1, 1_000_001)
        if (n % 3 == 1) and (n % 5 == 2) and (n % 7 == 3) and (n % 11 != 0)
    )

    # Q2: sum of decimal digits of 7^777.
    truths[2] = sum(int(d) for d in str(7 ** 777))

    # Q3: number of positive divisors of 10! (prime-factorize factorial(10)).
    def _count_divisors(num: int) -> int:
        n = num
        result = 1
        d = 2
        while d * d <= n:
            if n % d == 0:
                exp = 0
                while n % d == 0:
                    exp += 1
                    n //= d
                result *= (exp + 1)
            d += 1
        if n > 1:
            result *= 2
        return result
    truths[3] = _count_divisors(math.factorial(10))

    # Q4: 1000th prime via a sieve of adequate upper bound.
    def _nth_prime(n: int) -> int:
        if n < 6:
            bound = 15
        else:
            ln_n = math.log(n)
            bound = int(n * (ln_n + math.log(ln_n))) + 16
        sieve = bytearray(b"\x01") * (bound + 1)
        sieve[0] = sieve[1] = 0
        for i in range(2, int(bound ** 0.5) + 1):
            if sieve[i]:
                for j in range(i * i, bound + 1, i):
                    sieve[j] = 0
        count = 0
        for i in range(2, bound + 1):
            if sieve[i]:
                count += 1
                if count == n:
                    return i
        raise RuntimeError("sieve bound too small")
    truths[4] = _nth_prime(1000)

    # Q5: 10th Catalan number C_10 = C(20, 10) // 11.
    truths[5] = math.comb(20, 10) // 11

    # Q6: last three digits of 13^500.
    truths[6] = pow(13, 500, 1000)

    # Q7: integers in [1, 10000] that are a perfect square or a perfect cube.
    squares = {x * x for x in range(1, 101)}          # sqrt(10000) = 100
    cubes = {x * x * x for x in range(1, 22)}         # 21^3 = 9261 < 10000, 22^3 = 10648
    truths[7] = len(squares | cubes)

    # Q8: onto functions from a 4-element set to a 3-element set
    #     (inclusion-exclusion over the codomain).
    def _surjections(n: int, k: int) -> int:
        total = 0
        for i in range(k + 1):
            total += ((-1) ** i) * math.comb(k, i) * ((k - i) ** n)
        return total
    truths[8] = _surjections(4, 3)

    # Q9: trailing zeros of 200! (Legendre for p=5).
    def _trailing_zeros_factorial(k: int) -> int:
        count = 0
        p = 5
        while p <= k:
            count += k // p
            p *= 5
        return count
    truths[9] = _trailing_zeros_factorial(200)

    # Q10: derangements of 7 elements via the subfactorial recurrence.
    def _derangements(n: int) -> int:
        d_prev2, d_prev1 = 1, 0  # D(0)=1, D(1)=0
        for i in range(2, n + 1):
            d_prev2, d_prev1 = d_prev1, (i - 1) * (d_prev1 + d_prev2)
        return d_prev1 if n >= 1 else d_prev2
    truths[10] = _derangements(7)

    # Q11: smallest n with exactly 100 positive divisors.
    #      Uses d(n) = (e1+1)(e2+1)... where n = p1^e1 * p2^e2 * ...
    #      Minimizing n over all ways to factor 100 into an ordered product
    #      of integers >= 2, assigning the largest exponents to the smallest
    #      primes. The optimal factorization is 100 = 2 * 2 * 5 * 5 giving
    #      exponents (4, 4, 1, 1) on primes (2, 3, 5, 7): n = 2^4 * 3^4 * 5 * 7.
    def _smallest_n_with_d(target: int, primes: list[int] | None = None) -> int:
        if primes is None:
            # Generate enough primes.
            bound = 200
            sieve = bytearray(b"\x01") * (bound + 1)
            sieve[0] = sieve[1] = 0
            for i in range(2, int(bound ** 0.5) + 1):
                if sieve[i]:
                    for j in range(i * i, bound + 1, i):
                        sieve[j] = 0
            primes = [i for i in range(2, bound + 1) if sieve[i]]

        def _factorizations(n: int, max_parts: int) -> list[list[int]]:
            # All factorizations of n into factors >= 2, in non-increasing order.
            if n == 1:
                return [[]]
            if max_parts == 0:
                return []
            out: list[list[int]] = []
            # Single factor: just n.
            out.append([n])
            # Split as f * rest with f >= 2, rest >= f (non-increasing).
            for f in range(2, int(n ** 0.5) + 1):
                if n % f == 0:
                    for rest in _factorizations(n // f, max_parts - 1):
                        if not rest or rest[0] >= f:
                            out.append([f] + rest)
            return out

        best = None
        for fcts in _factorizations(target, len(primes)):
            exps = sorted([f - 1 for f in fcts], reverse=True)
            if len(exps) > len(primes):
                continue
            n_val = 1
            for e, p in zip(exps, primes):
                n_val *= p ** e
            if best is None or n_val < best:
                best = n_val
        return best
    truths[11] = _smallest_n_with_d(100)

    # Q12: number of subsets of {1..20} with sum divisible by 5.
    #      DP over subset-sum modulo k.
    def _subsets_div_k(n: int, k: int) -> int:
        dp = [0] * k
        dp[0] = 1
        for j in range(1, n + 1):
            new = dp[:]
            for s in range(k):
                new[(s + j) % k] += dp[s]
            dp = new
        return dp[0]
    truths[12] = _subsets_div_k(20, 5)

    # Q13: sum of all prime factors (with multiplicity) of 100!.
    #      For each prime p <= 100, v_p(100!) = floor(100/p) + floor(100/p^2) + ...
    #      Contribution: p * v_p(100!). Total is the sum.
    def _sum_prime_factors_of_factorial(n: int) -> int:
        def _vp(m: int, p: int) -> int:
            s, pk = 0, p
            while pk <= m:
                s += m // pk
                pk *= p
            return s
        total = 0
        for p in range(2, n + 1):
            if all(p % q for q in range(2, int(p ** 0.5) + 1)):
                total += p * _vp(n, p)
        return total
    truths[13] = _sum_prime_factors_of_factorial(100)

    return truths


def _compute_long_horizon_truths() -> dict[str, Any]:
    """CPM ground truth for the project-planning benchmark.

    Forward pass computes earliest start/finish per task + project end.
    Backward pass computes latest start/finish per task.
    Slack = latest_start - earliest_start. Critical tasks have slack == 0.
    """
    # (duration_in_days, list_of_predecessors) — topological order A..F.
    tasks: dict[str, tuple[int, list[str]]] = {
        "A": (3, []),
        "B": (5, ["A"]),
        "C": (2, ["A"]),
        "D": (4, ["B"]),
        "E": (6, ["B", "C"]),
        "F": (1, ["D", "E"]),
    }
    ordering = list(tasks.keys())

    # Forward pass: earliest start (es) and earliest finish (ef).
    es: dict[str, int] = {}
    ef: dict[str, int] = {}
    for t in ordering:
        dur, preds = tasks[t]
        es[t] = max((ef[p] for p in preds), default=0)
        ef[t] = es[t] + dur
    project_end = max(ef.values())

    # Build successor map for the backward pass.
    successors: dict[str, list[str]] = {t: [] for t in ordering}
    for t, (_, preds) in tasks.items():
        for p in preds:
            successors[p].append(t)

    # Backward pass: latest finish (lf) and latest start (ls).
    lf: dict[str, int] = {}
    ls: dict[str, int] = {}
    for t in reversed(ordering):
        dur = tasks[t][0]
        succs = successors[t]
        lf[t] = project_end if not succs else min(ls[s] for s in succs)
        ls[t] = lf[t] - dur

    slack = {t: ls[t] - es[t] for t in ordering}

    # Critical tasks (zero slack) in topological order.
    critical_tasks = [t for t in ordering if slack[t] == 0]
    non_critical_slack = {t: slack[t] for t in ordering if slack[t] > 0}

    # Fast-track threshold: if the project exceeds this, propose reducing one
    # critical task by 2 days. Pick the longest-duration critical task that
    # has duration >= 2 (so the reduction is feasible); fall back to any.
    duration_threshold = 15
    fast_track_reduction = 2
    action_task: str | None = None
    action_new_duration: int | None = None
    if project_end > duration_threshold:
        candidates = [t for t in critical_tasks if tasks[t][0] >= fast_track_reduction]
        if not candidates:
            candidates = critical_tasks
        if candidates:
            action_task = max(candidates, key=lambda t: tasks[t][0])
            action_new_duration = project_end - fast_track_reduction

    return {
        "critical_path": critical_tasks,
        "duration": project_end,
        "non_critical_slack": non_critical_slack,
        "duration_threshold": duration_threshold,
        "fast_track_reduction": fast_track_reduction,
        "action_task": action_task,
        "action_new_duration": action_new_duration,
    }


#: Manual lazy cache (module attribute, so tests can reset/starve it).
_LONG_HORIZON_CACHE: dict[str, Any] | None = None


def long_horizon_truths() -> dict[str, Any]:
    """Lazy accessor: the CPM graph is computed on FIRST call, not at import."""
    global _LONG_HORIZON_CACHE
    if _LONG_HORIZON_CACHE is None:
        _LONG_HORIZON_CACHE = _compute_long_horizon_truths()
    return _LONG_HORIZON_CACHE


__all__ = ["long_horizon_truths", "reasoning_truths"]