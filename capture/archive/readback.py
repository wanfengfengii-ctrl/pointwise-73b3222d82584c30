from dataclasses import dataclass
import sqlite3

from capture.measurement.report import MeasurementReport
from capture.tasks.snapshot import TaskSnapshot

from .original import ArchivedOriginal
from .schema import decode_report


class UnknownReport(KeyError):
    """No fixed report has the given number."""


class ArchiveReadError(ValueError):
    """The report exists but its archived records cannot be read as a whole."""


@dataclass(frozen=True)
class FixedResult:
    report_id: int
    task: TaskSnapshot
    original: ArchivedOriginal
    report: MeasurementReport


class ReportReader:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def get(self, report_id: int) -> FixedResult:
        if type(report_id) is not int or not 1 <= report_id <= 2**63 - 1:
            raise UnknownReport(report_id)
        row = self._db.execute('''
            SELECT r.report_id, r.report_json, o.raw, o.sha256,
                   t.task_id, t.station_code, t.channel, t.multiplier, t.start_tick, t.end_tick
              FROM reports AS r
              LEFT JOIN originals AS o ON o.report_id = r.report_id
              LEFT JOIN tasks AS t ON t.task_id = r.task_id AND t.report_id = r.report_id
             WHERE r.report_id = ?
        ''', (report_id,)).fetchone()
        if row is None:
            raise UnknownReport(report_id)
        number, encoded, raw, digest, *snapshot = row
        if snapshot[0] is None or not isinstance(raw, bytes) or not isinstance(digest, str):
            raise ArchiveReadError(f'report {report_id} has an incomplete archive')
        try:
            task = TaskSnapshot.from_storage(*snapshot)
            report = decode_report(encoded)
        except (TypeError, ValueError) as exc:
            raise ArchiveReadError(f'report {report_id} has invalid archived data') from exc
        return FixedResult(number, task, ArchivedOriginal(raw, digest), report)
