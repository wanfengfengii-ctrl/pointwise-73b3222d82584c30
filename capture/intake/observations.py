from dataclasses import dataclass


@dataclass(frozen=True)
class Observation:
    time: int
    channel: str
    seq: int
    value: int
    line: int


@dataclass(frozen=True)
class ObservedRows:
    rows: tuple[Observation, ...]

    def is_empty(self) -> bool:
        return not self.rows
