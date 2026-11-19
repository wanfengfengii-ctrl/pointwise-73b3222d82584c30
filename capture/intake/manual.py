import sqlite3

from capture.archive.original import capture_original
from capture.archive.publication import find_manual_replay, publish_manual_result
from capture.archive.readback import FixedResult, ReportReader
from capture.measurement.report import build_report
from capture.tasks.registry import TaskRegistry

from .csv_rows import parse_csv
from .eligibility import validate_observations


class ManualCapture:
    def __init__(self, db: sqlite3.Connection):
        self._db = db
        self._tasks = TaskRegistry(db)
        self._reports = ReportReader(db)

    def submit(self, task_id: str, raw: bytes) -> FixedResult:
        task = self._tasks.get(task_id)
        original = capture_original(raw)
        replay = find_manual_replay(self._reports, task, original.raw)
        if replay is not None:
            return replay
        rows = validate_observations(task.snapshot, parse_csv(original.raw))
        report = build_report(task.snapshot, rows)
        return publish_manual_result(self._db, task.snapshot, original, report)


def _report_values(result: FixedResult) -> dict:
    stats = result.report.statistics
    coverage = result.report.coverage
    mean = stats.mean_um
    return {
        'report_id': result.report_id,
        'multiplier_um': result.task.multiplier_um,
        'count': stats.count,
        'min_um': stats.min_um,
        'max_um': stats.max_um,
        'mean_um': None if mean is None else [mean.numerator, mean.denominator],
        'ticks': [coverage.first_tick, coverage.last_tick],
        'lines': [sample.observation.line for sample in result.report.samples],
    }


def _demo() -> None:
    from contextlib import closing
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from capture.archive.audit import ReportAuditor
    from capture.archive.database import initialize_database, open_database
    from capture.stations.catalog import StationCatalog
    from capture.tasks.request import OpenTaskRequest
    from capture.tasks.window import TimeWindow

    sample = Path(__file__).resolve().parents[2] / 'samples' / 'shift.csv'
    raw = sample.read_bytes()
    next_raw = b'time,channel,seq,value\n26,gauge,3,-2\n'
    expected = {
        'report_id': 1, 'multiplier_um': 1, 'count': 2,
        'min_um': 17, 'max_um': 24, 'mean_um': [41, 2],
        'ticks': [10, 19], 'lines': [2, 3],
    }
    downstream_expected = {
        'day': expected,
        'evening': {
            'report_id': 2, 'multiplier_um': 3, 'count': 1,
            'min_um': -6, 'max_um': -6, 'mean_um': [-6, 1],
            'ticks': [26, 26], 'lines': [2],
        },
        'same_originals': True,
        'audits_passed': [True, True],
    }
    with TemporaryDirectory(prefix='yuchuan-') as directory:
        path = Path(directory) / 'shifts.sqlite'
        with closing(open_database(path)) as db:
            initialize_database(db)
            stations = StationCatalog(db)
            tasks = TaskRegistry(db)
            stations.upsert('north', [('gauge', 1)])
            tasks.open(OpenTaskRequest('day', 'north', 'gauge', TimeWindow(10, 20)))
            capture = ManualCapture(db)
            actual = _report_values(capture.submit('day', raw))
            stations.upsert('north', [('gauge', 3)])
            tasks.open(OpenTaskRequest('evening', 'north', 'gauge', TimeWindow(20, 30)))
            capture.submit('evening', next_raw)
            stations.upsert('north', [('gauge', 5)], active=False)
        with closing(open_database(path)) as db:
            tasks = TaskRegistry(db)
            reports = ReportReader(db)
            auditor = ReportAuditor(db)
            ids = [tasks.get(task_id).progress.report_id for task_id in ('day', 'evening')]
            day, evening = (reports.get(report_id) for report_id in ids)
            downstream = {
                'day': _report_values(day), 'evening': _report_values(evening),
                'same_originals': (day.original.raw, evening.original.raw) == (raw, next_raw),
                'audits_passed': [auditor.audit(report_id).passed for report_id in ids],
            }
    if actual != expected or downstream != downstream_expected:
        raise RuntimeError(f'unexpected shift results: {actual!r}, {downstream!r}')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'exact',
        'input': {'day_csv': raw.decode(), 'evening_csv': next_raw.decode(),
                  'windows': [[10, 20], [20, 30]], 'multipliers': [1, 3]},
        'expected': expected,
        'actual': actual,
        'downstream_expected': downstream_expected,
        'downstream_actual': downstream,
        'code_paths': ['capture/intake/manual.py', 'capture/intake/csv_rows.py',
                       'capture/measurement/report.py', 'capture/archive/publication.py',
                       'capture/archive/readback.py', 'capture/archive/audit.py'],
        'oracle': 'Manual calculation: (17 + 24) / 2 = 41/2 and (-2) * 3 = -6. '
                  'Compare the archived bytes to the two supplied originals.',
    }, sort_keys=True))


if __name__ == '__main__':
    _demo()
