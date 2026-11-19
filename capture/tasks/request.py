from dataclasses import dataclass

from .window import TimeWindow


@dataclass(frozen=True)
class OpenTaskRequest:
    task_id: str
    station_code: str
    channel: str
    window: TimeWindow

    def __post_init__(self) -> None:
        for name in ('task_id', 'station_code', 'channel'):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f'{name} must be nonempty text')
        if not isinstance(self.window, TimeWindow):
            raise TypeError('window must be a TimeWindow')

    def identity(self) -> tuple[str, str, int, int]:
        return (self.station_code, self.channel, self.window.start, self.window.end)
