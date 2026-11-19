from dataclasses import dataclass
import sqlite3

from capture.intake.csv_rows import ExportFormatError, parse_csv
from capture.intake.eligibility import ObservationEligibilityError, validate_observations
from capture.measurement.report import build_report

from .original import verify_original_digest
from .readback import ArchiveReadError, ReportReader


@dataclass(frozen=True)
class AuditResult:
    report_id: int
    passed: bool
    reason: str | None


class ReportAuditor:
    def __init__(self, db: sqlite3.Connection):
        self._reports = ReportReader(db)

    def audit(self, report_id: int) -> AuditResult:
        try:
            fixed = self._reports.get(report_id)
        except ArchiveReadError as exc:
            return AuditResult(report_id, False, str(exc))
        if not verify_original_digest(fixed.original):
            return AuditResult(report_id, False, 'original digest mismatch')
        try:
            rows = validate_observations(fixed.task, parse_csv(fixed.original.raw))
        except (ExportFormatError, ObservationEligibilityError) as exc:
            return AuditResult(report_id, False, f'archived original cannot be measured: {exc}')
        if build_report(fixed.task, rows) != fixed.report:
            return AuditResult(report_id, False, 'recomputed report differs from fixed report')
        return AuditResult(report_id, True, None)
