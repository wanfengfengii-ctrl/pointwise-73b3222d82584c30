from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from fractions import Fraction
import sqlite3
from threading import Barrier

from capture.archive.database import open_database
from capture.archive.publication import PublicationError, SubmissionConflict
from capture.archive.readback import ReportReader, UnknownReport
from capture.intake.browser import (
    OriginalExpired, ScriptedBrowser, ScriptedExport, StartResult, StartStep,
)
from capture.intake.csv_rows import ExportFormatError
from capture.intake.eligibility import ObservationEligibilityError
from capture.intake.manual import ManualCapture
from capture.tasks.registry import TaskRegistry, UnknownTask
from capture.tasks.status import CaptureStatus

from tests import DatabaseTestCase, HEADER, RejectNextCommit, SHIFT, archive_counts


class ManualCaptureTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.stations.upsert('north', [('gauge', 1)])
        self.open_task()

    def test_exact_report(self):
        fixed = self.capture.submit('shift', SHIFT)
        stats = fixed.report.statistics
        self.assertEqual((stats.count, stats.min_um, stats.max_um, stats.mean_um),
                         (2, 17, 24, Fraction(41, 2)))
        self.assertEqual([(sample.observation.line, sample.observation.seq, sample.micrometres)
                          for sample in fixed.report.samples], [(2, 1, 17), (3, 2, 24)])
        self.assertEqual(fixed.original.raw, SHIFT)
        self.assertEqual(self.tasks.get('shift').progress.report_id, fixed.report_id)
        self.assertEqual(self.reports.get(fixed.report_id), fixed)

    def test_multiline_source(self):
        channel = 'gauge\r\nreference'
        self.stations.upsert('north', [('gauge', 1), (channel, 1)])
        self.open_task('multiline', channel=channel)
        raw = (b'\xef\xbb\xbftime,channel,seq,value\r\n'
               b'10,"gauge\r\nreference",7,+017\r\n'
               b'19,"gauge\r\nreference",8,24')
        signed = self.capture.submit('multiline', raw)
        fixed = self.reports.get(signed.report_id)
        self.assertEqual(fixed.original.raw, raw)
        self.assertEqual([(row.observation.line, row.observation.seq, row.observation.channel)
                          for row in fixed.report.samples], [(2, 7, channel), (4, 8, channel)])
        self.assertEqual(fixed.report.statistics.mean_um, Fraction(41, 2))
        self.assertTrue(self.auditor.audit(fixed.report_id).passed)

    def test_signed_and_large(self):
        tick = 2**90
        self.stations.upsert('north', [('gauge', 3)])
        self.open_task('large', start=tick, end=tick + 10)
        raw = HEADER + f'{tick},gauge,+01,+9007199254740993\n{tick + 9},gauge,-2,-2\n'.encode()
        signed = self.capture.submit('large', raw)
        with closing(open_database(self.path)) as db:
            fixed = ReportReader(db).get(signed.report_id)
        self.assertEqual(fixed, signed)
        self.assertEqual(fixed.report.statistics.min_um, -6)
        self.assertEqual(fixed.report.statistics.max_um, 27021597764222979)
        self.assertEqual(fixed.report.statistics.mean_um, Fraction(27021597764222973, 2))
        self.assertEqual((fixed.report.coverage.first_tick, fixed.report.coverage.last_tick),
                         (tick, tick + 9))
        self.assertEqual([row.observation.seq for row in fixed.report.samples], [1, -2])

    def test_coverage_order(self):
        fixed = self.capture.submit('shift', HEADER + b'19,gauge,8,24\n10,gauge,7,17\n')
        self.assertEqual((fixed.report.coverage.first_tick, fixed.report.coverage.last_tick), (10, 19))
        self.assertEqual([(sample.observation.line, sample.observation.time)
                          for sample in fixed.report.samples], [(2, 19), (3, 10)])

    def test_header_only_export(self):
        signed = self.capture.submit('shift', HEADER)
        fixed = self.reports.get(signed.report_id)
        stats = fixed.report.statistics
        self.assertEqual((stats.count, stats.min_um, stats.max_um, stats.mean_um), (0, None, None, None))
        self.assertEqual((fixed.report.coverage.first_tick, fixed.report.coverage.last_tick), (None, None))
        self.assertEqual(fixed.report.samples, ())
        self.assertEqual(fixed.original.raw, HEADER)
        self.assertTrue(self.auditor.audit(fixed.report_id).passed)
        self.assertEqual(self.tasks.get('shift').progress.status, CaptureStatus.DELIVERED)

    def test_right_endpoint_rejected(self):
        with self.assertRaises(ObservationEligibilityError):
            self.capture.submit('shift', HEADER + b'10,gauge,1,17\n20,gauge,2,99\n')
        self.assert_pending()
        self.assertEqual(archive_counts(self.db), (0, 0))
        self.assertEqual(self.capture.submit('shift', SHIFT).report.statistics.count, 2)

    def test_channel_and_bounds(self):
        for invalid in (b'9,gauge,2,24\n', b'20,gauge,2,24\n', b'19,other,2,24\n'):
            with self.subTest(row=invalid):
                with self.assertRaises(ObservationEligibilityError):
                    self.capture.submit('shift', HEADER + b'10,gauge,1,17\n' + invalid)
                self.assert_pending()
                self.assertEqual(archive_counts(self.db), (0, 0))
        valid = SHIFT + b'19,gauge,3,0\n'
        result = self.capture.submit('shift', valid)
        self.assertEqual(result.report.statistics.count, 3)
        self.assertEqual([row.observation.time for row in result.report.samples], [10, 19, 19])

    def test_duplicate_sequence(self):
        with self.assertRaisesRegex(ObservationEligibilityError, 'duplicate sequence'):
            self.capture.submit('shift', HEADER + b'10,gauge,7,17\n19,gauge,+07,24\n')
        self.assert_pending()
        self.assertEqual(archive_counts(self.db), (0, 0))
        fixed = self.capture.submit('shift', HEADER + b'10,gauge,7,17\n19,gauge,8,24\n')
        self.assertEqual([(sample.observation.line, sample.observation.seq)
                          for sample in fixed.report.samples], [(2, 7), (3, 8)])
        self.assertEqual(archive_counts(self.db), (1, 1))

    def test_malformed_export(self):
        for raw in (b'', HEADER + b'10,\xff,1,17\n', b'channel,time,seq,value\n',
                    HEADER + b'10,gauge,1\n', HEADER + b'10,gauge,1,17,99\n',
                    HEADER + b'10,gauge,1,17.0\n', HEADER + b'10,"gauge,1,17\n'):
            with self.subTest(raw=raw):
                with self.assertRaises(ExportFormatError):
                    self.capture.submit('shift', raw)
                self.assert_pending()
                self.assertEqual(archive_counts(self.db), (0, 0))
        with self.assertRaises(UnknownTask):
            self.capture.submit('absent', SHIFT)

    def test_commit_retry(self):
        fault = RejectNextCommit(self.db, 'shift')
        trace = []
        self.db.set_trace_callback(trace.append)
        try:
            with self.assertRaises(PublicationError) as caught:
                self.capture.submit('shift', SHIFT)
        finally:
            self.db.set_trace_callback(None)
        self.assertIsInstance(caught.exception.__cause__, sqlite3.IntegrityError)
        self.assertTrue(fault.fired)
        writes = ('INSERT INTO reports', 'INSERT INTO originals', 'UPDATE tasks SET report_id', 'COMMIT')
        positions = [next(index for index, sql in enumerate(trace) if sql.strip().startswith(write))
                     for write in writes]
        self.assertEqual(positions, sorted(set(positions)))
        self.assertIn('ROLLBACK', trace)
        self.assert_pending()
        self.assertEqual(archive_counts(self.db), (0, 0))
        self.assertFalse(self.db.in_transaction)
        first = self.capture.submit('shift', SHIFT)
        again = self.capture.submit('shift', SHIFT)
        self.assertEqual(first.report_id, 1)
        self.assertEqual(first, again)
        self.assertEqual(self.tasks.get('shift').progress.report_id, first.report_id)
        self.assertEqual(archive_counts(self.db), (1, 1))

    def test_commit_visibility(self):
        self.open_task('earlier')
        fault = RejectNextCommit(self.db, 'shift')
        earlier = self.capture.submit('earlier', SHIFT)
        self.assertFalse(fault.fired)
        with closing(open_database(self.path)) as external:
            observer = TaskRegistry(external)
            reader = ReportReader(external)
            with self.assertRaises(PublicationError):
                self.capture.submit('shift', SHIFT)
            self.assertTrue(fault.fired)
            self.assertEqual(archive_counts(external), (1, 1))
            self.assertEqual(reader.get(earlier.report_id), earlier)
            pending = observer.get('shift').progress
            self.assertEqual(pending.status, CaptureStatus.PENDING)
            self.assertIsNone(pending.report_id)
            with self.assertRaises(UnknownReport):
                reader.get(earlier.report_id + 1)
            fixed = self.capture.submit('shift', SHIFT)
            self.assertEqual(fixed.report_id, earlier.report_id + 1)
            self.assertEqual(observer.get('shift').progress.report_id, fixed.report_id)
            self.assertEqual(reader.get(fixed.report_id), fixed)
            self.assertEqual(archive_counts(external), (2, 2))

    def test_raw_identity(self):
        original = HEADER + b'10,gauge,1,017\n'
        equivalent = HEADER + b'10,gauge,1,+17\n'
        initial = self.capture.submit('shift', original)
        self.assertEqual(self.capture.submit('shift', original), initial)
        with self.assertRaises(SubmissionConflict):
            self.capture.submit('shift', equivalent)
        stored = self.reports.get(initial.report_id)
        self.assertEqual(stored, initial)
        self.assertEqual(stored.original.raw, original)
        self.assertEqual(stored.report.statistics.mean_um, Fraction(17, 1))
        self.assertEqual(archive_counts(self.db), (1, 1))

    def test_concurrent_submission(self):
        alternative = HEADER + b'10,gauge,1,017\n19,gauge,2,24\n'
        for task_id, right in (('same', SHIFT), ('different', alternative)):
            with self.subTest(task_id=task_id):
                self.open_task(task_id)
                barrier = Barrier(2, timeout=5)

                def worker(raw):
                    with closing(open_database(self.path)) as db:
                        barrier.wait()
                        try:
                            return ManualCapture(db).submit(task_id, raw)
                        except SubmissionConflict:
                            return None

                with ThreadPoolExecutor(max_workers=2) as pool:
                    contenders = (pool.submit(worker, SHIFT), pool.submit(worker, right))
                    left, right_result = (future.result() for future in contenders)
                if task_id == 'same':
                    self.assertIsNotNone(left)
                    self.assertEqual(left, right_result)
                    winning = left
                else:
                    self.assertEqual(sum(result is not None for result in (left, right_result)), 1)
                    winning = left if left is not None else right_result
                    self.assertEqual(winning.original.raw, SHIFT if left is not None else alternative)
                self.assertEqual(self.tasks.get(task_id).progress.report_id, winning.report_id)
                self.assertEqual(self.reports.get(winning.report_id), winning)
                self.assertEqual(self.db.execute('SELECT count(*) FROM reports WHERE task_id = ?',
                                                 (task_id,)).fetchone(), (1,))
        self.assertEqual(archive_counts(self.db), (2, 2))


class BrowserPortTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.stations.upsert('north', [('gauge', 1)])
        self.stations.upsert('south', [('gauge', 1)])
        self.north = self.open_task('north-shift').snapshot
        self.south = self.open_task('south-shift', station='south').snapshot
        self.north_raw = HEADER + b'10,gauge,1,17\n'
        self.south_raw = HEADER + b'10,gauge,1,23\n'

    def test_early_interleaved_borrows_and_retention(self):
        browser = ScriptedBrowser([
            StartStep('north-shift', StartResult('D7')),
            StartStep('south-shift', StartResult('D8'), (
                ScriptedExport('D7', self.north_raw), ScriptedExport('D8', self.south_raw),
            )),
        ])
        borrows = []
        handles = []
        copies = []
        events = []

        def receive(completion):
            events.append(completion.download_id)
            borrows.append(completion.original)
            copies.append(completion.original.read())
            handles.append(completion.original.retain())

        browser.subscribe(receive)
        self.assertEqual(browser.start(self.north), StartResult('D7'))
        self.assertEqual(events, [])
        south_start = browser.start(self.south)
        events.append(south_start.download_id + ' accepted')
        self.assertEqual(events, ['D7', 'D8', 'D8 accepted'])
        self.assertEqual(browser.starts, ('north-shift', 'south-shift'))
        self.assertEqual(copies, [self.north_raw, self.south_raw])
        for borrowed in borrows:
            with self.assertRaises(OriginalExpired):
                borrowed.read()
            with self.assertRaises(OriginalExpired):
                borrowed.retain()
        browser.stop()
        self.assertEqual([handle.read() for handle in handles], copies)
        for download_id, handle in zip(('D7', 'D8'), handles):
            self.assertEqual(browser.active_handles(download_id), 1)
            handle.release()
            self.assertEqual(browser.active_handles(download_id), 0)
            with self.assertRaises(OriginalExpired):
                handle.read()

    def test_rejection_after_notification_and_late_delivery(self):
        browser = ScriptedBrowser([
            StartStep('north-shift', StartResult('D7')),
            StartStep('south-shift', StartResult(None), (ScriptedExport('D8', self.south_raw),)),
        ])
        completions = []
        browser.subscribe(completions.append)
        self.assertTrue(browser.start(self.north).accepted)
        self.assertFalse(browser.start(self.south).accepted)
        self.assertEqual([item.download_id for item in completions], ['D8'])
        browser.deliver(ScriptedExport('D7', self.north_raw))
        self.assertEqual([item.download_id for item in completions], ['D8', 'D7'])
        for completion in completions:
            with self.assertRaises(OriginalExpired):
                completion.original.read()
        with self.assertRaises(ValueError):
            browser.deliver(ScriptedExport('D7', self.north_raw))
        browser.stop()
        with self.assertRaises(RuntimeError):
            browser.deliver(ScriptedExport('D9', self.north_raw))
