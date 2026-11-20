import base64
import csv
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch
from uuid import uuid4

import graphene
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from core.models import User
from individual.models import Group, GroupIndividual, Individual

from household_validation import test_individual_workbook as fixtures
from household_validation.individual_upload import IndividualValidationUploadService
from household_validation.individual_snapshots import iter_selection, selection_rows
from household_validation.individual_workbook import INDIVIDUAL_SHEET, METADATA_SHEET
from household_validation.models import HouseholdValidationBatch, HouseholdValidationBatchRow
from household_validation.schema import Mutation, Query


class IndividualValidationUploadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.IndividualWorkbookTests.setUpTestData.__func__(cls)

    def setUp(self):
        fixtures.IndividualWorkbookTests.setUp(self)
        self.uploader = IndividualValidationUploadService(self.actor)

    generate = fixtures.IndividualWorkbookTests.generate
    workbook = staticmethod(fixtures.IndividualWorkbookTests.workbook)
    serialise = staticmethod(fixtures.IndividualWorkbookTests.serialise)

    def make_upload(self, decisions=("VERIFIED", "NOT_VERIFIED", None)):
        batch, content = self.generate(target_count=len(decisions))
        workbook = self.workbook(content)
        sheet = workbook[INDIVIDUAL_SHEET]
        headers = {cell.value: cell.column for cell in sheet[1]}
        for index, decision in enumerate(decisions, start=2):
            if decision is not None:
                sheet.cell(index, headers["decision"], decision)
                sheet.cell(index, headers["Turnover"], 25)
                sheet.cell(index, headers["Owns business"], "No")
                sheet.cell(index, headers["validation_notes"], "Checked")
        return batch, workbook, headers

    def test_applies_both_decisions_preserves_unrelated_json_and_skips_blank_rows(self):
        batch, workbook, _ = self.make_upload()
        selected = [row["individual_uuid"] for row in list(iter_selection(batch))]
        Individual.objects.filter(pk__in=selected).update(json_ext={"untouched": {"value": "keep"}})
        groups = list(Group.objects.values_list("pk", "json_ext"))
        memberships = list(GroupIndividual.objects.values_list("pk", "json_ext", "recipient_type"))
        result = self.uploader.upload(self.serialise(workbook), source_file_name="completed.xlsx")
        self.assertEqual(result["rows_applied"], 2)
        self.assertEqual(result["rows_skipped"], 1)
        self.assertEqual(result["individuals_updated"], 2)
        self.assertEqual(result["individuals_verified"], 1)
        self.assertEqual(result["individuals_not_verified"], 1)
        self.assertEqual(result["status"], "PROCESSED")
        for pk, expected in zip(selected, ("VERIFIED", "NOT_VERIFIED", None)):
            person = Individual.objects.get(pk=pk)
            self.assertEqual(person.json_ext.get("validation_status"), expected)
            self.assertEqual(person.json_ext["untouched"], {"value": "keep"})
        self.assertEqual(groups, list(Group.objects.values_list("pk", "json_ext")))
        self.assertEqual(memberships, list(GroupIndividual.objects.values_list("pk", "json_ext", "recipient_type")))
        self.assertEqual(batch.rows.filter(upload_attempt_id__isnull=False).count(), 3)
        self.assertEqual(len(self.uploader.history(batch.pk)), 1)

    def test_dry_run_has_identical_decisions_but_no_persistent_changes(self):
        batch, workbook, _ = self.make_upload()
        before = list(Individual.objects.values_list("pk", "json_ext", "version"))
        history = Individual.history.count()
        batch_history = batch.history.count()
        result = self.uploader.upload(self.serialise(workbook), dry_run=True)
        self.assertEqual(result["would_update"], 2)
        self.assertEqual(result["individuals_updated"], 0)
        self.assertIsNone(result["upload_attempt_id"])
        self.assertEqual(before, list(Individual.objects.values_list("pk", "json_ext", "version")))
        self.assertEqual(history, Individual.history.count())
        self.assertEqual(batch_history, batch.history.count())
        self.assertFalse(batch.rows.filter(upload_attempt_id__isnull=False).exists())
        self.assertEqual(self.uploader.history(batch.pk), [])

    def test_partial_success_logs_invalid_row_and_applies_the_rest(self):
        batch, workbook, headers = self.make_upload()
        workbook[INDIVIDUAL_SHEET].cell(3, headers["Turnover"], -1)
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["status"], "PARTIAL_SUCCESS")
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["rows_applied"], 1)
        self.assertEqual(result["rows_skipped"], 1)
        self.assertEqual(batch.rows.filter(status="ERROR").count(), 1)
        report, count = self.uploader.error_report(batch.pk, result["upload_attempt_id"])
        self.assertEqual(count, 1)
        [row] = list(csv.DictReader(StringIO(report)))
        self.assertEqual(row["Turnover"], "-1")
        self.assertIn("valid number", row["error_message"])

    def test_identical_retry_records_a_new_attempt_without_individual_history_writes(self):
        batch, workbook, _ = self.make_upload()
        content = self.serialise(workbook)
        first = self.uploader.upload(content)
        history = Individual.history.count()
        second = self.uploader.upload(content)
        self.assertNotEqual(first["upload_attempt_id"], second["upload_attempt_id"])
        self.assertEqual(second["individuals_updated"], 0)
        self.assertEqual(second["would_update"], 0)
        self.assertEqual(history, Individual.history.count())
        self.assertEqual(batch.rows.filter(upload_attempt_id__isnull=False).count(), 6)
        self.assertEqual(len(self.uploader.history(batch.pk)), 2)

    def test_corrections_preserve_previous_attempt_decisions(self):
        batch, workbook, headers = self.make_upload()
        first = self.uploader.upload(self.serialise(workbook))
        workbook[INDIVIDUAL_SHEET].cell(2, headers["decision"], "NOT_VERIFIED")
        second = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(second["individuals_updated"], 1)
        original = batch.rows.get(upload_attempt_id=first["upload_attempt_id"], row_number=2)
        corrected = batch.rows.get(upload_attempt_id=second["upload_attempt_id"], row_number=2)
        self.assertEqual(original.json_ext["decision"], "VERIFIED")
        self.assertEqual(corrected.json_ext["decision"], "NOT_VERIFIED")

    def test_upload_preserves_saved_selection_rows_and_keeps_attempts_separate(self):
        batch, workbook, _ = self.make_upload()
        before = list(selection_rows(batch).values_list("pk", "raw_row", "status", "version"))
        history = HouseholdValidationBatchRow.history.filter(upload_attempt_id__isnull=True).count()
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(before, list(selection_rows(batch).values_list("pk", "raw_row", "status", "version")))
        self.assertEqual(history, HouseholdValidationBatchRow.history.filter(upload_attempt_id__isnull=True).count())
        rows = self.uploader.attempt_rows(batch.pk, result["upload_attempt_id"])
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["status"] in ("APPLIED", "SKIPPED") for row in rows))

    def test_json_only_selection_cannot_be_uploaded(self):
        batch, workbook, _ = self.make_upload()
        batch.json_ext["selection"] = list(iter_selection(batch))
        batch.json_ext.pop("selection_storage")
        batch.save(user=self.actor)
        selection_rows(batch).delete()
        before = list(Individual.objects.values_list("pk", "json_ext"))
        with self.assertRaisesMessage(ValidationError, "Batch is not a generated individual list."):
            self.uploader.upload(self.serialise(workbook))
        self.assertEqual(before, list(Individual.objects.values_list("pk", "json_ext")))
        self.assertFalse(batch.rows.filter(upload_attempt_id__isnull=False).exists())

    def test_unknown_batches_do_not_create_new_batches_or_attempts(self):
        batch, workbook, _ = self.make_upload()
        workbook[METADATA_SHEET]["B1"] = str(uuid4())
        with self.assertRaises(ValidationError):
            self.uploader.upload(self.serialise(workbook))
        self.assertEqual(HouseholdValidationBatch.objects.count(), 1)
        self.assertFalse(batch.rows.filter(upload_attempt_id__isnull=False).exists())

    def test_mixed_batch_ids_fail_the_whole_file_without_applying_valid_rows(self):
        batch, workbook, headers = self.make_upload()
        workbook[INDIVIDUAL_SHEET].cell(2, headers["batch_id"], str(uuid4()))
        before = list(Individual.objects.values_list("pk", "json_ext"))
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["rows_applied"], 0)
        self.assertEqual(before, list(Individual.objects.values_list("pk", "json_ext")))
        self.assertEqual(batch.rows.filter(status="ERROR").count(), 3)

    def test_duplicate_and_foreign_individual_rows_cannot_update_other_people(self):
        batch, workbook, headers = self.make_upload(("VERIFIED", "NOT_VERIFIED"))
        sheet = workbook[INDIVIDUAL_SHEET]
        sheet.append([cell.value for cell in sheet[2]])
        sheet.cell(3, headers["individual_uuid"], str(self.active_member.pk))
        before = self.active_member.json_ext
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["rows_applied"], 0)
        self.assertEqual(result["errors"], 3)
        self.active_member.refresh_from_db()
        self.assertEqual(self.active_member.json_ext, before)
        self.assertFalse(batch.rows.filter(individual=self.active_member).exists())

    def test_deleted_individual_is_an_error_without_blocking_other_rows(self):
        batch, workbook, _ = self.make_upload(("VERIFIED", "VERIFIED"))
        row = list(iter_selection(batch))[1]
        Individual.objects.filter(pk=row["individual_uuid"]).update(is_deleted=True)
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["status"], "PARTIAL_SUCCESS")
        self.assertEqual(result["individuals_updated"], 1)
        self.assertTrue(any("deleted" in error for error in result["error_messages"]))
        attempt_rows = self.uploader.attempt_rows(batch.pk, result["upload_attempt_id"])
        self.assertTrue(attempt_rows[1]["redacted"])
        self.assertEqual(attempt_rows[1]["raw_row"], {})
        report, _ = self.uploader.error_report(batch.pk, result["upload_attempt_id"])
        self.assertNotIn(row["first_name"], report)

    def test_lost_access_is_a_row_error_and_history_redacts_the_unavailable_person(self):
        batch, workbook, _ = self.make_upload(("VERIFIED", "VERIFIED"))
        private = list(iter_selection(batch))[1]["individual_uuid"]
        with patch.object(Individual, "get_queryset", side_effect=lambda qs, user: qs.exclude(pk=private)):
            result = self.uploader.upload(self.serialise(workbook))
            self.assertEqual(result["status"], "PARTIAL_SUCCESS")
            self.assertEqual(result["individuals_updated"], 1)
            rows = self.uploader.attempt_rows(batch.pk, result["upload_attempt_id"])
            self.assertTrue(rows[1]["redacted"])
            self.assertIsNone(rows[1]["individual_id"])
            self.assertEqual(rows[1]["raw_row"], {})

    def test_current_source_changes_are_rejected_using_the_saved_rule(self):
        batch, workbook, _ = self.make_upload(("VERIFIED",))
        pk = list(iter_selection(batch))[0]["individual_uuid"]
        Individual.objects.filter(pk=pk).update(json_ext={"data_source": "other"})
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["errors"], 1)
        self.assertNotIn("validation_status", Individual.objects.get(pk=pk).json_ext)

    def test_people_who_join_an_active_household_are_rejected_at_commit(self):
        batch, workbook, _ = self.make_upload(("VERIFIED",))
        pk = list(iter_selection(batch))[0]["individual_uuid"]
        GroupIndividual.objects.bulk_create([GroupIndividual(
            id=uuid4(), group=self.group, individual_id=pk, user_created=self.actor, user_updated=self.actor,
        )])
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["individuals_updated"], 0)

    def test_current_geographic_scope_is_rechecked_without_reselecting_the_list(self):
        batch, workbook, _ = self.make_upload(("VERIFIED",))
        pk = list(iter_selection(batch))[0]["individual_uuid"]
        Individual.objects.filter(pk=pk).update(location=self.other)
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["individuals_updated"], 0)

    def test_changed_current_configuration_does_not_reinterpret_a_saved_file(self):
        batch, workbook, _ = self.make_upload(("VERIFIED",))
        from household_validation.apps import HouseholdValidationConfig
        with patch.object(HouseholdValidationConfig, "program_eligibility_rules", {}), patch.object(
            HouseholdValidationConfig, "program_specific_export_columns", {},
        ):
            result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["individuals_updated"], 1)
        self.assertEqual(result["status"], "PROCESSED")

    def test_unexpected_database_write_failure_rolls_back_the_whole_attempt(self):
        batch, workbook, _ = self.make_upload()
        original = Individual.save
        calls = []

        def fail_second(person, *args, **kwargs):
            calls.append(person.pk)
            if len(calls) == 2:
                raise RuntimeError("Unexpected failure")
            return original(person, *args, **kwargs)

        before = list(Individual.objects.values_list("pk", "json_ext"))
        history = Individual.history.count()
        with patch.object(Individual, "save", autospec=True, side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                self.uploader.upload(self.serialise(workbook))
        self.assertEqual(before, list(Individual.objects.values_list("pk", "json_ext")))
        self.assertEqual(history, Individual.history.count())
        self.assertFalse(batch.rows.filter(upload_attempt_id__isnull=False).exists())
        self.assertEqual(self.uploader.history(batch.pk), [])

    def test_file_errors_are_audited_without_creating_individual_updates(self):
        batch, workbook, _ = self.make_upload()
        workbook[INDIVIDUAL_SHEET].delete_cols(1)
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["rows_read"], 0)
        self.assertEqual(result["individuals_updated"], 0)
        self.assertEqual(batch.rows.filter(upload_attempt_id__isnull=False).count(), 1)
        report, count = self.uploader.error_report(batch.pk, result["upload_attempt_id"])
        self.assertEqual(count, 1)
        self.assertIn("Missing required", report)

    def test_reports_are_bound_to_the_requested_attempt(self):
        batch, workbook, headers = self.make_upload()
        workbook[INDIVIDUAL_SHEET].cell(2, headers["Turnover"], -1)
        first = self.uploader.upload(self.serialise(workbook))
        workbook[INDIVIDUAL_SHEET].cell(2, headers["Turnover"], 25)
        second = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(self.uploader.error_report(batch.pk, first["upload_attempt_id"])[1], 1)
        self.assertEqual(self.uploader.error_report(batch.pk, second["upload_attempt_id"])[1], 0)
        with self.assertRaises(ValidationError):
            self.uploader.error_report(batch.pk, uuid4())

    def test_only_the_corresponding_validation_right_is_required(self):
        batch, workbook, _ = self.make_upload()
        result = self.uploader.upload(self.serialise(workbook))
        self.actor.has_perms = lambda perms: list(perms) == ["958002"]
        self.assertTrue(self.uploader.upload(self.serialise(workbook), dry_run=True)["dry_run"])
        with self.assertRaises(PermissionDenied):
            self.uploader.history(batch.pk)
        self.actor.has_perms = lambda perms: list(perms) == ["958004"]
        self.uploader.error_report(batch.pk, result["upload_attempt_id"])
        with self.assertRaises(PermissionDenied):
            self.uploader.attempt_rows(batch.pk, result["upload_attempt_id"])

    def test_permissions_and_input_validation_precede_any_updates(self):
        _, workbook, _ = self.make_upload()
        content = self.serialise(workbook)
        for kwargs in ({"dry_run": "yes"}, {"source_file_name": "x" * 256}):
            with self.assertRaises(ValidationError):
                self.uploader.upload(content, **kwargs)
        self.actor.has_perms = lambda perms: False
        with self.assertRaises(PermissionDenied):
            self.uploader.upload(content)
        self.assertFalse(HouseholdValidationBatchRow.objects.filter(upload_attempt_id__isnull=False).exists())

    def test_unrelated_user_with_no_current_rows_cannot_read_batch_history(self):
        batch, workbook, _ = self.make_upload()
        result = self.uploader.upload(self.serialise(workbook))
        outsider = User.objects.create(username="unrelated-validation-user")
        outsider.has_perms = lambda perms: True
        service = IndividualValidationUploadService(outsider)
        with patch.object(User, "is_imis_admin", new_callable=PropertyMock, return_value=False), patch.object(
            Individual, "get_queryset", side_effect=lambda qs, user: qs.none(),
        ):
            for action in (lambda: service.history(batch.pk),
                           lambda: service.attempt_rows(batch.pk, result["upload_attempt_id"]),
                           lambda: service.error_report(batch.pk, result["upload_attempt_id"])):
                with self.assertRaises(PermissionDenied):
                    action()

    def test_invalid_base64_is_a_graphql_error_without_writes(self):
        batch, _, _ = self.make_upload()
        schema = graphene.Schema(query=Query, mutation=Mutation)
        result = schema.execute('''mutation {
          uploadIndividualValidationList(fileBase64: "invalid!") { batchId }
        }''', context_value=SimpleNamespace(user=self.actor))
        self.assertTrue(result.errors)
        self.assertFalse(batch.rows.filter(upload_attempt_id__isnull=False).exists())

    def test_graphql_upload_history_and_report_roundtrip(self):
        batch, workbook, headers = self.make_upload()
        workbook[INDIVIDUAL_SHEET].cell(3, headers["Turnover"], -1)
        schema = graphene.Schema(query=Query, mutation=Mutation)
        result = schema.execute('''mutation($file: String!) {
          uploadIndividualValidationList(fileBase64: $file, sourceFileName: "validated.xlsx") {
            batchId uploadAttemptId rowsApplied errors individualsUpdated status
          }
        }''', variable_values={"file": base64.b64encode(self.serialise(workbook)).decode("ascii")},
                                context_value=SimpleNamespace(user=self.actor))
        self.assertIsNone(result.errors)
        upload = result.data["uploadIndividualValidationList"]
        self.assertEqual(upload["status"], "PARTIAL_SUCCESS")
        result = schema.execute('''query($batch: UUID!, $attempt: UUID!) {
          individualValidationAttempts(batchId: $batch) { count attempts }
          individualValidationAttemptRows(batchId: $batch, uploadAttemptId: $attempt) { count items }
          individualValidationErrorReport(batchId: $batch, uploadAttemptId: $attempt) { errorCount fileBase64 }
        }''', variable_values={"batch": str(batch.pk), "attempt": upload["uploadAttemptId"]},
                                context_value=SimpleNamespace(user=self.actor))
        self.assertIsNone(result.errors)
        self.assertEqual(result.data["individualValidationAttempts"]["count"], 1)
        self.assertEqual(result.data["individualValidationAttemptRows"]["count"], 3)
        self.assertEqual(result.data["individualValidationErrorReport"]["errorCount"], 1)
        json.dumps(result.data)  # GenericScalar values must also survive HTTP JSON serialization.
