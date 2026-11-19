from capture.stations.catalog import UnknownStation
from capture.stations.definition import InvalidStationDefinition
from capture.stations.lifecycle import StationInactive, UnknownChannel
from capture.tasks.registry import TaskRequestConflict, UnknownTask
from capture.tasks.request import OpenTaskRequest
from capture.tasks.status import CaptureStatus
from capture.tasks.window import InvalidWindow, TimeWindow

from tests import DatabaseTestCase, SHIFT


class StationTaskTests(DatabaseTestCase):
    def test_freeze_unit(self):
        self.stations.upsert('north', [('gauge', 2), ('reference', 1)])
        old = self.open_task('day')
        self.stations.upsert('north', [('gauge', 3)])
        new = self.open_task('night')
        self.assertEqual(self.tasks.get('day'), old)
        self.assertEqual(new.snapshot.multiplier_um, 3)
        self.assertEqual(old.snapshot.multiplier_um, 2)
        self.assertEqual(old.snapshot.request_identity(), ('north', 'gauge', 10, 20))
        self.assertEqual(new.progress.status, CaptureStatus.PENDING)

    def test_invalid_update_preserves_entire_definition(self):
        initial = self.stations.upsert('north', [('gauge', 2), ('reference', 4)])
        for channels in (
            [('gauge', 3), ('gauge', 4)], [('gauge', 3), ('reference', 0)],
            [('gauge', -1)], [('gauge', 2.5)], [('gauge', True)],
        ):
            with self.subTest(channels=channels):
                with self.assertRaises(InvalidStationDefinition):
                    self.stations.upsert('north', channels, active=False)
                self.assertEqual(self.stations.get('north'), initial)
        with self.assertRaises(InvalidStationDefinition):
            self.stations.upsert('south', [('gauge', 0)])
        with self.assertRaises(UnknownStation):
            self.stations.get('south')
        self.assertEqual(self.open_task().snapshot.multiplier_um, 2)
        self.assertEqual(self.open_task('reference', channel='reference').snapshot.multiplier_um, 4)

    def test_request_identity_preserves_snapshot_and_progress(self):
        self.stations.upsert('north', [('gauge', 2), ('reference', 1)])
        self.stations.upsert('south', [('gauge', 4)])
        request = OpenTaskRequest('shift', 'north', 'gauge', TimeWindow(10, 20))
        opened = self.tasks.open(request)
        self.stations.upsert('north', [('gauge', 3)])
        self.assertEqual(self.tasks.open(request), opened)
        fixed = self.capture.submit('shift', SHIFT)
        current = self.tasks.open(request)
        self.assertEqual(current.snapshot, opened.snapshot)
        self.assertEqual(current.progress.report_id, fixed.report_id)
        self.assertEqual(current.progress.status, CaptureStatus.DELIVERED)
        for request in (
            OpenTaskRequest('shift', 'south', 'gauge', TimeWindow(10, 20)),
            OpenTaskRequest('shift', 'north', 'reference', TimeWindow(10, 20)),
            OpenTaskRequest('shift', 'north', 'gauge', TimeWindow(11, 20)),
            OpenTaskRequest('shift', 'north', 'gauge', TimeWindow(10, 21)),
        ):
            with self.subTest(request=request), self.assertRaises(TaskRequestConflict):
                self.tasks.open(request)
        self.assertEqual(self.tasks.get('shift'), current)

    def test_reject_inactive_site(self):
        self.stations.upsert('north', [('gauge', 2)])
        original = self.open_task('day')
        self.stations.upsert('north', [('gauge', 3)], active=False)
        with self.assertRaises(StationInactive):
            self.open_task('next')
        with self.assertRaises(UnknownTask):
            self.tasks.get('next')
        self.assertEqual(self.tasks.get('day'), original)

    def test_cutover_keeps_existing_tasks_usable(self):
        self.stations.upsert('north', [('gauge', 2)])
        opened = self.open_task()
        self.stations.upsert('north', [('reference', 5)], active=False)
        self.assertEqual(self.open_task(), opened)
        fixed = self.capture.submit('shift', SHIFT)
        self.assertEqual(fixed.report.statistics.min_um, 34)
        self.assertEqual(fixed.report.statistics.max_um, 48)
        self.assertEqual(self.open_task().progress.report_id, fixed.report_id)
        with self.assertRaises(StationInactive):
            self.open_task('next', channel='reference')

    def test_reenable_uses_new_definition(self):
        self.stations.upsert('north', [('gauge', 2)])
        self.open_task('day')
        self.stations.upsert('north', [('gauge', 3)], active=False)
        with self.assertRaises(InvalidStationDefinition):
            self.stations.upsert('north', [('gauge', 0)], active=True)
        self.assertFalse(self.stations.get('north').active)
        self.stations.upsert('north', [('gauge', 5)], active=True)
        self.assertEqual(self.open_task('next').snapshot.multiplier_um, 5)
        self.assertEqual(self.tasks.get('day').snapshot.multiplier_um, 2)

    def test_invalid_scope_and_unknown_task(self):
        with self.assertRaises(UnknownStation):
            self.open_task()
        self.stations.upsert('north', [('gauge', 1)])
        with self.assertRaises(UnknownChannel):
            self.open_task(channel='missing')
        for start, end in ((20, 20), (21, 20), (10.0, 20), (True, 20)):
            with self.subTest(start=start, end=end), self.assertRaises(InvalidWindow):
                self.open_task(start=start, end=end)
        with self.assertRaises(UnknownTask):
            self.tasks.get('shift')
