from dataclasses import dataclass
from fractions import Fraction

from .normalize import MeasuredSample


@dataclass(frozen=True)
class ExactStatistics:
    count: int
    min_um: int | None
    max_um: int | None
    mean_um: Fraction | None


def aggregate_samples(samples: tuple[MeasuredSample, ...]) -> ExactStatistics:
    if not samples:
        return ExactStatistics(0, None, None, None)
    values = tuple(sample.micrometres for sample in samples)
    count = len(values)
    return ExactStatistics(count, min(values), max(values), Fraction(sum(values), count))
