from dataclasses import dataclass

from capture.intake.observations import ObservedRows
from capture.tasks.snapshot import TaskSnapshot

from .aggregate import ExactStatistics, aggregate_samples
from .coverage import SampleCoverage, measure_coverage
from .normalize import MeasuredSample, normalize_samples


@dataclass(frozen=True)
class MeasurementReport:
    statistics: ExactStatistics
    coverage: SampleCoverage
    samples: tuple[MeasuredSample, ...]


def build_report(task: TaskSnapshot, rows: ObservedRows) -> MeasurementReport:
    samples = () if rows.is_empty() else normalize_samples(task, rows)
    return MeasurementReport(aggregate_samples(samples), measure_coverage(samples), samples)
