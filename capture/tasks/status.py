from dataclasses import dataclass
from enum import Enum
import sqlite3


class CaptureStatus(str, Enum):
    PENDING = 'pending'
    DELIVERED = 'delivered'


@dataclass(frozen=True)
class TaskProgress:
    status: CaptureStatus
    report_id: int | None


class TaskStateConflict(RuntimeError):
    """Publication could not associate its report with the pending task."""


def progress_from_report_id(report_id: int | None) -> TaskProgress:
    if report_id is None:
        return TaskProgress(CaptureStatus.PENDING, None)
    return TaskProgress(CaptureStatus.DELIVERED, report_id)


def mark_delivered(db: sqlite3.Connection, task_id: str, report_id: int) -> None:
    if not db.in_transaction:
        raise RuntimeError('mark_delivered requires the publication transaction')
    changed = db.execute('''
        UPDATE tasks SET report_id = ? WHERE task_id = ? AND report_id IS NULL
    ''', (report_id, task_id))
    if changed.rowcount != 1:
        raise TaskStateConflict(task_id)
