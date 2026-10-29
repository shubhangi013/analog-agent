"""Small nearest-neighbour predictor; estimates rank work, never verify it."""

import math

from agent.sim import Candidate, Metrics
from agent.tools import candidate_ranges
from agent.verifier import Targets


def predict(candidate: Candidate, observations: list[tuple[Candidate, Metrics]]) -> Metrics | None:
    if len(observations) < 3:
        return None
    ranges = candidate_ranges()

    def distance(other: Candidate) -> float:
        return sum(
            ((math.log(getattr(candidate, name)) - math.log(getattr(other, name)))
             / math.log(high / low)) ** 2
            for name, (low, high) in ranges.items()
        )

    neighbours = sorted(observations, key=lambda row: distance(row[0]))[:3]
    weights = [1 / max(distance(other), 1e-12) for other, _ in neighbours]
    return Metrics(**{
        name: sum(weight * getattr(metrics, name)
                  for weight, (_, metrics) in zip(weights, neighbours)) / sum(weights)
        for name in Metrics.model_fields
    })


def prediction_error(prediction: Metrics, measured: Metrics, targets: Targets) -> dict[str, float]:
    return {name: abs(getattr(prediction, name) - getattr(measured, name)) / target
            for name, target in targets.model_dump().items()}
