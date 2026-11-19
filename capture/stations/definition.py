from collections.abc import Iterable
from dataclasses import dataclass

from .units import InvalidMultiplier, require_micrometre_multiplier


class InvalidStationDefinition(ValueError):
    """The complete proposed station definition was rejected."""


@dataclass(frozen=True)
class ChannelDefinition:
    name: str
    multiplier_um: int


@dataclass(frozen=True)
class StationDefinition:
    code: str
    channels: tuple[ChannelDefinition, ...]
    active: bool


def build_station_definition(
    code: str, channels: Iterable[tuple[str, int]], active: bool = True
) -> StationDefinition:
    if not isinstance(code, str) or not code:
        raise InvalidStationDefinition('station code must be nonempty text')
    if type(active) is not bool:
        raise InvalidStationDefinition('active must be a boolean')

    seen: set[str] = set()
    definitions = []
    for name, multiplier in channels:
        if not isinstance(name, str) or not name:
            raise InvalidStationDefinition('channel name must be nonempty text')
        if name in seen:
            raise InvalidStationDefinition(f'duplicate channel {name!r}')
        try:
            scale = require_micrometre_multiplier(multiplier)
        except InvalidMultiplier as exc:
            raise InvalidStationDefinition(f'invalid multiplier for {name!r}') from exc
        definitions.append(ChannelDefinition(name, scale))
        seen.add(name)
    return StationDefinition(code, tuple(definitions), active)
