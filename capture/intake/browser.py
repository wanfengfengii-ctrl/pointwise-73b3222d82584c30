"""Deterministic browser source port with callback-scoped original reads."""

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from capture.archive.original import capture_original
from capture.archive.publication import PublicationError, publish_browser_result
from capture.archive.readback import BrowserFixedResult, ReportReader
from capture.tasks.registry import TaskRegistry, UnknownTask
from capture.tasks.snapshot import TaskSnapshot


class OriginalExpired(RuntimeError):
    """The borrow ended or its retained handle has been released."""


class RetainedOriginal:
    def __init__(self, raw: bytes):
        self._raw: bytes | None = raw

    @property
    def released(self) -> bool:
        return self._raw is None

    def read(self) -> bytes:
        raw = self._raw
        if raw is None:
            raise OriginalExpired('retained original has been released')
        return raw

    def release(self) -> None:
        self._raw = None


class BorrowedOriginal:
    def __init__(self, raw: bytes, handles: list[RetainedOriginal]):
        self._raw: bytes | None = raw
        self._handles = handles

    def read(self) -> bytes:
        raw = self._raw
        if raw is None:
            raise OriginalExpired('download callback has returned')
        return raw

    def retain(self) -> RetainedOriginal:
        handle = RetainedOriginal(self.read())
        self._handles.append(handle)
        return handle

    def _invalidate(self) -> None:
        self._raw = None


@dataclass(frozen=True)
class DownloadCompletion:
    download_id: str
    original: BorrowedOriginal


@dataclass(frozen=True)
class StartResult:
    download_id: str | None

    @property
    def accepted(self) -> bool:
        return self.download_id is not None


CompletionHandler = Callable[[DownloadCompletion], None]


class BrowserPort(Protocol):
    def subscribe(self, handler: CompletionHandler) -> None:
        """Install the completion recipient for this browser session."""
        ...

    def start(self, task: TaskSnapshot) -> StartResult:
        """Start serially. Completions can be delivered before this returns."""
        ...


@dataclass(frozen=True)
class ScriptedExport:
    download_id: str
    raw: bytes


@dataclass(frozen=True)
class StartStep:
    task_id: str
    result: StartResult
    during_start: tuple[ScriptedExport, ...] = ()


class ScriptedBrowser:
    """Play source notifications in script order with an explicit borrow boundary."""

    def __init__(self, steps: Iterable[StartStep]):
        self._steps = deque(steps)
        accepted = [step.result.download_id for step in self._steps if step.result.accepted]
        if len(accepted) != len(set(accepted)):
            raise ValueError('script repeats an accepted download ID')
        self._handler: CompletionHandler | None = None
        self._starts: list[str] = []
        self._notified: set[str] = set()
        self._handles: dict[str, list[RetainedOriginal]] = {}
        self._starting = False
        self._stopped = False

    @property
    def starts(self) -> tuple[str, ...]:
        return tuple(self._starts)

    def subscribe(self, handler: CompletionHandler) -> None:
        if self._stopped or self._handler is not None:
            raise RuntimeError('browser session cannot accept another subscriber')
        self._handler = handler

    def start(self, task: TaskSnapshot) -> StartResult:
        if self._stopped or self._starting or self._handler is None:
            raise RuntimeError('browser session cannot start a download now')
        if not self._steps or self._steps[0].task_id != task.task_id:
            raise RuntimeError(f'unexpected download start for {task.task_id!r}')
        step = self._steps.popleft()
        self._starts.append(task.task_id)
        self._starting = True
        try:
            for export in step.during_start:
                self.deliver(export)
            return step.result
        finally:
            self._starting = False

    def deliver(self, export: ScriptedExport) -> None:
        handler = self._handler
        if self._stopped or handler is None:
            raise RuntimeError('browser session cannot deliver a notification now')
        if export.download_id in self._notified:
            raise ValueError(f'repeated completion for {export.download_id!r}')
        self._notified.add(export.download_id)
        handles: list[RetainedOriginal] = []
        self._handles[export.download_id] = handles
        borrowed = BorrowedOriginal(export.raw, handles)
        try:
            handler(DownloadCompletion(export.download_id, borrowed))
        finally:
            borrowed._invalidate()

    def active_handles(self, download_id: str) -> int:
        return sum(not handle.released for handle in self._handles.get(download_id, ()))

    def stop(self) -> None:
        self._stopped = True
        self._handler = None
        self._steps.clear()


class BrowserState(str, Enum):
    """Session-side states of a task handled by a browser capture session."""

    PENDING = 'pending'
    START_REJECTED = 'start_rejected'
    INVALID_ORIGINAL = 'invalid_original'
    RETRY_PENDING = 'retry_pending'
    ARCHIVED = 'archived'


@dataclass(frozen=True)
class BrowserStatus:
    status: BrowserState
    result: BrowserFixedResult | None


class _TaskRecord:
    """Session bookkeeping for one started task.

    The full original bytes are captured inside the completion callback, while the
    borrow is still valid. They stay in session memory only until the download is
    archived or finally rejected, never beyond the session.
    """

    def __init__(self, snapshot: TaskSnapshot, state: BrowserState,
                 download_id: str | None = None):
        self.snapshot = snapshot
        self.download_id = download_id
        self.state = state
        self.raw: bytes | None = None
        self.result: BrowserFixedResult | None = None


class BrowserCapture:
    """Drive one browser session and archive downloads by confirmed download ID.

    Source CSVs carry no station or task number, so attribution comes solely from
    the download ID the browser finally confirms on ``start``. Notifications may
    arrive interleaved, even synchronously inside another task's ``start`` call;
    accepted downloads already confirmed during this session are never lost, and
    notifications without an accepted owner are discarded with their originals.
    """

    def __init__(self, db, browser: BrowserPort):
        self._db = db
        self._browser = browser
        self._tasks = TaskRegistry(db)
        self._records: dict[str, _TaskRecord] = {}
        self._accepted: dict[str, str] = {}
        self._starting: str | None = None
        self._pending: dict[str, bytes] = {}
        browser.subscribe(self._on_completion)

    def _on_completion(self, completion: DownloadCompletion) -> None:
        download_id = completion.download_id
        task_id = self._accepted.get(download_id)
        if task_id is None:
            if self._starting is not None:
                # Owner does not exist yet, or the current start is rejected:
                # keep the bytes only until start() adjudicates ownership.
                self._pending.setdefault(download_id, completion.original.read())
            # Otherwise the download has no accepted owner: drop the original.
            return
        if task_id == self._starting:
            # The owner's own start verdict has not been returned yet.
            self._pending.setdefault(download_id, completion.original.read())
            return
        # The borrow ends when this callback returns, so take the full bytes now.
        self._archive_bytes(task_id, download_id, completion.original.read())

    def _archive_bytes(self, task_id: str, download_id: str, raw: bytes) -> None:
        record = self._records.get(task_id)
        if record is None or record.state in (
            BrowserState.ARCHIVED, BrowserState.INVALID_ORIGINAL
        ):
            # No live accepted owner: the original is unclaimed and is dropped
            # together with the borrow when the callback returns.
            return
        from capture.intake.csv_rows import ExportFormatError, parse_csv
        from capture.intake.eligibility import (
            ObservationEligibilityError, validate_observations,
        )
        from capture.measurement.report import build_report

        record.raw = raw
        try:
            rows = validate_observations(record.snapshot, parse_csv(raw))
            report = build_report(record.snapshot, rows)
            archived = publish_browser_result(
                self._db, record.snapshot, download_id, capture_original(raw), report,
            )
        except (ExportFormatError, ObservationEligibilityError):
            record.state = BrowserState.INVALID_ORIGINAL
            record.raw = None
            return
        except PublicationError:
            # Nothing was committed; keep the bytes for an in-session retry.
            record.state = BrowserState.RETRY_PENDING
            return
        record.state = BrowserState.ARCHIVED
        record.result = archived
        record.raw = None

    def start(self, task_id: str) -> BrowserStatus:
        if task_id in self._records:
            raise RuntimeError(f'task {task_id!r} was already started in this session')
        task = self._tasks.get(task_id)
        # Notifications may be delivered synchronously inside the browser call,
        # so the adjudication window must be open before invoking start().
        self._starting = task_id
        self._pending = {}
        try:
            result = self._browser.start(task.snapshot)
            if not result.accepted:
                # Keep a terminal record so get() keeps reporting the rejection.
                self._records[task_id] = _TaskRecord(
                    task.snapshot, BrowserState.START_REJECTED,
                )
            else:
                record = _TaskRecord(task.snapshot, BrowserState.PENDING, result.download_id)
                self._records[task_id] = record
                self._accepted[result.download_id] = task_id
        finally:
            self._starting = None
            pending = self._pending
            self._pending = {}
        # Adjudicate every notification delivered during this start call. Older
        # accepted downloads are processed first so confirmed work is not lost;
        # the newly accepted owner is processed after its verdict. Downloads
        # without an accepted owner (including rejected starts) are discarded.
        for download_id, raw in pending.items():
            owner = self._accepted.get(download_id)
            if owner is not None:
                self._archive_bytes(owner, download_id, raw)
        return self.get(task_id)

    def get(self, task_id: str) -> BrowserStatus:
        record = self._records.get(task_id)
        if record is None:
            # Unknown identifiers stay exceptional; a known task not yet started
            # in this session is simply still pending.
            self._tasks.get(task_id)
            return BrowserStatus(BrowserState.PENDING, None)
        return BrowserStatus(record.state, record.result)

    def retry(self, task_id: str) -> BrowserStatus:
        record = self._records.get(task_id)
        if record is None:
            self._tasks.get(task_id)
            return BrowserStatus(BrowserState.PENDING, None)
        if record.state is not BrowserState.RETRY_PENDING or record.raw is None:
            # retry only retries: every other state is returned unchanged.
            return BrowserStatus(record.state, record.result)
        # Reuse the original download and its retained bytes: no new start call.
        self._archive_bytes(task_id, record.download_id, record.raw)
        return self.get(task_id)


def _report_values(result: BrowserFixedResult) -> dict:
    stats = result.fixed.report.statistics
    coverage = result.fixed.report.coverage
    mean = stats.mean_um
    return {
        'download_id': result.download_id,
        'report_id': result.fixed.report_id,
        'station': result.fixed.task.station_code,
        'multiplier_um': result.fixed.task.multiplier_um,
        'count': stats.count,
        'min_um': stats.min_um,
        'max_um': stats.max_um,
        'mean_um': None if mean is None else [mean.numerator, mean.denominator],
        'ticks': [coverage.first_tick, coverage.last_tick],
        'physical_lines': [sample.observation.line for sample in result.fixed.report.samples],
        'raw_csv': result.fixed.original.raw.decode(),
    }


def _demo() -> None:
    import json
    from contextlib import closing
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from capture.archive.database import initialize_database, open_database
    from capture.stations.catalog import StationCatalog
    from capture.tasks.registry import TaskRegistry
    from capture.tasks.request import OpenTaskRequest
    from capture.tasks.window import TimeWindow
    from tests import RejectNextCommit

    north_csv = b'time,channel,seq,value\n10,gauge,1,17\n'
    south_csv = b'time,channel,seq,value\n10,gauge,1,23\n'
    north_export = ScriptedExport('D7', north_csv)
    south_export = ScriptedExport('D8', south_csv)
    expected_north = {
        'download_id': 'D7', 'report_id': 1, 'station': 'north', 'multiplier_um': 1,
        'count': 1, 'min_um': 17, 'max_um': 17, 'mean_um': [17, 1],
        'ticks': [10, 10], 'physical_lines': [2], 'raw_csv': north_csv.decode(),
    }
    expected_south = {
        'download_id': 'D8', 'report_id': 2, 'station': 'south', 'multiplier_um': 1,
        'count': 1, 'min_um': 23, 'max_um': 23, 'mean_um': [23, 1],
        'ticks': [10, 10], 'physical_lines': [2], 'raw_csv': south_csv.decode(),
    }

    scenario: dict = {'interleaved': {}, 'commit_failure_retry': {}, 'reopened_claim': {}}
    with TemporaryDirectory(prefix='yuchuan-browser-') as directory:
        path = Path(directory) / 'shifts.sqlite'
        with closing(open_database(path)) as db:
            initialize_database(db)
            stations = StationCatalog(db)
            stations.upsert('north', [('gauge', 1)])
            stations.upsert('south', [('gauge', 1)])
            tasks = TaskRegistry(db)
            tasks.open(OpenTaskRequest('north-shift', 'north', 'gauge', TimeWindow(10, 20)))
            tasks.open(OpenTaskRequest('south-shift', 'south', 'gauge', TimeWindow(10, 20)))

            # Scenario 1: D7 and D8 arrive interleaved inside the south start call.
            browser = ScriptedBrowser([
                StartStep('north-shift', StartResult('D7')),
                StartStep('south-shift', StartResult('D8'), (north_export, south_export)),
            ])
            capture = BrowserCapture(db, browser)
            north_status = capture.start('north-shift')
            scenario['interleaved']['north_status_at_start_return'] = north_status.status.value
            south_status = capture.start('south-shift')
            scenario['interleaved']['south_status_at_start_return'] = south_status.status.value
            scenario['interleaved']['starts'] = list(browser.starts)
            north = _report_values(capture.get('north-shift').result)
            south = _report_values(capture.get('south-shift').result)
            scenario['interleaved']['north'] = north
            scenario['interleaved']['south'] = south
            browser.stop()

        # Scenario 2: a fresh session where the south commit fails once and is
        # retried with the retained original and no second browser download.
        with closing(open_database(path)) as db:
            initialize_database(db)
            tasks = TaskRegistry(db)
            tasks.open(OpenTaskRequest('retry-shift', 'south', 'gauge', TimeWindow(10, 20)))
            # The manual intake stays open alongside the browser sessions.
            tasks.open(OpenTaskRequest('manual-shift', 'north', 'gauge', TimeWindow(10, 20)))
            from capture.intake.manual import ManualCapture

            manual = ManualCapture(db).submit('manual-shift', north_csv)
            fault = RejectNextCommit(db, 'retry-shift')
            browser = ScriptedBrowser([
                StartStep('retry-shift', StartResult('D9'), (ScriptedExport('D9', south_csv),)),
            ])
            capture = BrowserCapture(db, browser)
            failed = capture.start('retry-shift')
            scenario['commit_failure_retry']['first_status'] = failed.status.value
            scenario['commit_failure_retry']['nothing_published'] = (
                db.execute('SELECT count(*) FROM browser_downloads').fetchone()[0],
                db.execute('SELECT count(*) FROM reports').fetchone()[0],
                tasks.get('retry-shift').progress.report_id,
            )
            retried = capture.retry('retry-shift')
            scenario['commit_failure_retry']['retry_status'] = retried.status.value
            scenario['commit_failure_retry']['fault_fired'] = fault.fired
            scenario['commit_failure_retry']['starts'] = list(browser.starts)
            scenario['commit_failure_retry']['archived'] = _report_values(retried.result)
            scenario['commit_failure_retry']['manual_report_id'] = manual.report_id
            scenario['commit_failure_retry']['manual_has_no_browser_binding'] = (
                ReportReader(db).get_browser('manual-shift') is None
            )
            browser.stop()

        # Scenario 3: stop the source and reopen; the fixed results are claimed
        # from the database alone, including download provenance and raw CSV.
        with closing(open_database(path)) as db:
            initialize_database(db)
            reader = ReportReader(db)
            claims = {}
            for task_id in ('north-shift', 'south-shift', 'retry-shift'):
                claimed = reader.get_browser(task_id)
                claims[task_id] = None if claimed is None else _report_values(claimed)
            claims['manual-shift'] = reader.get_browser('manual-shift')
            scenario['reopened_claim'] = claims
            tasks = TaskRegistry(db)
            scenario['reopened_claim']['delivery'] = {
                task_id: tasks.get(task_id).progress.report_id
                for task_id in ('north-shift', 'south-shift', 'retry-shift', 'manual-shift')
            }

    if north != expected_north or south != expected_south:
        raise RuntimeError(f'unexpected browser capture results: {north!r}, {south!r}')
    if failed.status is not BrowserState.RETRY_PENDING or retried.status is not BrowserState.ARCHIVED:
        raise RuntimeError('browser commit failure did not reach retry_pending then archived')
    if claims['north-shift'] != expected_north or claims['south-shift'] != expected_south:
        raise RuntimeError('reopened database did not return the same fixed browser results')
    if claims['manual-shift'] is not None:
        raise RuntimeError('a manual delivery must not expose a browser download binding')
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'browser_capture',
        'scenario': scenario,
        'expected': {'north': expected_north, 'south': expected_south},
        'code_paths': ['capture/intake/browser.py', 'capture/archive/publication.py',
                       'capture/archive/readback.py', 'capture/archive/schema.py'],
        'oracle': 'CSV has no station column: attribution is the browser-confirmed '
                  'download ID. One-row samples have mean 17 um (D7) and 23 um (D8), '
                  'each with two physical lines; the failed commit leaves no rows and '
                  'the retained original re-publishes on retry without another start.',
    }, sort_keys=True, ensure_ascii=False))


if __name__ == '__main__':
    _demo()
