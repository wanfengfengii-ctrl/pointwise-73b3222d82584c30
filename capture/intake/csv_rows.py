import csv
from decimal import Decimal
import io
import re

from .observations import Observation


CSV_COLUMNS = ('time', 'channel', 'seq', 'value')
_INTEGER = re.compile(r'[+-]?[0-9]+')


class ExportFormatError(ValueError):
    """The export cannot be read as the expected instrument CSV."""


def _parse_integer(text: str, field: str, line: int) -> int:
    if _INTEGER.fullmatch(text) is None:
        raise ExportFormatError(f'line {line}: {field} must be a decimal integer')
    return int(Decimal(text))


def parse_csv(raw: bytes) -> tuple[Observation, ...]:
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ExportFormatError('export is not valid UTF-8') from exc

    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    rows = []
    try:
        header = next(reader, ())
        if tuple(header) != CSV_COLUMNS:
            raise ExportFormatError('line 1: expected time,channel,seq,value header')
        while True:
            line = reader.line_num + 1
            fields = next(reader, None)
            if fields is None:
                break
            if len(fields) != len(CSV_COLUMNS):
                raise ExportFormatError(f'line {line}: expected four columns')
            time, channel, seq, value = fields
            rows.append(Observation(
                time=_parse_integer(time, 'time', line),
                channel=channel,
                seq=_parse_integer(seq, 'seq', line),
                value=_parse_integer(value, 'value', line),
                line=line,
            ))
    except csv.Error as exc:
        raise ExportFormatError(f'invalid CSV at physical line {reader.line_num}') from exc
    return tuple(rows)
