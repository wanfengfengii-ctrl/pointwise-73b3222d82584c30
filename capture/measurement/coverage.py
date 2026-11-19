from dataclasses import dataclass

from .normalize import MeasuredSample


@dataclass(frozen=True)
class SampleCoverage:
    first_tick: int | None
    last_tick: int | None


def measure_coverage(samples: tuple[MeasuredSample, ...]) -> SampleCoverage:
    if not samples:
        return SampleCoverage(None, None)
    ticks = tuple(sample.observation.time for sample in samples)
    return SampleCoverage(min(ticks), max(ticks))
