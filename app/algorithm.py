"""
Eurowings blind-booking destination classifier.

Oracle rule: price returned iff selection contains ≥2 simultaneously available
destinations.

Strategy: adaptive CSP over city triples.
  1. Quick exit — test all cities; no price → ≤1 available → all False.
  2. Query triples in coverage order (most under-covered cities first).
  3. After every CHECK_EVERY queries, run a backtracking solver.
     Stop as soon as exactly one consistent assignment remains.
  4. Return {city: state} where state ∈ {'available','uncertain','unavailable'}.

When the oracle returns contradictory results (e.g. same city appears forced
both True and False by different triples), the CSP has 0 solutions.  We fall
back to direct-evidence voting: look at constraints where both companion cities
have definite known values — those give unambiguous votes about the third city.
Conflicting votes → 'uncertain'.
"""

from __future__ import annotations

from itertools import combinations
from typing import Callable, Awaitable, Optional

MIN_SELECTED = 3
_CHECK_EVERY = 5

# State constants
AVAILABLE   = "available"
UNCERTAIN   = "uncertain"
UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# CSP internals
# ---------------------------------------------------------------------------

def _coverage_order(n: int) -> list[tuple[int, int, int]]:
    """All C(n,3) triples, sorted so under-covered cities appear first."""
    triples = list(combinations(range(n), 3))
    counts = [0] * n
    ordered: list[tuple[int, int, int]] = []
    remaining = list(triples)
    while remaining:
        best = min(range(len(remaining)),
                   key=lambda idx: sum(counts[c] for c in remaining[idx]))
        t = remaining.pop(best)
        ordered.append(t)
        for c in t:
            counts[c] += 1
    return ordered


def _perturbation_order(n: int, seed: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """
    All triples (excluding seed) sorted by overlap with seed descending.
    Triples sharing 2 cities with seed come first — most informative given
    the seed is a confirmed price triple (≥2 available cities inside it).
    Within each overlap tier, coverage order maximises city exposure.
    """
    seed_set = set(seed)
    counts = [0] * n
    tiers: dict[int, list] = {2: [], 1: [], 0: []}
    for t in combinations(range(n), 3):
        overlap = len(set(t) & seed_set)
        if overlap < 3:
            tiers[overlap].append(t)

    result: list[tuple[int, int, int]] = []
    for tier in (2, 1, 0):
        remaining = list(tiers[tier])
        while remaining:
            best = min(range(len(remaining)),
                       key=lambda idx: sum(counts[c] for c in remaining[idx]))
            t = remaining.pop(best)
            result.append(t)
            for c in t:
                counts[c] += 1
    return result


def _solve(
    n: int,
    constraints: list[tuple[tuple[int, int, int], int]],
    max_solutions: int = 2,
    fixed: dict[int, int] | None = None,
) -> list[tuple[int, ...]]:
    """
    Backtracking solver with pruning.
    *fixed* pins specific city indices to 0 or 1 before searching.
    Stops after *max_solutions* found.
    """
    x = [0] * n
    if fixed:
        for k, v in fixed.items():
            x[k] = v
    solutions: list[tuple[int, ...]] = []

    def partial_ok(upto: int) -> bool:
        for (i, j, k), res in constraints:
            if max(i, j, k) > upto:
                continue
            s = x[i] + x[j] + x[k]
            if res == 1 and s < 2:
                return False
            if res == 0 and s > 1:
                return False
        return True

    def backtrack(i: int) -> None:
        if len(solutions) >= max_solutions:
            return
        if i == n:
            solutions.append(tuple(x))
            return
        if fixed and i in fixed:
            if partial_ok(i):
                backtrack(i + 1)
        else:
            for val in (0, 1):
                x[i] = val
                if partial_ok(i):
                    backtrack(i + 1)

    backtrack(0)
    return solutions


def _find_forced(
    n: int,
    constraints: list[tuple[tuple[int, int, int], int]],
    already_known: dict[int, bool],
) -> dict[int, bool]:
    """
    For each city not yet in *already_known*, check whether its value is forced
    by the current constraints.  Returns only newly forced cities (True/False).
    Only called when the constraint set is consistent (≥1 solution exists).
    """
    newly: dict[int, bool] = {}
    for i in range(n):
        if i in already_known:
            continue
        can_be_false = bool(_solve(n, constraints, max_solutions=1, fixed={i: 0}))
        can_be_true  = bool(_solve(n, constraints, max_solutions=1, fixed={i: 1}))
        if not can_be_false and can_be_true:
            newly[i] = True
        elif not can_be_true and can_be_false:
            newly[i] = False
        # both False → contradictory (shouldn't happen if caller checked ≥1 solution)
    return newly


def _find_contradicted(
    n: int,
    constraints: list[tuple[tuple[int, int, int], int]],
    known: dict[int, bool],
) -> set[int]:
    """
    For each city in *known*, check whether direct evidence from other known
    cities contradicts its classification.  Returns indices that should be
    demoted to 'uncertain'.
    Called only when the CSP has 0 solutions (oracle contradictions detected).
    """
    contradicted: set[int] = set()
    for i in known:
        tv = fv = 0
        for triple, result in constraints:
            if i not in triple:
                continue
            others = [c for c in triple if c != i]
            if not all(c in known for c in others):
                continue
            other_sum = sum(1 for c in others if known[c])
            if other_sum == 1:
                if result == 1:
                    tv += 1
                else:
                    fv += 1
        if known[i] and fv > 0:       # classified True but false evidence exists
            contradicted.add(i)
        elif not known[i] and tv > 0: # classified False but true evidence exists
            contradicted.add(i)
    return contradicted


def _direct_classify(
    n: int,
    constraints: list[tuple[tuple[int, int, int], int]],
    known: dict[int, bool],
) -> dict[int, bool | None]:
    """
    Classify unclassified cities using direct-evidence votes from constraints
    where both companion cities have definite known values.

    For a triple (i, j, k) with other_sum = known[j] + known[k]:
      result=1, other_sum=1 → city must be True  (true vote)
      result=0, other_sum=1 → city must be False (false vote)
      other_sum=0 or 2       → constraint gives no info about city

    Returns: True = certain available, False = certain unavailable, None = uncertain.
    Iterates until stable (newly classified cities unlock more inferences).
    """
    newly: dict[int, bool | None] = {}
    working = dict(known)   # only definite (True/False) values; uncertain excluded

    changed = True
    while changed:
        changed = False
        for i in range(n):
            if i in working or i in newly:
                continue

            tv = fv = 0
            for triple, result in constraints:
                if i not in triple:
                    continue
                others = [c for c in triple if c != i]
                if not all(c in working for c in others):
                    continue
                other_sum = sum(1 for c in others if working[c])
                if other_sum == 1:
                    if result == 1:
                        tv += 1
                    else:
                        fv += 1

            if tv > 0 and fv == 0:
                newly[i] = True
                working[i] = True
                changed = True
            elif fv > 0 and tv == 0:
                newly[i] = False
                working[i] = False
                changed = True
            elif tv > 0 and fv > 0:
                newly[i] = None   # uncertain — keep out of working to avoid propagation
                changed = True

    return newly


# ---------------------------------------------------------------------------
# Post-classification verification
# ---------------------------------------------------------------------------

async def _verify_available(
    result: dict[str, str],
    test_fn: Callable[[list[str]], Awaitable[Optional[str]]],
    on_progress: Callable[[str, list[str], bool], Awaitable[None]] | None,
    on_city_result: Callable[[str, str], Awaitable[None]] | None,
    min_selected: int,
) -> dict[str, str]:
    """
    Two-stage verification of AVAILABLE cities.

    Stage 1 — Pairwise (C(k,2) queries):
      Test every pair (A, B) from the available set with unavailable padding.
      A truly available city produces a price with at least one partner.
      Cities with 0 successful pairs → UNCERTAIN.
      Catches false positives when multiple found cities exist.

    Stage 2 — Isolation (k queries):
      Test each found city alone against the FULL unavailable pool.
      Selection: {city} + all unavailable cities.
      Expected: no price (only 1 available city in the selection).
      A price means ≥2 actually-available cities exist in the unavailable pool
      (we missed them) → oracle inconsistency → UNCERTAIN.
      Catches cases like: "all cities without X still give a price" — a hidden
      pair in the unavailable pool is providing the signal.
    """
    available = [c for c, s in result.items() if s == AVAILABLE]
    unavailable_pool = [c for c, s in result.items() if s == UNAVAILABLE]

    if not available:
        return result

    # Stage 1: Pairwise
    if len(available) >= 2:
        successes: dict[str, int] = {c: 0 for c in available}
        for a, b in combinations(available, 2):
            sel = [a, b]
            for p in unavailable_pool:
                if len(sel) >= min_selected:
                    break
                sel.append(p)
            if len(sel) < min_selected:
                continue
            price = await test_fn(sel)
            if on_progress:
                await on_progress(f"verify {a}+{b}", sel, price is not None)
            if price is not None:
                successes[a] += 1
                successes[b] += 1

        for city, count in successes.items():
            if count == 0:
                result[city] = UNCERTAIN
                if on_city_result:
                    await on_city_result(city, UNCERTAIN)

    # Stage 2: Isolation — each found city alone against the full unavailable pool
    for city in available:
        if result.get(city) != AVAILABLE:
            continue   # already downgraded
        sel = [city] + unavailable_pool
        if len(sel) < min_selected:
            continue
        price = await test_fn(sel)
        if on_progress:
            await on_progress(f"isolate {city}", sel, price is not None)
        if price is not None:
            result[city] = UNCERTAIN
            if on_city_result:
                await on_city_result(city, UNCERTAIN)

    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def classify_destinations(
    cities: list[str],
    test_fn: Callable[[list[str]], Awaitable[Optional[str]]],
    on_progress: Callable[[str, list[str], bool], Awaitable[None]] | None = None,
    on_city_result: Callable[[str, str], Awaitable[None]] | None = None,
    min_selected: int = MIN_SELECTED,
) -> dict[str, str]:
    """
    Classify all *cities* as 'available', 'uncertain', or 'unavailable'.

    Emits intermediate city results via *on_city_result(city_name, state)* as
    soon as a city's state can be determined from accumulated constraints.
    """
    n = len(cities)
    if n < min_selected:
        return {c: UNAVAILABLE for c in cities}

    if await test_fn(list(cities)) is None:
        return {c: UNAVAILABLE for c in cities}

    constraints: list[tuple[tuple[int, int, int], int]] = []
    queried: set[tuple[int, int, int]] = set()
    known: dict[int, bool] = {}   # definite index → bool, already emitted
    emitted: set[int] = set()     # all indices emitted (including uncertain)
    seed: tuple[int, int, int] | None = None

    async def _emit(i: int, state: str) -> None:
        emitted.add(i)
        if on_city_result:
            await on_city_result(cities[i], state)

    async def _checkpoint() -> dict[str, str] | None:
        """Run solver; emit forced/uncertain cities; return final result if unique."""
        sols = _solve(n, constraints, max_solutions=2)

        if len(sols) == 1:
            solution = sols[0]
            result = {cities[i]: (AVAILABLE if solution[i] else UNAVAILABLE)
                      for i in range(n)}
            for i, val in enumerate(solution):
                if i not in emitted:
                    await _emit(i, AVAILABLE if val else UNAVAILABLE)
            return await _verify_available(
                result, test_fn, on_progress, on_city_result, min_selected
            )

        if len(sols) >= 2:
            # Consistent but ambiguous — emit any cities now provably forced
            newly = _find_forced(n, constraints, known)
            for i, val in newly.items():
                known[i] = val
                await _emit(i, AVAILABLE if val else UNAVAILABLE)
        else:
            # 0 solutions: oracle returned contradictory results.
            # Step 1 — demote any already-classified city whose classification
            # is now contradicted by direct evidence from other known cities.
            contradicted = _find_contradicted(n, constraints, known)
            for i in contradicted:
                del known[i]
                await _emit(i, UNCERTAIN)   # re-emit; frontend updates the chip

            # Step 2 — classify still-unknown cities via direct-evidence voting.
            newly_dc = _direct_classify(n, constraints, known)
            for i, val in newly_dc.items():
                if i in emitted:
                    continue
                if val is True:
                    known[i] = True
                    await _emit(i, AVAILABLE)
                elif val is False:
                    known[i] = False
                    await _emit(i, UNAVAILABLE)
                else:
                    await _emit(i, UNCERTAIN)

        return None

    # Phase 1: coverage order until first price triple found
    for triple in _coverage_order(n):
        selected = [cities[i] for i in triple]
        result = await test_fn(selected)
        r = 1 if result is not None else 0
        if on_progress:
            await on_progress(', '.join(selected), selected, r == 1)
        constraints.append((triple, r))
        queried.add(triple)

        if r == 1:
            seed = triple
            final = await _checkpoint()
            if final is not None:
                return final
            break

    if seed is None:
        return {c: UNAVAILABLE for c in cities}

    # Phase 2: perturbation order around the seed
    step = 0
    for triple in _perturbation_order(n, seed):
        if triple in queried:
            continue
        if len(emitted) == n:          # all cities classified — stop early
            break
        selected = [cities[i] for i in triple]
        result = await test_fn(selected)
        r = 1 if result is not None else 0
        if on_progress:
            await on_progress(', '.join(selected), selected, r == 1)
        constraints.append((triple, r))
        queried.add(triple)
        step += 1

        if step % _CHECK_EVERY == 0:
            final = await _checkpoint()
            if final is not None:
                return final
            if len(emitted) == n:
                break

    # Exhausted or all cities emitted — do one last pass then return
    final_checkpoint = await _checkpoint()
    if final_checkpoint is not None:
        return final_checkpoint

    # Build result from emitted states; anything still unknown → unavailable
    result_map: dict[str, str] = {}
    for i, city in enumerate(cities):
        if i in known:
            result_map[city] = AVAILABLE if known[i] else UNAVAILABLE
        elif i in emitted:
            result_map[city] = UNCERTAIN
        else:
            result_map[city] = UNAVAILABLE
    return await _verify_available(
        result_map, test_fn, on_progress, on_city_result, min_selected
    )
