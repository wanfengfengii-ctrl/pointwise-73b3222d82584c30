from .definition import ChannelDefinition, StationDefinition


class StationInactive(ValueError):
    """The station cannot take a new task while disabled."""


class UnknownChannel(KeyError):
    """The requested channel is absent from the active definition."""


def select_open_channel(station: StationDefinition, name: str) -> ChannelDefinition:
    if not station.active:
        raise StationInactive(station.code)
    for channel in station.channels:
        if channel.name == name:
            return channel
    raise UnknownChannel((station.code, name))
