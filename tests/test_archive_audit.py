from contextlib import closing
from fractions import Fraction
from hashlib import sha256
import json

from capture.archive.audit import ReportAuditor
from capture.archive.database import initialize_database, open_database
from capture.archive.readback import ReportReader, UnknownReport
from capture.stations.catalog import StationCatalog
from capture.tasks.registry import TaskRegistry

from tests import DatabaseTestCase, HEADER, SHIFT, archive_counts


class ArchiveAuditTests(DatabaseTestCase):
    def _sign_two_shifts(self):
        self.stations.upsert('north', [('gauge', 1)])
        self.open_task('day')
        first = self.capture.submit('day', SHIFT)
        self.stations.upsert('north', [('gauge', 3)])
        self.open_task('evening', start=20, end=30)
        second = self.capture.submit('evening', HEADER + b'26,gauge,8,-2\n')
        return first, second

    def test_reopen_original(self):
        first, second = self._sign_two_shifts()
        self.stations.upsert('north', [('gauge', 5)], active=False)
        self.db.close()
        with closing(open_database(self.path)) as db:
            tasks = TaskRegistry(db)
            reports = ReportReader(db)
            auditor = ReportAuditor(db)
            for task_id, expected, scale in (('day', first, 1), ('evening', second, 3)):
                with self.subTest(task=task_id):
                    number = tasks.get(task_id).progress.report_id
                    self.assertEqual(number, expected.report_id)
                    fixed = reports.get(number)
                    self.assertEqual(fixed, expected)
                    self.assertEqual(fixed.task.multiplier_um, scale)
                    self.assertTrue(auditor.audit(number).passed)

    def test_history_keeps_each_shifts_bytes_and_measurement(self):
        first, second = self._sign_two_shifts()
        self.stations.upsert('north', [('gauge', 5)])
        self.db.close()
        with closing(open_database(self.path)) as db:
            reader = ReportReader(db)
            day = reader.get(first.report_id)
            evening = reader.get(second.report_id)
            self.assertNotEqual(day.report_id, evening.report_id)
            self.assertEqual(day.task.task_id, 'day')
            self.assertEqual(evening.task.task_id, 'evening')
            self.assertEqual(day.original.raw, SHIFT)
            self.assertEqual(evening.original.raw, HEADER + b'26,gauge,8,-2\n')
            self.assertEqual(day.report.statistics.mean_um, Fraction(41, 2))
            self.assertEqual(evening.report.statistics.mean_um, Fraction(-6, 1))
            self.assertEqual([sample.observation.line for sample in day.report.samples], [2, 3])
            self.assertEqual([sample.observation.line for sample in evening.report.samples], [2])
            with self.assertRaises(UnknownReport):
                reader.get(second.report_id + 1)
            with self.assertRaises(UnknownReport):
                ReportAuditor(db).audit(second.report_id + 1)

    def test_reinitialize(self):
        first, second = self._sign_two_shifts()
        definition = self.stations.upsert('north', [('gauge', 5), ('reference', 4)], active=False)
        initialize_database(self.db)
        initialize_database(self.db)
        self.db.close()
        with closing(open_database(self.path)) as db:
            self.assertEqual(StationCatalog(db).get('north'), definition)
            for result in (first, second):
                task = TaskRegistry(db).get(result.task.task_id)
                self.assertEqual(task.snapshot, result.task)
                self.assertEqual(task.progress.report_id, result.report_id)
                self.assertEqual(ReportReader(db).get(result.report_id), result)
                self.assertTrue(ReportAuditor(db).audit(result.report_id).passed)
            self.assertEqual(archive_counts(db), (2, 2))

    def test_corrupt_original(self):
        first, second = self._sign_two_shifts()
        self.assertTrue(self.auditor.audit(first.report_id).passed)
        self.assertTrue(self.auditor.audit(second.report_id).passed)
        altered = SHIFT.replace(b',17\n', b',97\n', 1)
        self.db.execute('UPDATE originals SET raw = ? WHERE report_id = ?', (altered, first.report_id))
        self.db.close()
        with closing(open_database(self.path)) as db:
            audit = ReportAuditor(db)
            self.assertFalse(audit.audit(first.report_id).passed)
            self.assertEqual(ReportReader(db).get(second.report_id), second)
            self.assertTrue(audit.audit(second.report_id).passed)

    def test_byte_identity(self):
        self.stations.upsert('north', [('gauge', 1)])
        self.open_task('first')
        self.open_task('second')
        original = HEADER + b'10,gauge,1,017\n'
        equal_value = HEADER + b'10,gauge,1,+17\n'
        first = self.capture.submit('first', original)
        second = self.capture.submit('second', equal_value)
        self.assertEqual(first.report, second.report)
        self.assertEqual(sum(left != right for left, right in zip(original, equal_value)), 1)
        self.db.execute('UPDATE originals SET raw = ? WHERE report_id = ?', (equal_value, first.report_id))
        self.db.close()
        with closing(open_database(self.path)) as db:
            fixed = ReportReader(db).get(first.report_id)
            self.assertEqual(fixed.original.raw, equal_value)
            self.assertEqual(fixed.original.sha256, sha256(original).hexdigest())
            verdict = ReportAuditor(db).audit(first.report_id)
            self.assertFalse(verdict.passed)
            self.assertEqual(verdict.reason, 'original digest mismatch')
            self.assertTrue(ReportAuditor(db).audit(second.report_id).passed)

    def test_report_drift(self):
        first, second = self._sign_two_shifts()
        encoded, = self.db.execute('SELECT report_json FROM reports WHERE report_id = ?',
                                   (first.report_id,)).fetchone()
        data = json.loads(encoded)
        data['samples'][0]['line'] = '3'
        self.db.execute('UPDATE reports SET report_json = ? WHERE report_id = ?',
                        (json.dumps(data), first.report_id))
        self.db.close()
        with closing(open_database(self.path)) as db:
            changed = ReportReader(db).get(first.report_id)
            self.assertEqual(changed.original, first.original)
            self.assertEqual(changed.report.statistics, first.report.statistics)
            self.assertEqual(changed.report.samples[0].observation.line, 3)
            verdict = ReportAuditor(db).audit(first.report_id)
            self.assertFalse(verdict.passed)
            self.assertEqual(verdict.reason, 'recomputed report differs from fixed report')
            self.assertEqual(ReportReader(db).get(second.report_id), second)
            self.assertTrue(ReportAuditor(db).audit(second.report_id).passed)
