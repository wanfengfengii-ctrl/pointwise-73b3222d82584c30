import sqlite3

from capture.measurement.report import MeasurementReport
from capture.tasks.registry import CaptureTask, TaskRegistry
from capture.tasks.snapshot import TaskSnapshot
from capture.tasks.status import TaskStateConflict, mark_delivered

from .original import ArchivedOriginal
from .readback import BrowserFixedResult, FixedResult, ReportReader
from .schema import encode_report
from .transaction import write_transaction


class SubmissionConflict(ValueError):
    """The task has already been delivered with different original bytes."""


class PublicationError(RuntimeError):
    """The archive transaction could not publish a fixed result."""


def find_manual_replay(reader: ReportReader, task: CaptureTask, raw: bytes) -> FixedResult | None:
    report_id = task.progress.report_id
    if report_id is None:
        return None
    result = reader.get(report_id)
    if result.original.raw != raw:
        raise SubmissionConflict(task.snapshot.task_id)
    return result


def publish_manual_result(
    db: sqlite3.Connection,
    snapshot: TaskSnapshot,
    original: ArchivedOriginal,
    report: MeasurementReport,
) -> FixedResult:
    try:
        with write_transaction(db):
            current = TaskRegistry(db).get(snapshot.task_id)
            if current.snapshot != snapshot:
                raise PublicationError('stored task scope differs from the measured scope')
            result = find_manual_replay(ReportReader(db), current, original.raw)
            if result is None:
                cursor = db.execute('INSERT INTO reports (task_id, report_json) VALUES (?, ?)',
                                    (snapshot.task_id, encode_report(report)))
                report_id = cursor.lastrowid
                db.execute('INSERT INTO originals (report_id, raw, sha256) VALUES (?, ?, ?)',
                           (report_id, original.raw, original.sha256))
                mark_delivered(db, snapshot.task_id, report_id)
                result = FixedResult(report_id, snapshot, original, report)
    except (sqlite3.Error, TaskStateConflict) as exc:
        raise PublicationError(f'could not publish task {snapshot.task_id!r}') from exc
    return result


def publish_browser_result(
    db: sqlite3.Connection,
    snapshot: TaskSnapshot,
    download_id: str,
    original: ArchivedOriginal,
    report: MeasurementReport,
) -> BrowserFixedResult:
    """Publish the download binding, original, fixed report and delivery together.

    Any failure rolls the whole transaction back: no report, original, delivery
    association or download binding survives, so the retained session original can
    be retried without starting another browser download.
    """
    try:
        with write_transaction(db):
            current = TaskRegistry(db).get(snapshot.task_id)
            if current.snapshot != snapshot:
                raise PublicationError('stored task scope differs from the measured scope')
            if current.progress.report_id is not None:
                raise PublicationError(f'task {snapshot.task_id!r} was already delivered')
            cursor = db.execute('INSERT INTO reports (task_id, report_json) VALUES (?, ?)',
                                (snapshot.task_id, encode_report(report)))
            report_id = cursor.lastrowid
            db.execute('INSERT INTO originals (report_id, raw, sha256) VALUES (?, ?, ?)',
                       (report_id, original.raw, original.sha256))
            db.execute('''
                INSERT INTO browser_downloads (task_id, download_id, report_id)
                VALUES (?, ?, ?)
            ''', (snapshot.task_id, download_id, report_id))
            mark_delivered(db, snapshot.task_id, report_id)
            fixed = FixedResult(report_id, snapshot, original, report)
    except (sqlite3.Error, TaskStateConflict) as exc:
        raise PublicationError(f'could not publish task {snapshot.task_id!r}') from exc
    return BrowserFixedResult(download_id, fixed)
