"""
Tests for issue #69: audit recorded landings against the filesystem.

Before this check, ``files_processed`` was ``len(self._processed_files)`` and an
entry landed there whenever ``shutil.move`` did not raise. The documented
accounting identity was therefore self-referential -- it compared the tool's
counters to each other and never to ``backup/`` -- so a recorded move that
produced no file, or two records naming one file, still printed "Success!".

The tests below drive the real public entry point
(``FileProcessor.process_all_files``) and corrupt the archive between the move
and the audit, which is the only way to exercise the audit without a genuine
bug to trigger it. Each one was confirmed to FAIL with ``_verify_landings``
stubbed out to ``return`` immediately -- see the mutation note on
``TestAuditIsLoadBearing``.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from src.cli_interface import is_unqualified_success
from src.file_processor import FileProcessor, Settings
from tests.fixtures import make_exif_jpeg


class _LandingAuditCase(unittest.TestCase):
    """Shared export/backup scaffolding for the audit tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export = os.path.join(self.temp_dir, "export")
        self.backup = os.path.join(self.temp_dir, "backup")
        os.makedirs(self.export)
        self.processor = FileProcessor(Settings(export_dir=self.export, backup_dir=self.backup))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_photos(self, count, second_offset=0):
        """Create ``count`` JPEGs with distinct capture seconds."""
        return [
            make_exif_jpeg(
                os.path.join(self.export, f"photo_{i}.jpg"),
                date_time_original=(
                    f"2024:01:15 14:30:{second_offset + i:02d}"
                ),
            )
            for i in range(count)
        ]


class TestCleanRunReportsNoDiscrepancy(_LandingAuditCase):
    """The audit must be silent on a healthy run -- no false positives."""

    def test_clean_real_run_has_zero_discrepancies_and_stays_a_success(self):
        self._make_photos(3)

        results = self.processor.process_all_files(dry_run=False)

        self.assertEqual(results['files_processed'], 3)
        self.assertEqual(results['landing_discrepancies'], 0)
        self.assertEqual(results['landing_discrepancy_list'], [])
        self.assertTrue(
            is_unqualified_success(results),
            "a clean run must remain an unqualified success",
        )

    def test_every_recorded_landing_actually_exists(self):
        """The audit's own premise: recorded paths are real files."""
        self._make_photos(4)

        results = self.processor.process_all_files(dry_run=False)

        for record in results['processed_files']:
            self.assertTrue(
                os.path.isfile(record['final_path']),
                f"{record['final_path']} was recorded but is not on disk",
            )


class TestMissingLandingIsDetected(_LandingAuditCase):
    """A recorded landing absent from disk must be caught and reported."""

    def _run_deleting_one_landing_after_the_move(self):
        """Delete a file immediately after it is placed, before the audit runs.

        Patches ``_place_source_content`` -- the single seam every landing goes
        through -- so the file is genuinely moved (the run records a success in
        the ordinary way, with no error) and then removed behind the tool's back.
        This reproduces the *observable shape* of the defect the audit exists to
        catch, without needing a real bug to be present.
        """
        original = self.processor._place_source_content
        state = {'victim': None}

        def move_then_vanish(source, destination):
            original(source, destination)
            if state['victim'] is None:
                state['victim'] = destination
                os.remove(destination)

        with patch.object(
            self.processor, '_place_source_content', side_effect=move_then_vanish
        ):
            results = self.processor.process_all_files(dry_run=False)
        return results, state['victim']

    def test_missing_landing_is_reported_as_a_discrepancy(self):
        self._make_photos(3)

        results, victim = self._run_deleting_one_landing_after_the_move()

        self.assertEqual(
            results['landing_discrepancies'], 1,
            "the vanished landing was not detected",
        )
        self.assertIn(
            ('missing_landing', victim),
            results['landing_discrepancy_list'],
        )

    def test_missing_landing_withholds_unqualified_success(self):
        self._make_photos(3)

        results, _victim = self._run_deleting_one_landing_after_the_move()

        # The run recorded no failure at all -- this is exactly the case that
        # used to print "Success!" while a file was missing.
        self.assertEqual(results['files_failed'], 0)
        self.assertFalse(
            is_unqualified_success(results),
            "a run with a missing landing must not report unqualified success",
        )

    def test_files_processed_still_counts_the_move_it_recorded(self):
        """The audit reports the mismatch; it does not silently rewrite counts.

        ``files_processed`` continues to reflect what the tool recorded, so the
        discrepancy is visible as a disagreement rather than being papered over
        by adjusting the total -- and the accounting identity is left intact.
        """
        self._make_photos(3)

        results, _victim = self._run_deleting_one_landing_after_the_move()

        self.assertEqual(results['files_processed'], 3)
        self.assertEqual(results['landing_discrepancies'], 1)


class TestDuplicateLandingIsDetected(_LandingAuditCase):
    """Two records naming one path must be caught.

    Reservation is atomic (``O_CREAT | O_EXCL``, issue #6) so this cannot happen
    today; the assertion exists because it is the one failure shape that leaves
    counts and filenames looking entirely ordinary, which is precisely why it
    must not depend on the reservation staying correct forever.
    """

    def test_duplicate_final_path_is_reported(self):
        self._make_photos(2)

        results = self.processor.process_all_files(dry_run=False)
        self.assertEqual(results['landing_discrepancies'], 0)

        # Forge the state a reservation bug would produce: two records naming
        # one landing. Re-auditing must catch it.
        shared = self.processor._processed_files[0]['final_path']
        self.processor._processed_files[1]['final_path'] = shared
        self.processor._landing_discrepancies.clear()

        self.processor._verify_landings(dry_run=False)

        self.assertIn(
            ('duplicate_landing', shared),
            self.processor._landing_discrepancies,
        )

    def test_a_path_both_duplicated_and_absent_reports_each_problem_once(self):
        """Not once per record -- the two properties are reported independently."""
        self._make_photos(2)
        self.processor.process_all_files(dry_run=False)

        shared = self.processor._processed_files[0]['final_path']
        self.processor._processed_files[1]['final_path'] = shared
        os.remove(shared)
        self.processor._landing_discrepancies.clear()

        self.processor._verify_landings(dry_run=False)

        found = self.processor._landing_discrepancies
        self.assertEqual(
            found.count(('duplicate_landing', shared)), 1,
            "a duplicated path must be reported once, not once per record",
        )
        self.assertEqual(
            found.count(('missing_landing', shared)), 1,
            "an absent path must be reported once, not once per record",
        )


class TestQuarantinedLandingsAreVerified(_LandingAuditCase):
    """Quarantined files land in backup/corrupt/ and get the same audit (#58)."""

    def test_missing_quarantined_landing_is_detected(self):
        # A .jpg whose bytes do not decode is quarantined rather than archived.
        corrupt = os.path.join(self.export, "truncated.jpg")
        with open(corrupt, "wb") as handle:
            handle.write(b"not a jpeg at all")

        results = self.processor.process_all_files(dry_run=False)
        self.assertEqual(results['files_quarantined'], 1)
        self.assertEqual(results['landing_discrepancies'], 0)

        # Remove the quarantined file and re-audit.
        landed = self.processor._quarantined_files[0]['final_path']
        os.remove(landed)
        self.processor._landing_discrepancies.clear()

        self.processor._verify_landings(dry_run=False)

        self.assertIn(
            ('missing_landing', landed),
            self.processor._landing_discrepancies,
        )


class TestDryRunParity(_LandingAuditCase):
    """A dry run moves nothing, so it must never report a discrepancy (#10)."""

    def test_dry_run_reports_zero_discrepancies(self):
        self._make_photos(3)

        results = self.processor.process_all_files(dry_run=True)

        self.assertEqual(results['landing_discrepancies'], 0)
        self.assertEqual(results['landing_discrepancy_list'], [])
        self.assertTrue(is_unqualified_success(results))

    def test_dry_run_with_a_quarantine_candidate_reports_zero_discrepancies(self):
        """The trap the dry_run flag exists for.

        A dry run never appends to ``_processed_files``, but
        ``_quarantine_file`` DOES record an entry whose ``final_path`` was only
        resolved and never created. Inferring "nothing to audit" from an empty
        ``_processed_files`` would report a phantom discrepancy here.
        """
        corrupt = os.path.join(self.export, "truncated.jpg")
        with open(corrupt, "wb") as handle:
            handle.write(b"not a jpeg at all")

        results = self.processor.process_all_files(dry_run=True)

        self.assertEqual(results['files_quarantined'], 1)
        self.assertFalse(
            os.path.exists(
                self.processor._quarantined_files[0]['final_path']
            ),
            "a dry run must not have created the quarantine destination",
        )
        self.assertEqual(
            results['landing_discrepancies'], 0,
            "a dry run's resolved-but-uncreated quarantine path is not a "
            "discrepancy",
        )


class TestReuseContract(_LandingAuditCase):
    """Discrepancies are per-run state, reset like every other accumulator (#35)."""

    def test_a_second_clean_run_does_not_inherit_the_first_runs_discrepancy(self):
        self._make_photos(2)
        original = self.processor._place_source_content
        state = {'done': False}

        def move_then_vanish(source, destination):
            original(source, destination)
            if not state['done']:
                state['done'] = True
                os.remove(destination)

        with patch.object(
            self.processor, '_place_source_content', side_effect=move_then_vanish
        ):
            first = self.processor.process_all_files(dry_run=False)
        self.assertEqual(first['landing_discrepancies'], 1)

        # A fresh, healthy run on the SAME instance.
        self._make_photos(2, second_offset=30)
        second = self.processor.process_all_files(dry_run=False)

        self.assertEqual(
            second['landing_discrepancies'], 0,
            "the second run inherited the first run's discrepancy",
        )
        self.assertTrue(is_unqualified_success(second))

    def test_clear_processing_state_resets_discrepancies(self):
        self.processor._landing_discrepancies.append(('missing_landing', '/x'))

        self.processor.clear_processing_state()

        self.assertEqual(self.processor._landing_discrepancies, [])


class TestSummaryDoesNotAliasInternalState(_LandingAuditCase):
    """The reported list is a copy, per issue #37's no-aliasing precedent."""

    def test_mutating_the_reported_list_does_not_corrupt_the_accumulator(self):
        self._make_photos(1)
        results = self.processor.process_all_files(dry_run=False)

        results['landing_discrepancy_list'].append(('missing_landing', '/fake'))

        self.assertEqual(self.processor._landing_discrepancies, [])


class TestAuditIsLoadBearing(_LandingAuditCase):
    """Mutation check: the audit, not incidental behaviour, is what catches this.

    Recorded verification that these tests are not vacuous. With the body of
    ``_verify_landings`` replaced by a bare ``return``:

        TestMissingLandingIsDetected.test_missing_landing_is_reported_as_a_discrepancy   FAIL
        TestMissingLandingIsDetected.test_missing_landing_withholds_unqualified_success  FAIL
        TestMissingLandingIsDetected.test_files_processed_still_counts_...               FAIL
        TestDuplicateLandingIsDetected (both)                                            FAIL
        TestQuarantinedLandingsAreVerified.test_missing_quarantined_landing_is_detected   FAIL
        TestReuseContract.test_a_second_clean_run_does_not_inherit_...                   FAIL

    and with the ``if dry_run: return`` guard removed,
    ``TestDryRunParity.test_dry_run_with_a_quarantine_candidate_...`` FAILs.
    See the task report for the exact commands.
    """

    def test_summary_exposes_the_audit_keys(self):
        """Guards the contract the display layer and main.py read."""
        self._make_photos(1)

        results = self.processor.process_all_files(dry_run=False)

        self.assertIn('landing_discrepancies', results)
        self.assertIn('landing_discrepancy_list', results)


if __name__ == "__main__":
    unittest.main()
