from capture.tasks.snapshot import TaskSnapshot

from .observations import Observation, ObservedRows


class ObservationEligibilityError(ValueError):
    """At least one observation is outside the task or repeats a sequence."""


def validate_observations(task: TaskSnapshot, rows: tuple[Observation, ...]) -> ObservedRows:
    seen: set[int] = set()
    for row in rows:
        if row.channel != task.channel:
            raise ObservationEligibilityError(f'line {row.line}: channel differs from task')
        if not task.window.contains(row.time):
            raise ObservationEligibilityError(f'line {row.line}: time outside task window')
        if row.seq in seen:
            raise ObservationEligibilityError(f'line {row.line}: duplicate sequence')
        seen.add(row.seq)
    return ObservedRows(rows)
