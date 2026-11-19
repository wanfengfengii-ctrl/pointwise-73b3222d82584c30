from dataclasses import dataclass
import sqlite3

from capture.archive.transaction import write_transaction
from capture.stations.catalog import StationCatalog
from capture.stations.lifecycle import select_open_channel

from .request import OpenTaskRequest
from .snapshot import TaskSnapshot, freeze_task_snapshot
from .status import TaskProgress, progress_from_report_id


class UnknownTask(KeyError):
    """No task has the requested number."""


class TaskRequestConflict(ValueError):
    """The task number already identifies a different request."""


@dataclass(frozen=True)
class CaptureTask:
    snapshot: TaskSnapshot
    progress: TaskProgress


class TaskRegistry:
    def __init__(self, db: sqlite3.Connection):
        self._db = db
        self._stations = StationCatalog(db)

    @staticmethod
    def create_schema(db: sqlite3.Connection) -> None:
        db.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY NOT NULL,
                station_code TEXT NOT NULL REFERENCES stations(code),
                channel TEXT NOT NULL,
                multiplier TEXT NOT NULL,
                start_tick TEXT NOT NULL,
                end_tick TEXT NOT NULL,
                report_id INTEGER UNIQUE,
                FOREIGN KEY (report_id, task_id) REFERENCES reports(report_id, task_id)
            )
        ''')

    def _find(self, task_id: str) -> CaptureTask | None:
        row = self._db.execute('''
            SELECT task_id, station_code, channel, multiplier, start_tick, end_tick, report_id
              FROM tasks WHERE task_id = ?
        ''', (task_id,)).fetchone()
        if row is None:
            return None
        return CaptureTask(TaskSnapshot.from_storage(*row[:6]), progress_from_report_id(row[6]))

    def open(self, request: OpenTaskRequest) -> CaptureTask:
        with write_transaction(self._db):
            existing = self._find(request.task_id)
            if existing is not None:
                if existing.snapshot.request_identity() != request.identity():
                    raise TaskRequestConflict(request.task_id)
                return existing

            station = self._stations.get(request.station_code)
            channel = select_open_channel(station, request.channel)
            snapshot = freeze_task_snapshot(request, channel)
            self._db.execute('''
                INSERT INTO tasks (task_id, station_code, channel, multiplier, start_tick, end_tick)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', snapshot.to_storage())
            return CaptureTask(snapshot, progress_from_report_id(None))

    def get(self, task_id: str) -> CaptureTask:
        task = self._find(task_id)
        if task is None:
            raise UnknownTask(task_id)
        return task
