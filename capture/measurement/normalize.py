from dataclasses import dataclass

from capture.intake.observations import Observation, ObservedRows
from capture.tasks.snapshot import TaskSnapshot


@dataclass(frozen=True)
class MeasuredSample:
    observation: Observation
    micrometres: int


def normalize_samples(task: TaskSnapshot, rows: ObservedRows) -> tuple[MeasuredSample, ...]:
    return tuple(MeasuredSample(row, row.value * task.multiplier_um) for row in rows.rows)
