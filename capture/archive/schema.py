from fractions import Fraction
import json
import sqlite3

from capture.intake.observations import Observation
from capture.measurement.aggregate import ExactStatistics
from capture.measurement.coverage import SampleCoverage
from capture.measurement.normalize import MeasuredSample
from capture.measurement.report import MeasurementReport


class ArchiveFormatError(ValueError):
    """The persistent measurement representation is incomplete or invalid."""


def create_archive_schema(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            report_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
            report_json TEXT NOT NULL,
            UNIQUE (report_id, task_id),
            FOREIGN KEY (report_id) REFERENCES originals(report_id) DEFERRABLE INITIALLY DEFERRED
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS originals (
            report_id INTEGER PRIMARY KEY REFERENCES reports(report_id),
            raw BLOB NOT NULL CHECK (typeof(raw) = 'blob'),
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64)
        )
    ''')


def _optional_text(value: int | None) -> str | None:
    return None if value is None else format(value, 'x')


def _sample_data(sample: MeasuredSample) -> dict:
    row = sample.observation
    return {
        'time': format(row.time, 'x'),
        'channel': row.channel,
        'seq': format(row.seq, 'x'),
        'value': format(row.value, 'x'),
        'line': format(row.line, 'x'),
        'micrometres': format(sample.micrometres, 'x'),
    }


def encode_report(report: MeasurementReport) -> str:
    stats = report.statistics
    mean = stats.mean_um
    coverage = report.coverage
    return json.dumps({
        'statistics': {
            'count': format(stats.count, 'x'),
            'min_um': _optional_text(stats.min_um),
            'max_um': _optional_text(stats.max_um),
            'mean_um': (None if mean is None else
                        [format(mean.numerator, 'x'), format(mean.denominator, 'x')]),
        },
        'coverage': {
            'first_tick': _optional_text(coverage.first_tick),
            'last_tick': _optional_text(coverage.last_tick),
        },
        'samples': [_sample_data(sample) for sample in report.samples],
    }, sort_keys=True, separators=(',', ':'))


def _integer(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError('expected a textual integer')
    number = int(value, 16)
    if format(number, 'x') != value:
        raise ValueError('expected a canonical hexadecimal integer')
    return number


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _mean(value: object) -> Fraction | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('expected a pair of integers for the mean')
    numerator, denominator = map(_integer, value)
    if denominator <= 0:
        raise ValueError('expected a positive denominator')
    mean = Fraction(numerator, denominator)
    if mean.denominator != denominator:
        raise ValueError('mean must be in lowest terms')
    return mean


def _decode_sample(data: dict) -> MeasuredSample:
    channel = data['channel']
    line = _integer(data['line'])
    if not isinstance(channel, str) or not channel or line < 2:
        raise ValueError('invalid sample location or channel')
    return MeasuredSample(
        Observation(
            time=_integer(data['time']),
            channel=channel,
            seq=_integer(data['seq']),
            value=_integer(data['value']),
            line=line,
        ),
        _integer(data['micrometres']),
    )


def decode_report(encoded: str) -> MeasurementReport:
    try:
        data = json.loads(encoded)
        stats = data['statistics']
        coverage = data['coverage']
        samples = data['samples']
        count = _integer(stats['count'])
        if count < 0 or not isinstance(samples, list):
            raise ValueError('invalid archive sample count or sequence')
        return MeasurementReport(
            statistics=ExactStatistics(
                count, _optional_integer(stats['min_um']),
                _optional_integer(stats['max_um']), _mean(stats['mean_um']),
            ),
            coverage=SampleCoverage(
                _optional_integer(coverage['first_tick']), _optional_integer(coverage['last_tick']),
            ),
            samples=tuple(_decode_sample(sample) for sample in samples),
        )
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise ArchiveFormatError('malformed fixed measurement report') from exc
