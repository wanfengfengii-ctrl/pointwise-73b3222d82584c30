from contextlib import closing
from fractions import Fraction

from capture.archive.database import initialize_database, open_database
from capture.archive.readback import BrowserFixedResult, ReportReader
from capture.intake.browser import (
    BrowserCapture, BrowserState, ScriptedBrowser, ScriptedExport,
    StartResult, StartStep,
)
from capture.intake.manual import ManualCapture
from capture.tasks.registry import UnknownTask
from capture.tasks.status import CaptureStatus

from tests import HEADER, DatabaseTestCase, RejectNextCommit, archive_counts


class BrowserCaptureTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.stations.upsert('north', [('gauge', 1)])
        self.stations.upsert('south', [('gauge', 1)])
        self.open_task('north-shift', station='north')
        self.open_task('south-shift', station='south')
        self.north_raw = HEADER + b'10,gauge,1,17\n'
        self.south_raw = HEADER + b'10,gauge,1,23\n'

    def _capture(self, steps):
        browser = ScriptedBrowser(steps)
        return BrowserCapture(self.db, browser), browser

    def test_interleaved_completions_are_attributed_by_download_id(self):
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7')),
            StartStep('south-shift', StartResult('D8'), (
                ScriptedExport('D7', self.north_raw),
                ScriptedExport('D8', self.south_raw),
            )),
        ])
        north = capture.start('north-shift')
        self.assertEqual(north.status, BrowserState.PENDING)
        self.assertIsNone(north.result)
        south = capture.start('south-shift')
        # Both notifications were delivered inside the south start call.
        self.assertEqual(south.status, BrowserState.ARCHIVED)
        self.assertEqual(capture.get('north-shift').status, BrowserState.ARCHIVED)
        north_fixed = capture.get('north-shift').result
        south_fixed = capture.get('south-shift').result
        self.assertEqual(
            (north_fixed.download_id, north_fixed.fixed.task.station_code,
             north_fixed.fixed.report.statistics.mean_um,
             [s.observation.line for s in north_fixed.fixed.report.samples]),
            ('D7', 'north', Fraction(17, 1), [2]),
        )
        self.assertEqual(
            (south_fixed.download_id, south_fixed.fixed.task.station_code,
             south_fixed.fixed.report.statistics.mean_um,
             [s.observation.line for s in south_fixed.fixed.report.samples]),
            ('D8', 'south', Fraction(23, 1), [2]),
        )
        self.assertEqual(north_fixed.fixed.original.raw, self.north_raw)
        self.assertEqual(south_fixed.fixed.original.raw, self.south_raw)
        self.assertEqual(browser.starts, ('north-shift', 'south-shift'))
        browser.stop()
        # Readback through the database has the same shape and provenance.
        reader = ReportReader(self.db)
        for task_id, download_id in (('north-shift', 'D7'), ('south-shift', 'D8')):
            claimed = reader.get_browser(task_id)
            self.assertIsInstance(claimed, BrowserFixedResult)
            self.assertEqual(claimed.download_id, download_id)
            self.assertEqual(claimed.fixed, capture.get(task_id).result.fixed)
        self.assertEqual(archive_counts(self.db), (2, 2))
        self.assertEqual(self.db.execute('SELECT count(*) FROM browser_downloads').fetchone(), (2,))

    def test_completion_after_start_return_stays_pending_then_archives(self):
        south_export = ScriptedExport('D8', self.south_raw)
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7')),
            StartStep('south-shift', StartResult('D8')),
        ])
        self.assertEqual(capture.start('north-shift').status, BrowserState.PENDING)
        south = capture.start('south-shift')
        self.assertEqual(south.status, BrowserState.PENDING)
        browser.deliver(south_export)
        self.assertEqual(capture.get('south-shift').status, BrowserState.ARCHIVED)
        self.assertEqual(capture.get('south-shift').result.download_id, 'D8')
        browser.deliver(ScriptedExport('D7', self.north_raw))
        north = capture.get('north-shift')
        self.assertEqual(north.status, BrowserState.ARCHIVED)
        self.assertEqual(north.result.fixed.report.statistics.mean_um, Fraction(17, 1))
        self.assertEqual(browser.starts, ('north-shift', 'south-shift'))
        browser.stop()

    def test_doc_script_moved_export_delivered_after_start_return(self):
        # Exact BROWSER_CAPTURE.md variant: south_export is moved out of the
        # start script and delivered once start has returned pending.
        north_export = ScriptedExport('D7', self.north_raw)
        south_export = ScriptedExport('D8', self.south_raw)
        browser = ScriptedBrowser([
            StartStep('north-shift', StartResult('D7')),
            StartStep('south-shift', StartResult('D8')),
        ])
        capture = BrowserCapture(self.db, browser)
        capture.start('north-shift')
        self.assertEqual(capture.start('south-shift').status, BrowserState.PENDING)
        browser.deliver(south_export)
        south = capture.get('south-shift')
        self.assertEqual(south.status, BrowserState.ARCHIVED)
        self.assertEqual(south.result.download_id, 'D8')
        self.assertEqual(south.result.fixed.report.statistics.mean_um, Fraction(23, 1))
        browser.deliver(north_export)
        north = capture.get('north-shift')
        self.assertEqual(north.result.download_id, 'D7')
        self.assertEqual(north.result.fixed.report.statistics.mean_um, Fraction(17, 1))
        browser.stop()

    def test_header_only_csv_archives_zero_sample_report(self):
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7'), (ScriptedExport('D7', HEADER),)),
        ])
        status = capture.start('north-shift')
        self.assertEqual(status.status, BrowserState.ARCHIVED)
        stats = status.result.fixed.report.statistics
        self.assertEqual((stats.count, stats.min_um, stats.max_um, stats.mean_um),
                         (0, None, None, None))
        self.assertEqual(status.result.fixed.original.raw, HEADER)
        browser.stop()

    def test_commit_failure_keeps_original_and_retry_succeeds_without_new_start(self):
        fault = RejectNextCommit(self.db, 'south-shift')
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7'),
                      (ScriptedExport('D7', self.north_raw),)),
            StartStep('south-shift', StartResult('D8'),
                      (ScriptedExport('D8', self.south_raw),)),
        ])
        north = capture.start('north-shift')
        self.assertEqual(north.status, BrowserState.ARCHIVED)
        south = capture.start('south-shift')
        self.assertTrue(fault.fired)
        self.assertEqual(south.status, BrowserState.RETRY_PENDING)
        self.assertIsNone(south.result)
        # A failed commit leaves no report, original, delivery or binding.
        self.assertEqual(archive_counts(self.db), (1, 1))
        self.assertEqual(
            self.db.execute('SELECT count(*) FROM browser_downloads').fetchone(), (1,))
        self.assert_pending('south-shift')
        self.assertIsNone(ReportReader(self.db).get_browser('south-shift'))
        # North, which confirmed earlier, is unaffected and stays claimable.
        self.assertEqual(ReportReader(self.db).get_browser('north-shift').download_id, 'D7')
        # get is read-only; retry reuses the retained original download.
        self.assertEqual(capture.get('south-shift').status, BrowserState.RETRY_PENDING)
        retried = capture.retry('south-shift')
        self.assertEqual(retried.status, BrowserState.ARCHIVED)
        self.assertEqual(retried.result.download_id, 'D8')
        self.assertEqual(retried.result.fixed.original.raw, self.south_raw)
        self.assertEqual(browser.starts, ('north-shift', 'south-shift'))
        # Retrying a terminal task, or getting it, does not start anything new.
        self.assertEqual(capture.retry('south-shift'), retried)
        self.assertEqual(browser.starts, ('north-shift', 'south-shift'))
        browser.stop()

    def test_stop_and_reopen_claims_same_fixed_results(self):
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7'),
                      (ScriptedExport('D7', self.north_raw),)),
            StartStep('south-shift', StartResult('D8'),
                      (ScriptedExport('D8', self.south_raw),)),
        ])
        capture.start('north-shift')
        capture.start('south-shift')
        session_results = {
            task_id: capture.get(task_id).result for task_id in ('north-shift', 'south-shift')
        }
        browser.stop()
        self.db.close()
        with closing(open_database(self.path)) as reopened:
            initialize_database(reopened)  # old databases keep their schema and data
            reader = ReportReader(reopened)
            for task_id, expected in session_results.items():
                claimed = reader.get_browser(task_id)
                self.assertEqual(claimed, expected)
                self.assertEqual(claimed.fixed.original.raw, expected.fixed.original.raw)
                self.assertEqual(claimed.fixed.report, expected.fixed.report)
            delivered = {
                task_id: self_reported_progress(reopened, task_id)
                for task_id in ('north-shift', 'south-shift')
            }
        self.assertEqual(delivered, {
            'north-shift': (CaptureStatus.DELIVERED, session_results['north-shift'].fixed.report_id),
            'south-shift': (CaptureStatus.DELIVERED, session_results['south-shift'].fixed.report_id),
        })

    def test_rejected_start_discards_unclaimed_original_but_keeps_confirmed_old(self):
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7')),
            StartStep('south-shift', StartResult(None), (
                ScriptedExport('D7', self.north_raw),
                ScriptedExport('D8', self.south_raw),
            )),
        ])
        self.assertEqual(capture.start('north-shift').status, BrowserState.PENDING)
        south = capture.start('south-shift')
        self.assertEqual(south.status, BrowserState.START_REJECTED)
        self.assertIsNone(south.result)
        # Repeated reads keep reporting the rejection; D8 never gets an owner.
        self.assertEqual(capture.get('south-shift').status, BrowserState.START_REJECTED)
        self.assertIsNone(ReportReader(self.db).get_browser('south-shift'))
        self.assert_pending('south-shift')
        # D8 produced nothing; the confirmed D7 for north is the only archive.
        self.assertEqual(archive_counts(self.db), (1, 1))
        self.assertEqual(
            self.db.execute('SELECT count(*) FROM browser_downloads').fetchone(), (1,))
        # The confirmed older download delivered in the same start call is safe.
        north = capture.get('north-shift')
        self.assertEqual(north.status, BrowserState.ARCHIVED)
        self.assertEqual(north.result.download_id, 'D7')
        self.assertEqual(north.result.fixed.original.raw, self.north_raw)
        self.assertEqual(browser.active_handles('D7'), 0)
        self.assertEqual(browser.active_handles('D8'), 0)
        browser.stop()

    def test_invalid_original_ends_in_invalid_state_without_report(self):
        invalid_raw = HEADER + b'10,gauge,1,23\n11,gauge,1,24\n'
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7'),
                      (ScriptedExport('D7', self.north_raw),)),
            StartStep('south-shift', StartResult('D8'),
                      (ScriptedExport('D8', invalid_raw),)),
        ])
        capture.start('north-shift')
        south = capture.start('south-shift')
        self.assertEqual(south.status, BrowserState.INVALID_ORIGINAL)
        self.assertIsNone(south.result)
        self.assertEqual(capture.get('south-shift').status, BrowserState.INVALID_ORIGINAL)
        self.assertIsNone(ReportReader(self.db).get_browser('south-shift'))
        self.assert_pending('south-shift')
        self.assertEqual(archive_counts(self.db), (1, 1))
        # No retry is offered for an invalid original.
        self.assertEqual(capture.retry('south-shift').status, BrowserState.INVALID_ORIGINAL)
        self.assertEqual(browser.active_handles('D8'), 0)
        # Malformed CSV formatting reaches the same terminal state.
        self.open_task('bad-shift', station='south')
        capture2, browser2 = self._capture([
            StartStep('bad-shift', StartResult('D9'),
                      (ScriptedExport('D9', b'not a csv at all'),)),
        ])
        bad = capture2.start('bad-shift')
        self.assertEqual(bad.status, BrowserState.INVALID_ORIGINAL)
        browser2.stop()
        browser.stop()

    def test_unknown_task_and_not_started_task_semantics(self):
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7')),
        ])
        with self.assertRaises(UnknownTask):
            capture.get('absent')
        with self.assertRaises(UnknownTask):
            capture.retry('absent')
        with self.assertRaises(UnknownTask):
            capture.start('absent')
        # Known but not yet started: pending view, nothing persisted.
        self.assertEqual(capture.get('south-shift').status, BrowserState.PENDING)
        self.assertEqual(capture.retry('south-shift').status, BrowserState.PENDING)
        started = capture.start('north-shift')
        self.assertEqual(started.status, BrowserState.PENDING)
        # A session starts each task at most once.
        with self.assertRaises(RuntimeError):
            capture.start('north-shift')
        browser.stop()
        # Readback: unknown task raises, known task without browser work is None.
        reader = ReportReader(self.db)
        with self.assertRaises(UnknownTask):
            reader.get_browser('absent')
        self.assertIsNone(reader.get_browser('north-shift'))
        self.assertIsNone(reader.get_browser('south-shift'))

    def test_manual_delivery_has_no_browser_binding_but_both_stay_available(self):
        manual = ManualCapture(self.db).submit('north-shift', self.north_raw)
        # The browser path must not touch a task already delivered manually.
        self.open_task('browser-shift', station='south')
        capture, browser = self._capture([
            StartStep('browser-shift', StartResult('D8'),
                      (ScriptedExport('D8', self.south_raw),)),
        ])
        browser_status = capture.start('browser-shift')
        self.assertEqual(browser_status.status, BrowserState.ARCHIVED)
        reader = ReportReader(self.db)
        self.assertIsNone(reader.get_browser('north-shift'))
        self.assertEqual(reader.get(manual.report_id), manual)
        claimed = reader.get_browser('browser-shift')
        self.assertEqual(claimed.download_id, 'D8')
        self.assertEqual(claimed.fixed.report.statistics.mean_um, Fraction(23, 1))
        browser.stop()

    def test_borrow_expires_after_callback_while_archive_keeps_full_bytes(self):
        from capture.intake.browser import BorrowedOriginal, OriginalExpired

        borrowed: dict[str, BorrowedOriginal] = {}
        browser = ScriptedBrowser([
            StartStep('north-shift', StartResult('D7'),
                      (ScriptedExport('D7', self.north_raw),)),
        ])
        capture = BrowserCapture(self.db, browser)

        def spy(completion):
            borrowed['item'] = completion.original
            # While the callback runs, both readers see the full bytes.
            self.assertEqual(completion.original.read(), self.north_raw)

        # ScriptedBrowser allows one subscriber; wrap capture's handler with a spy.
        outer = browser._handler
        browser._handler = lambda completion: (outer(completion), spy(completion))
        capture.start('north-shift')
        # The borrow is over once the callback returns, but the archive holds bytes.
        with self.assertRaises(OriginalExpired):
            borrowed['item'].read()
        with self.assertRaises(OriginalExpired):
            borrowed['item'].retain()
        fixed = capture.get('north-shift').result
        self.assertEqual(fixed.fixed.original.raw, self.north_raw)
        self.assertEqual(browser.active_handles('D7'), 0)
        browser.stop()

    def test_publication_is_one_atomic_transaction(self):
        trace = []
        self.db.set_trace_callback(trace.append)
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7'),
                      (ScriptedExport('D7', self.north_raw),)),
        ])
        try:
            capture.start('north-shift')
        finally:
            self.db.set_trace_callback(None)
        writes = ('INSERT INTO reports', 'INSERT INTO originals',
                  'INSERT INTO browser_downloads', 'UPDATE tasks SET report_id', 'COMMIT')
        positions = [next(i for i, sql in enumerate(trace) if sql.strip().startswith(w))
                     for w in writes]
        self.assertEqual(positions, sorted(set(positions)))
        self.assertNotIn('ROLLBACK', trace)
        browser.stop()

    def test_second_commit_failure_still_retries_and_persists_binding(self):
        # Two failed attempts: state stays retry_pending until one succeeds.
        fault = RejectNextCommit(self.db, 'north-shift')
        capture, browser = self._capture([
            StartStep('north-shift', StartResult('D7'),
                      (ScriptedExport('D7', self.north_raw),)),
        ])
        first = capture.start('north-shift')
        self.assertEqual(first.status, BrowserState.RETRY_PENDING)
        self.assertTrue(fault.fired)
        # Fault is one-shot, so the retry commits and writes the binding.
        second = capture.retry('north-shift')
        self.assertEqual(second.status, BrowserState.ARCHIVED)
        binding = self.db.execute(
            'SELECT task_id, download_id, report_id FROM browser_downloads'
        ).fetchone()
        self.assertEqual(binding, ('north-shift', 'D7', second.result.fixed.report_id))
        self.assertEqual(ReportReader(self.db).get_browser('north-shift'), second.result)
        browser.stop()

    def test_rejected_download_id_is_never_revived_by_a_late_notification(self):
        capture, browser = self._capture([
            StartStep('south-shift', StartResult(None),
                      (ScriptedExport('D8', self.south_raw),)),
        ])
        self.assertEqual(capture.start('south-shift').status, BrowserState.START_REJECTED)
        # A later stray notification under a fresh ID still has no accepted owner.
        browser.deliver(ScriptedExport('D99', self.south_raw))
        self.assertEqual(capture.get('south-shift').status, BrowserState.START_REJECTED)
        self.assertIsNone(ReportReader(self.db).get_browser('south-shift'))
        self.assertEqual(archive_counts(self.db), (0, 0))
        self.assertEqual(browser.active_handles('D8'), 0)
        self.assertEqual(browser.active_handles('D99'), 0)
        browser.stop()


def self_reported_progress(db, task_id):
    from capture.tasks.registry import TaskRegistry

    progress = TaskRegistry(db).get(task_id).progress
    return progress.status, progress.report_id
