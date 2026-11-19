from dataclasses import dataclass

from capture.stations.definition import ChannelDefinition

from .request import OpenTaskRequest
from .window import TimeWindow


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    station_code: str
    channel: str
    multiplier_um: int
    window: TimeWindow

    def request_identity(self) -> tuple[str, str, int, int]:
        return (self.station_code, self.channel, self.window.start, self.window.end)

    def to_storage(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.task_id,
            self.station_code,
            self.channel,
            format(self.multiplier_um, 'x'),
            format(self.window.start, 'x'),
            format(self.window.end, 'x'),
        )

    @classmethod
    def from_storage(
        cls, task_id: str, station_code: str, channel: str,
        multiplier: str, start_tick: str, end_tick: str,
    ) -> 'TaskSnapshot':
        scale = int(multiplier, 16)
        if scale <= 0:
            raise ValueError('invalid archived task multiplier')
        window = TimeWindow(int(start_tick, 16), int(end_tick, 16))
        return cls(task_id, station_code, channel, scale, window)


def freeze_task_snapshot(request: OpenTaskRequest, channel: ChannelDefinition) -> TaskSnapshot:
    if request.channel != channel.name:
        raise ValueError('selected channel differs from task request')
    return TaskSnapshot(
        task_id=request.task_id,
        station_code=request.station_code,
        channel=channel.name,
        multiplier_um=channel.multiplier_um,
        window=request.window,
    )
