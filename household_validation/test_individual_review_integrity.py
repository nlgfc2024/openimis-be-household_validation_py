"""Registry drift and cross-subject protections for issued individual workbooks."""
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from individual.models import Individual

from household_validation import test_individual_workbook as fixtures
from household_validation import tests as household_fixtures
from household_validation.apps import DEFAULT_CONFIG, HouseholdValidationConfig
from household_validation.individual_selection import EligibleIndividualSelectionService
from household_validation.individual_snapshots import iter_selection, selection_rows
from household_validation.individual_workbook import INDIVIDUAL_SHEET
from household_validation.models import HouseholdValidationBatch, HouseholdValidationBatchRow
from household_validation.services import HouseholdValidationUploadService
from household_validation.upload import VALIDATION_LIST_SHEET, parse_validation_workbook


class IndividualReviewIntegrityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.IndividualWorkbookTests.setUpTestData.__func__(cls)

    def setUp(self):
        fixtures.IndividualWorkbookTests.setUp(self)

    generate = fixtures.IndividualWorkbookTests.generate
    workbook = staticmethod(fixtures.IndividualWorkbookTests.workbook)
    serialise = staticmethod(fixtures.IndividualWorkbookTests.serialise)
    edit = staticmethod(fixtures.IndividualWorkbookTests.edit)

    def test_editable_cells_start_blank_even_for_incompatible_registry_values(self):
        Individual.objects.update(json_ext={
            "turnover": 80, "owns": True, "kind": "Retail", "description": {"old": ["answer"]},
            "national_id": "001234",
        })
        batch, content = self.generate(target_count=1)
        snapshot = next(iter_selection(batch))
        sheet = self.workbook(content)[INDIVIDUAL_SHEET]
        headers = {cell.value: cell.column for cell in sheet[1]}
        for column in fixtures.COLUMNS:
            name = column["column_name"]
            self.assertIsNone(snapshot[name])
            self.assertIsNone(sheet.cell(2, headers[name]).value)
        self.assertEqual(snapshot["national_id"], "001234")
        self.assertIsNone(snapshot["decision"])

    def test_not_verified_cannot_echo_old_business_values_or_clear_live_dependants(self):
        Individual.objects.update(json_ext={"turnover": 20, "owns": "No", "kind": None})
        batch, content = self.generate(target_count=1)
        selected = next(iter_selection(batch))["individual_uuid"]
        current = {"turnover": 80, "owns": "Yes", "kind": "Farming", "unrelated": {"live": True}}
        Individual.objects.filter(pk=selected).update(json_ext=current)
        workbook = self.workbook(content)
        self.edit(workbook, decision="NOT_VERIFIED")
        result = self.service.parse(self.serialise(workbook))
        self.assertFalse(result.errors)
        self.assertFalse(result.rows[0].errors)
        self.assertEqual(result.rows[0].updates, {})
        self.assertEqual(Individual.objects.get(pk=selected).json_ext, current)

    def test_verified_requires_fresh_answers_instead_of_reusing_registry_answers(self):
        Individual.objects.update(json_ext={"turnover": 20, "owns": "No"})
        _, content = self.generate(target_count=1)
        workbook = self.workbook(content)
        self.edit(workbook, decision="VERIFIED")
        result = self.service.parse(self.serialise(workbook))
        self.assertTrue(any("Turnover is required" in error for error in result.rows[0].errors))
        self.assertTrue(any("Owns business is required" in error for error in result.rows[0].errors))
        self.assertEqual(result.rows[0].updates, {})

    def test_selected_identities_do_not_swap_when_eligibility_changes_after_selection(self):
        original_select = EligibleIndividualSelectionService.select
        expected = sorted([self.standalone.pk, self.second.pk, self.former_member.pk,
                           self.deleted_group_member.pk])[:2]

        def change_after_selection(selector, *args, **kwargs):
            result = original_select(selector, *args, **kwargs)
            Individual.objects.filter(pk=expected[0]).update(location=self.other)
            Individual.objects.filter(pk=self.outside.pk).update(location=self.village)
            return result

        with patch.object(EligibleIndividualSelectionService, "select", autospec=True,
                          side_effect=change_after_selection):
            batch, _ = self.generate(target_count=2)
        self.assertEqual([row["individual_uuid"] for row in iter_selection(batch)], [str(pk) for pk in expected])
        self.assertEqual(batch.json_ext["eligible_individuals"], 4)
        self.assertEqual(batch.json_ext["selected_individuals"], 2)

    def test_disappearing_selected_identity_rolls_back_instead_of_shortening_the_list(self):
        original_select = EligibleIndividualSelectionService.select

        def delete_after_selection(selector, *args, **kwargs):
            result = original_select(selector, *args, **kwargs)
            Individual.objects.filter(pk=self.standalone.pk).delete()
            return result

        with patch.object(EligibleIndividualSelectionService, "select", autospec=True,
                          side_effect=delete_after_selection), self.assertRaisesMessage(
            ValidationError, "Selected individuals disappeared",
        ):
            self.generate()
        self.assertFalse(HouseholdValidationBatch.objects.exists())
        self.assertFalse(HouseholdValidationBatchRow.objects.exists())
        self.assertFalse(HouseholdValidationBatchRow.history.exists())
        self.assertTrue(Individual.objects.filter(pk=self.standalone.pk).exists())

    def test_pasted_wrong_batch_ids_produce_one_file_error_and_per_row_errors(self):
        _, content = self.generate(target_count=3)
        workbook = self.workbook(content)
        sheet = workbook[INDIVIDUAL_SHEET]
        for number in range(2, 5):
            sheet.cell(number, 1, str(uuid4()))
        result = self.service.parse(self.serialise(workbook))
        self.assertEqual(result.errors, ["Workbook contains mixed or mismatched batch IDs."])
        self.assertEqual(len(result.rows), 3)
        self.assertTrue(all(any("batch ID does not match" in error for error in row.errors) for row in result.rows))
        self.assertEqual(result.valid_rows, [])

    def household_workbook(self, batch):
        workbook = household_fixtures.ValidationUploadParserTest()._upload_workbook()
        sheet = workbook[VALIDATION_LIST_SHEET]
        headers = {cell.value: cell.column for cell in sheet[1]}
        for key, value in {"batch_id": str(batch.pk), "group_uuid": str(self.group.pk),
                           "member_uuid": str(self.active_member.pk)}.items():
            sheet.cell(2, headers[key], value)
        return self.serialise(workbook)

    def test_household_upload_cannot_write_into_an_individual_batch_in_either_mode(self):
        batch, _ = self.generate(target_count=1)
        content = self.household_workbook(batch)
        before = list(HouseholdValidationBatch.objects.values())
        rows = list(selection_rows(batch).values())
        batch_history = HouseholdValidationBatch.history.count()
        row_history = HouseholdValidationBatchRow.history.count()
        pwp_columns = {"PWP": DEFAULT_CONFIG["program_specific_export_columns"]["PWP"]}
        with patch.object(HouseholdValidationConfig, "program_specific_export_columns", pwp_columns):
            self.assertEqual(len(parse_validation_workbook(content).rows), 1)
            for dry_run in (True, False):
                with self.subTest(dry_run=dry_run), self.assertRaisesMessage(
                    ValidationError, "Household uploads cannot use an individual validation batch",
                ):
                    HouseholdValidationUploadService(self.actor).upload(content, dry_run=dry_run)
        self.assertEqual(list(HouseholdValidationBatch.objects.values()), before)
        self.assertEqual(list(selection_rows(batch).values()), rows)
        self.assertEqual(HouseholdValidationBatch.history.count(), batch_history)
        self.assertEqual(HouseholdValidationBatchRow.history.count(), row_history)
        self.assertFalse(batch.rows.filter(upload_attempt_id__isnull=False).exists())

    def test_subject_guard_also_rejects_deleted_or_unsupported_individual_lists(self):
        batch, _ = self.generate(target_count=0)
        batch.is_deleted = True
        batch.json_ext.pop("selection_storage")
        batch.save(user=self.actor)
        parsed = SimpleNamespace(rows=[SimpleNamespace(values={"batch_id": str(batch.pk)})])
        with self.assertRaisesMessage(ValidationError, "Household uploads cannot use an individual validation batch"):
            HouseholdValidationUploadService(self.actor)._get_or_create_batch(parsed)

    def test_legacy_household_batch_can_still_be_reused(self):
        batch = HouseholdValidationBatch(json_ext={"legacy": True})
        batch.save(user=self.actor)
        parsed = SimpleNamespace(rows=[SimpleNamespace(values={"batch_id": str(batch.pk)})])
        reused = HouseholdValidationUploadService(self.actor)._get_or_create_batch(parsed)
        self.assertEqual(reused.pk, batch.pk)
        self.assertEqual(reused.json_ext, {"legacy": True})
        self.assertEqual(HouseholdValidationBatch.objects.count(), 1)
