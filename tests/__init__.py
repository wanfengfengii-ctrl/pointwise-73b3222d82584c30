"""Shared fixtures for real SQLite tests and instrument exports."""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from capture.archive.audit import ReportAuditor
from capture.archive.database import initialize_database, open_database
from capture.archive.readback import ReportReader
from capture.intake.manual import ManualCapture
from capture.stations.catalog import StationCatalog
from capture.tasks.registry import TaskRegistry
from capture.tasks.request import OpenTaskRequest
from capture.tasks.status import CaptureStatus
from capture.tasks.window import TimeWindow


HEADER = b'time,channel,seq,value\n'
SHIFT = HEADER + b'10,gauge,1,17\n19,gauge,2,24\n'


def archive_counts(db: sqlite3.Connection) -> tuple[int, int]:
    return db.execute('SELECT (SELECT count(*) FROM reports), '
                      '(SELECT count(*) FROM originals)').fetchone()


class DatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(prefix='capture-tests-')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'shifts.sqlite'
        self.db = open_database(self.path)
        self.addCleanup(self.db.close)
        initialize_database(self.db)
        self.stations = StationCatalog(self.db)
        self.tasks = TaskRegistry(self.db)
        self.capture = ManualCapture(self.db)
        self.reports = ReportReader(self.db)
        self.auditor = ReportAuditor(self.db)

    def open_task(self, task_id='shift', station='north', channel='gauge', start=10, end=20):
        return self.tasks.open(OpenTaskRequest(task_id, station, channel, TimeWindow(start, end)))

    def assert_pending(self, task_id='shift') -> None:
        progress = self.tasks.get(task_id).progress
        self.assertEqual(progress.status, CaptureStatus.PENDING)
        self.assertIsNone(progress.report_id)
        self.assertIsNone(self.db.execute('SELECT report_id FROM reports WHERE task_id = ?',
                                         (task_id,)).fetchone())


class RejectNextCommit:
    """Reject one real commit that associates a report with the selected task.

    The connection owns the temporary trigger and its empty parent table. The
    trigger writes an invalid deferred foreign key, so the task UPDATE succeeds
    and SQLite rejects COMMIT. The one-shot flag survives the SQL rollback.
    """

    def __init__(self, db: sqlite3.Connection, task_id: str):
        self._task_id = task_id
        self.fired = False
        db.create_function('capture_trip_commit', 1, self._trip)
        db.execute('CREATE TEMP TABLE capture_fault_parent (id INTEGER PRIMARY KEY)')
        db.execute('''
            CREATE TEMP TABLE capture_fault_child (
                parent_id INTEGER REFERENCES capture_fault_parent(id) DEFERRABLE INITIALLY DEFERRED
            )
        ''')
        db.execute('''
            CREATE TEMP TRIGGER capture_reject_commit
            AFTER UPDATE OF report_id ON main.tasks
            WHEN NEW.report_id IS NOT NULL AND capture_trip_commit(NEW.task_id) = 1
            BEGIN
                INSERT INTO capture_fault_child (parent_id) VALUES (1);
            END
        ''')

    def _trip(self, task_id: str) -> int:
        if task_id != self._task_id or self.fired:
            return 0
        self.fired = True
        return 1
