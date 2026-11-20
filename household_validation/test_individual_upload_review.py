"""Regression coverage for upload review: useful errors, retries, notes and row locks."""
import csv
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Event, local
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.db import close_old_connections, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext

from core.models import User
from individual.models import Individual

from household_validation import test_individual_upload as fixtures
from household_validation.individual_snapshots import iter_selection
from household_validation.individual_upload import IndividualValidationUploadService
from household_validation.individual_workbook import INDIVIDUAL_SHEET, parse_individual_validation_workbook


class IndividualUploadReviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.IndividualValidationUploadTests.setUpTestData.__func__(cls)

    setUp = fixtures.IndividualValidationUploadTests.setUp
    generate = fixtures.IndividualValidationUploadTests.generate
    workbook = staticmethod(fixtures.IndividualValidationUploadTests.workbook)
    serialise = staticmethod(fixtures.IndividualValidationUploadTests.serialise)
    make_upload = fixtures.IndividualValidationUploadTests.make_upload

    def test_blank_notes_clear_previous_notes_only_on_decided_rows(self):
        for decision, note in (("VERIFIED", None), ("NOT_VERIFIED", "   ")):
            with self.subTest(decision=decision):
                batch, workbook, headers = self.make_upload((decision, None))
                selected = [row["individual_uuid"] for row in iter_selection(batch)]
                Individual.objects.filter(pk__in=selected).update(json_ext={
                    "validation_status": "NOT_VERIFIED", "validation_notes": "Not at home", "unrelated": "keep",
                })
                workbook[INDIVIDUAL_SHEET].cell(2, headers["validation_notes"]).value = note
                content = self.serialise(workbook)
                dry_run = self.uploader.upload(content, dry_run=True)
                self.assertEqual(dry_run["would_update"], 1)
                self.assertEqual(Individual.objects.get(pk=selected[0]).json_ext["validation_notes"], "Not at home")
                result = self.uploader.upload(content)
                self.assertEqual(result["rows_applied"], 1)
                self.assertEqual(result["rows_skipped"], 1)
                current = Individual.objects.get(pk=selected[0]).json_ext
                self.assertEqual(current["validation_status"], decision)
                self.assertIsNone(current["validation_notes"])
                self.assertEqual(current["unrelated"], "keep")
                self.assertEqual(Individual.objects.get(pk=selected[1]).json_ext["validation_notes"], "Not at home")
                history = Individual.history.count()
                self.assertEqual(self.uploader.upload(content)["individuals_updated"], 0)
                self.assertEqual(Individual.history.count(), history)

    def test_unlinked_identity_errors_remain_useful_without_exposing_submitted_data(self):
        for identity, expected in (("mistyped", "invalid individual UUID"),
                                   (str(uuid4()), "not in the saved selection"),
                                   (str(self.active_member.pk), "not in the saved selection")):
            with self.subTest(identity=identity):
                batch, workbook, headers = self.make_upload(("VERIFIED",))
                sheet = workbook[INDIVIDUAL_SHEET]
                sheet.cell(2, headers["individual_uuid"], identity)
                sheet.cell(2, headers["first_name"], "PRIVATE SUBMITTED NAME")
                result = self.uploader.upload(self.serialise(workbook))
                [row] = self.uploader.attempt_rows(batch.pk, result["upload_attempt_id"])["items"]
                self.assertIsNone(row["individual_id"])
                self.assertTrue(row["redacted"])
                self.assertEqual(row["raw_row"], {})
                self.assertEqual(row["json_ext"], {})
                self.assertIn(expected, row["error_message"])
                report, count = self.uploader.error_report(batch.pk, result["upload_attempt_id"])
                self.assertEqual(count, 1)
                [reported] = list(csv.DictReader(StringIO(report)))
                self.assertIn(expected, reported["error_message"])
                self.assertEqual(reported["individual_uuid"], "")
                self.assertNotIn("PRIVATE SUBMITTED NAME", report)
                self.assertNotIn(identity, report)

    def test_linked_inaccessible_errors_still_redact_messages_and_values(self):
        batch, workbook, headers = self.make_upload(("VERIFIED",))
        workbook[INDIVIDUAL_SHEET].cell(2, headers["Turnover"], -1)
        result = self.uploader.upload(self.serialise(workbook))
        with patch.object(Individual, "get_queryset", side_effect=lambda qs, user: qs.none()):
            [row] = self.uploader.attempt_rows(batch.pk, result["upload_attempt_id"])["items"]
            self.assertEqual(row["error_message"], "Row data is not currently accessible.")
            self.assertEqual(row["raw_row"], {})
            report, _ = self.uploader.error_report(batch.pk, result["upload_attempt_id"])
            self.assertIn("Row data is not currently accessible.", report)
            self.assertNotIn("valid number", report)

    def test_failed_retry_keeps_prior_batch_success_and_its_own_failed_attempt(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                batch, workbook, headers = self.make_upload(("VERIFIED", "VERIFIED"))
                sheet = workbook[INDIVIDUAL_SHEET]
                if partial:
                    sheet.cell(3, headers["Turnover"], -1)
                first = self.uploader.upload(self.serialise(workbook))
                expected = "PARTIAL_SUCCESS" if partial else "PROCESSED"
                self.assertEqual(first["status"], expected)
                sheet.cell(2, headers["batch_id"], str(uuid4()))
                content = self.serialise(workbook)
                history = len(self.uploader.history(batch.pk))
                self.assertEqual(self.uploader.upload(content, dry_run=True)["status"], "FAILED")
                self.assertEqual(len(self.uploader.history(batch.pk)), history)
                failed = self.uploader.upload(content)
                self.assertEqual(failed["status"], "FAILED")
                self.assertEqual(failed["rows_applied"], 0)
                batch.refresh_from_db()
                self.assertEqual(batch.status, expected)
                self.assertEqual(batch.json_ext["last_upload_status"], "FAILED")
                self.assertEqual(batch.json_ext["last_upload_attempt_id"], str(failed["upload_attempt_id"]))
                attempts = {item["upload_attempt_id"]: item for item in self.uploader.history(batch.pk)}
                self.assertEqual(attempts[str(first["upload_attempt_id"])]["status"], expected)
                self.assertEqual(attempts[str(failed["upload_attempt_id"])]["status"], "FAILED")
                summary = next(row for row in self.uploader.batches()["items"] if row["batch_id"] == batch.pk)
                self.assertEqual(summary["status"], expected)
                self.assertEqual(self.uploader.error_report(batch.pk, failed["upload_attempt_id"])[1], 2)
                sheet.cell(2, headers["batch_id"], str(batch.pk))
                sheet.cell(3, headers["Turnover"], 25)
                corrected = self.uploader.upload(self.serialise(workbook))
                self.assertEqual(corrected["status"], "PROCESSED")
                batch.refresh_from_db()
                self.assertEqual(batch.status, "PROCESSED")

    def test_blank_only_retry_does_not_promote_a_partial_batch(self):
        batch, workbook, headers = self.make_upload(("VERIFIED", "VERIFIED"))
        workbook[INDIVIDUAL_SHEET].cell(3, headers["Turnover"], -1)
        self.assertEqual(self.uploader.upload(self.serialise(workbook))["status"], "PARTIAL_SUCCESS")
        for number in (2, 3):
            workbook[INDIVIDUAL_SHEET].cell(number, headers["decision"]).value = None
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["rows_applied"], 0)
        self.assertEqual(result["rows_skipped"], 2)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "PARTIAL_SUCCESS")
        self.assertEqual(batch.json_ext["last_upload_status"], "PROCESSED")

    @skipUnlessDBFeature("has_select_for_update_of")
    def test_commit_locks_individual_rows_in_order_while_dry_run_takes_no_write_locks(self):
        _, workbook, _ = self.make_upload(("VERIFIED",))
        content = self.serialise(workbook)
        with CaptureQueriesContext(connection) as queries:
            self.uploader.upload(content, dry_run=True)
        self.assertFalse(any("FOR UPDATE" in query["sql"] for query in queries.captured_queries))
        with CaptureQueriesContext(connection) as queries:
            self.uploader.upload(content)
        locks = [query["sql"] for query in queries.captured_queries
                 if 'FOR UPDATE OF "individual_individual"' in query["sql"]]
        self.assertEqual(len(locks), 1)
        self.assertIn(f'ORDER BY "{Individual._meta.db_table}"."{Individual._meta.pk.column}" ASC', locks[0])


class IndividualUploadReviewConcurrencyTests(TransactionTestCase):
    available_apps = [config.name for config in apps.get_app_configs()]
    setUp = fixtures.IndividualValidationUploadConcurrencyTests.setUp
    generate = fixtures.IndividualValidationUploadTests.generate
    workbook = staticmethod(fixtures.IndividualValidationUploadTests.workbook)
    serialise = staticmethod(fixtures.IndividualValidationUploadTests.serialise)
    make_upload = fixtures.IndividualValidationUploadTests.make_upload

    def upload_in_thread(self, content, wrapper):
        close_old_connections()
        try:
            actor = User.objects.get(pk=self.actor.pk)
            actor.has_perms = lambda perms: True
            with connections["default"].execute_wrapper(wrapper):
                return IndividualValidationUploadService(actor).upload(content)
        finally:
            connections["default"].close()

    @skipUnlessDBFeature("has_select_for_update_of")
    def test_overlapping_batches_serialize_on_the_person_and_keep_both_business_changes(self):
        batch, workbook, _ = self.make_upload(("NOT_VERIFIED",))
        first_content = self.serialise(workbook)
        second_batch, workbook, headers = self.make_upload(("NOT_VERIFIED",))
        selected = next(iter_selection(batch))["individual_uuid"]
        self.assertEqual(next(iter_selection(second_batch))["individual_uuid"], selected)
        sheet = workbook[INDIVIDUAL_SHEET]
        for column in ("Turnover", "Owns business", "Business kind"):
            sheet.cell(2, headers[column]).value = None
        sheet.cell(2, headers["Description"], "Second batch answer")
        second_content = self.serialise(workbook)
        first_parsing, release_first, second_locking, second_parsing = Event(), Event(), Event(), Event()
        worker = local()

        def pause_first_parse(*args):
            if worker.number == 1:
                first_parsing.set()
                if not release_first.wait(15):
                    raise TimeoutError("First upload was not released")
            else:
                second_parsing.set()
            return parse_individual_validation_workbook(*args)

        def watch_lock(execute, sql, params, many, context):
            if worker.number == 2 and 'FOR UPDATE OF "individual_individual"' in sql:
                second_locking.set()
            return execute(sql, params, many, context)

        def upload(number, content):
            worker.number = number
            return self.upload_in_thread(content, watch_lock)

        with patch("household_validation.individual_upload.parse_individual_validation_workbook",
                   side_effect=pause_first_parse), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(upload, 1, first_content)
            try:
                self.assertTrue(first_parsing.wait(15), "First upload did not reach parsing")
                second = pool.submit(upload, 2, second_content)
                self.assertTrue(second_locking.wait(15), "Second upload did not request the individual lock")
                self.assertFalse(second_parsing.is_set(), "Second upload read the person before the first committed")
            finally:
                release_first.set()
            results = [first.result(timeout=20), second.result(timeout=20)]
        self.assertEqual([result["rows_applied"] for result in results], [1, 1])
        current = Individual.objects.get(pk=selected).json_ext
        self.assertEqual(current["turnover"], 25)
        self.assertEqual(current["description"], "Second batch answer")
        service = IndividualValidationUploadService(self.actor)
        self.assertEqual(len(service.history(batch.pk)), 1)
        self.assertEqual(len(service.history(second_batch.pk)), 1)

    @skipUnlessDBFeature("has_select_for_update_of")
    def test_waiting_upload_reads_the_registry_writers_latest_values_and_rechecks_source(self):
        for changed_source in (False, True):
            with self.subTest(changed_source=changed_source):
                batch, workbook, _ = self.make_upload(("VERIFIED",))
                selected = next(iter_selection(batch))["individual_uuid"]
                content = self.serialise(workbook)
                writer_locked, release_writer, uploader_locking = Event(), Event(), Event()

                def write_registry():
                    close_old_connections()
                    try:
                        with transaction.atomic():
                            person = Individual.objects.select_for_update().get(pk=selected)
                            person.json_ext = {"unrelated": {"latest": True}}
                            if changed_source:
                                person.json_ext["data_source"] = "other"
                            person.save(user=User.objects.get(pk=self.actor.pk))
                            writer_locked.set()
                            if not release_writer.wait(15):
                                raise TimeoutError("Registry writer was not released")
                    finally:
                        connections["default"].close()

                def watch_lock(execute, sql, params, many, context):
                    if 'FOR UPDATE OF "individual_individual"' in sql:
                        uploader_locking.set()
                    return execute(sql, params, many, context)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    writer = pool.submit(write_registry)
                    try:
                        self.assertTrue(writer_locked.wait(15), "Registry writer did not acquire the person lock")
                        upload = pool.submit(self.upload_in_thread, content, watch_lock)
                        self.assertTrue(uploader_locking.wait(15), "Upload did not request the person lock")
                    finally:
                        release_writer.set()
                    writer.result(timeout=20)
                    result = upload.result(timeout=20)
                current = Individual.objects.get(pk=selected).json_ext
                self.assertEqual(current["unrelated"], {"latest": True})
                self.assertEqual(result["rows_applied"], 0 if changed_source else 1)
                if changed_source:
                    self.assertNotIn("validation_status", current)
                    self.assertIn("no longer meets", result["error_messages"][0])
                else:
                    self.assertEqual(current["validation_status"], "VERIFIED")
