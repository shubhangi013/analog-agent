import math

import pytest

from agent.sim import Candidate
from agent.tools import candidate_key, candidate_ranges, make_candidates


@pytest.fixture
def base() -> Candidate:
    return Candidate(W_in=10, L_in=1, W_load=5, L_load=1, W_tail=5, L_tail=1, I_bias=25)


def test_ranges_come_from_candidate() -> None:
    assert candidate_ranges() == {
        "W_in": (1, 100), "L_in": (0.18, 2), "W_load": (1, 100), "L_load": (0.18, 2),
        "W_tail": (1, 100), "L_tail": (0.18, 2), "I_bias": (5, 200),
    }


def test_make_candidates_is_deterministic_distinct_and_new(base: Candidate) -> None:
    first = make_candidates(base, 6, 0.5, seed=3)
    assert first == make_candidates(base, 6, 0.5, seed=3)
    keys = {candidate_key(item) for item in first}
    assert len(keys) == 6 and candidate_key(base) not in keys
    for candidate in first:
        for name, value in candidate.model_dump().items():
            ratio = value / getattr(base, name)
            assert 1 / 1.5 - 0.02 <= ratio <= 1.5 + 0.02


def test_make_candidates_clips_to_ranges_and_excludes() -> None:
    corner = Candidate(W_in=100, L_in=2, W_load=1, L_load=0.18, W_tail=100, L_tail=2, I_bias=200)
    tried = make_candidates(corner, 6, 1.0, seed=1)
    fresh = make_candidates(corner, 6, 1.0, seed=1, exclude=tried)
    assert not {candidate_key(c) for c in fresh} & {candidate_key(c) for c in tried}
    for candidate in tried + fresh:
        Candidate.model_validate(candidate.model_dump())


@pytest.mark.parametrize("n,spread", [(0, 0.5), (6, 0), (6, math.nan), (6, -1)])
def test_invalid_arguments(base: Candidate, n: int, spread: float) -> None:
    with pytest.raises(ValueError):
        make_candidates(base, n, spread)
