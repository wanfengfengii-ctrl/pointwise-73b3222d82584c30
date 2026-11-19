from dataclasses import dataclass


class InvalidWindow(ValueError):
    """A task requires two increasing integer source ticks."""


@dataclass(frozen=True)
class TimeWindow:
    start: int
    end: int

    def __post_init__(self) -> None:
        if type(self.start) is not int or type(self.end) is not int or self.start >= self.end:
            raise InvalidWindow('window requires integer start < end')

    def contains(self, tick: int) -> bool:
        return self.start <= tick < self.end
