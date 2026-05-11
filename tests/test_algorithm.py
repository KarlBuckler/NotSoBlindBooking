"""
Tests for classify_destinations() using a faithful Eurowings simulator.

Confirmed Eurowings rule
------------------------
A price is returned iff the selection contains ≥2 simultaneously available
destinations.  One available city padded with empty cities → no price.

EurowingsSimulator
------------------
Implements this rule exactly.  Also asserts MIN_SELECTED is never violated
and records every call for efficiency analysis.
"""

import asyncio
import pytest
from app.algorithm import classify_destinations, AVAILABLE, UNAVAILABLE, MIN_SELECTED


# ---------------------------------------------------------------------------
# Eurowings simulator
# ---------------------------------------------------------------------------

class EurowingsSimulator:
    """
    Simulates the Eurowings blind-booking pricing oracle.

    price(selection) = "price" iff |selection ∩ available| >= min_avail
                       None   otherwise

    Default min_avail=2 matches the confirmed Eurowings behaviour.
    """

    def __init__(self, available: set[str], min_avail: int = 2):
        self.available  = available
        self.min_avail  = min_avail
        self.call_count = 0
        self.call_log: list[list[str]] = []

    async def test_fn(self, selected: list[str]) -> str | None:
        assert len(selected) >= MIN_SELECTED, (
            f"MIN_SELECTED violated: {len(selected)} cities sent: {selected}"
        )
        self.call_count += 1
        self.call_log.append(list(selected))
        n_avail = sum(1 for c in selected if c in self.available)
        return "price" if n_avail >= self.min_avail else None

    def reset(self):
        self.call_count = 0
        self.call_log.clear()


def sim(available: set[str]) -> EurowingsSimulator:
    return EurowingsSimulator(available)


def run(cities: list[str], available: set[str]) -> tuple[dict[str, str], EurowingsSimulator]:
    s = sim(available)
    results = asyncio.run(classify_destinations(cities, s.test_fn))
    return results, s


def assert_correct(results: dict[str, str], cities: list[str], available: set[str]):
    """With a consistent oracle, expect no 'uncertain' — only available/unavailable."""
    for city in cities:
        expected = AVAILABLE if city in available else UNAVAILABLE
        assert results.get(city) == expected, (
            f"{city}: expected {expected!r}, got {results.get(city)!r}"
        )


# ---------------------------------------------------------------------------
# Tests: zero / one available (undetectable)
# ---------------------------------------------------------------------------

def test_zero_available():
    """No available cities → full set has no price → all unavailable."""
    cities = list("ABCDEFGHIJ")
    results, s = run(cities, set())
    assert all(v == UNAVAILABLE for v in results.values())
    assert s.call_count == 1   # just the initial check


def test_one_available_undetectable():
    """
    With ≥2-avail rule, a single available city never produces a price.
    The algorithm should detect this early and return all unavailable.
    """
    cities = list("ABCDEFGHIJ")
    results, s = run(cities, {"E"})
    assert all(v == UNAVAILABLE for v in results.values()), (
        "Single available city cannot be detected under the ≥2-avail rule"
    )
    assert s.call_count == 1   # initial check returns no price → done


# ---------------------------------------------------------------------------
# Tests: two available (minimum detectable case)
# ---------------------------------------------------------------------------

def test_two_available_adjacent():
    """Two adjacent available cities are both found."""
    cities = list("ABCDEFGHIJ")
    results, s = run(cities, {"D", "E"})
    assert_correct(results, cities, {"D", "E"})
    print(f"\n  calls: {s.call_count}")


def test_two_available_far_apart():
    """Two available cities at opposite ends of the list."""
    cities = list("ABCDEFGHIJ")
    results, s = run(cities, {"A", "J"})
    assert_correct(results, cities, {"A", "J"})
    print(f"\n  calls: {s.call_count}")


def test_manchester_edinburgh_scenario():
    """
    Reproduces the Stadtabenteuer situation: 23 cities, only Manchester and
    Edinburgh are available — and only show a price when BOTH are in the selection.
    """
    cities = [
        "Hamburg", "Barcelona", "Berlin", "Bremen", "Rom", "London",
        "Lissabon", "Valencia", "Porto", "Venedig", "Budapest", "Bukarest",
        "Wien", "Zagreb", "Malaga", "Bari", "Stockholm", "Neapel",
        "Bilbao", "Edinburgh", "Manchester", "Dublin", "Mailand",
    ]
    available = {"Manchester", "Edinburgh"}
    results, s = run(cities, available)
    assert_correct(results, cities, available)
    assert results["Manchester"] == AVAILABLE, "Manchester must be available"
    assert results["Edinburgh"] == AVAILABLE,  "Edinburgh must be available"
    print(f"\n  calls: {s.call_count} for {len(cities)} cities, 2 available")


# ---------------------------------------------------------------------------
# Tests: three or more available
# ---------------------------------------------------------------------------

def test_three_available():
    cities = list("ABCDEFGHIJKLMNOPQRST")   # 20 cities
    available = {"C", "K", "R"}
    results, s = run(cities, available)
    assert_correct(results, cities, available)
    print(f"\n  calls: {s.call_count}")


def test_many_available():
    cities = list("ABCDEFGHIJKLMNOPQRST")
    available = {"B", "F", "J", "N", "R"}
    results, s = run(cities, available)
    assert_correct(results, cities, available)
    print(f"\n  calls: {s.call_count}")


def test_large_set_two_available():
    """Stress: 30 cities, 2 available at scattered positions."""
    cities = [f"C{i:02d}" for i in range(30)]
    available = {"C05", "C24"}
    results, s = run(cities, available)
    assert_correct(results, cities, available)
    print(f"\n  calls: {s.call_count} for 30 cities")


# ---------------------------------------------------------------------------
# Tests: no false positives
# ---------------------------------------------------------------------------

def test_no_false_positives():
    """Empty cities must never be marked available."""
    cities = list("ABCDEFGHIJ")
    results, s = run(cities, {"C", "H"})
    for c in cities:
        if c not in {"C", "H"}:
            assert results[c] != AVAILABLE, f"{c} falsely marked available"


def test_min_selected_never_violated():
    """Every test_fn call must receive ≥ MIN_SELECTED cities."""
    cities = list("ABCDEFGHIJKLMNOPQRST")
    s = sim({"D", "N"})
    asyncio.run(classify_destinations(cities, s.test_fn))
    for call in s.call_log:
        assert len(call) >= MIN_SELECTED, f"MIN_SELECTED violated: {call}"


# ---------------------------------------------------------------------------
# Tests: verification step catches false positives
# ---------------------------------------------------------------------------

class NoisySimulator(EurowingsSimulator):
    """
    Wraps EurowingsSimulator with an injection hook: specific triples can be
    forced to return a price regardless of what cities are actually available.
    This mimics the oracle inconsistency that produces false positives.
    """
    def __init__(self, available: set[str], forced_price_triples: list[frozenset]):
        super().__init__(available)
        self.forced = [frozenset(t) for t in forced_price_triples]

    async def test_fn(self, selected: list[str]) -> str | None:
        if frozenset(selected) in self.forced or frozenset(selected[:3]) in self.forced:
            self.call_count += 1
            self.call_log.append(list(selected))
            return "price"
        return await super().test_fn(selected)


def test_verification_catches_false_positive_2_cities():
    """
    Oracle inconsistency makes Manchester look available alongside Hamburg,
    but the pairwise test exposes it: Hamburg+Manchester → no price.
    Both are downgraded to uncertain (Hamburg's true partner is undetected).
    """
    cities = [
        "Hamburg", "Barcelona", "Berlin", "Bremen", "Manchester",
        "London", "Paris", "Wien", "Prag", "Budapest",
    ]
    truly_available = {"Hamburg"}   # only Hamburg; Manchester is a false positive

    s = NoisySimulator(
        available=truly_available,
        forced_price_triples=[frozenset(["Hamburg", "Manchester", "Berlin"])],
    )
    results = asyncio.run(classify_destinations(cities, s.test_fn))

    assert results["Manchester"] != AVAILABLE, "Manchester must not be marked available"
    assert results["Hamburg"]    != AVAILABLE, "Hamburg cannot be confirmed without its true partner"


def test_isolation_catches_hidden_available_in_unavailable_pool():
    """
    Reproduces the 514d0a29 scenario: Hamburg and Manchester are classified as
    available via noisy oracle, but two cities in the 'unavailable' pool
    (Edinburgh, Dublin) are actually available.

    The isolation test fires: {Hamburg, all_unavailable} → Edinburgh+Dublin make
    a price → Hamburg flagged uncertain. Same for Manchester.
    """
    cities = [
        "Hamburg", "Barcelona", "Berlin", "Manchester",
        "Edinburgh", "Dublin", "Wien", "Prag",
    ]
    truly_available = {"Edinburgh", "Dublin"}   # hidden in what the algorithm marks unavailable

    s = NoisySimulator(
        available=truly_available,
        forced_price_triples=[frozenset(["Hamburg", "Manchester", "Berlin"])],
    )
    results = asyncio.run(classify_destinations(cities, s.test_fn))

    assert results["Hamburg"]    != AVAILABLE, "Hamburg must not stay available"
    assert results["Manchester"] != AVAILABLE, "Manchester must not stay available"


def test_verification_keeps_genuine_pair():
    """
    When two cities are genuinely available, the pairwise verification keeps both.
    """
    cities = list("ABCDEFGHIJ")
    results, _ = run(cities, {"C", "G"})
    assert results["C"] == AVAILABLE
    assert results["G"] == AVAILABLE


def test_verification_three_available_one_false_positive():
    """
    3 claimed available, 1 is a false positive.
    The FP pairs with each of the 2 truly available cities → no price.
    The genuine pair → price.
    Verification should demote only the FP to uncertain.
    """
    cities = list("ABCDEFGHIJKLMNOPQRST")
    truly_available = {"C", "K"}
    false_positive  = "R"

    s = NoisySimulator(
        available=truly_available,
        forced_price_triples=[
            frozenset(["C", "K", "R"]),   # noisy triple that inflated R to available
        ],
    )
    results = asyncio.run(classify_destinations(cities, s.test_fn))

    assert results[false_positive] != AVAILABLE, f"{false_positive} should not be available"
    assert results["C"] == AVAILABLE, "C should remain available"
    assert results["K"] == AVAILABLE, "K should remain available"


if __name__ == "__main__":
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "pytest", __file__, "-v", "-s"], check=False)
