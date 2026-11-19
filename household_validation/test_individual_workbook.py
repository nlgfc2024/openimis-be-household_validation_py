"""Saved-list and workbook contracts, including snapshot drift and tampering."""
import base64
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch
from uuid import uuid4

import graphene
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from openpyxl import load_workbook

from core.models import User
from individual.models import Individual

from household_validation import test_individual_selection as fixtures
from household_validation.apps import HouseholdValidationConfig
from household_validation.individual_lists import IndividualValidationListService
from household_validation.individual_selection import EligibleIndividualSelectionService
from household_validation.individual_snapshots import iter_selection, selection_rows
from household_validation.individual_workbook import (
    BASE_COLUMNS, INDIVIDUAL_SHEET, METADATA_SHEET, IndividualValidationListExporter, validate_individual_columns,
)
from household_validation.models import HouseholdValidationBatch, HouseholdValidationBatchRow
from household_validation.schema import Mutation, Query


COLUMNS = [
    {"key": "turnover", "column_name": "Turnover", "target_individual_json_ext_key": "turnover",
     "type": "number", "min": 0, "max": 100, "required": True},
    {"key": "owns", "column_name": "Owns business", "target_individual_json_ext_key": "owns",
     "type": "select", "options": ["Yes", "No"], "required": True},
    {"key": "kind", "column_name": "Business kind", "target_individual_json_ext_key": "kind",
     "type": "select", "options": ["Retail", "Farming"], "required": True,
     "depends_on": {"key": "owns", "equals": "Yes"}},
    {"key": "description", "column_name": "Description", "target_individual_json_ext_key": "description",
     "type": "text", "required": False},
]


DISPLAY_FIELDS = [
    {"column_name": "national_id", "json_ext_key": "national_id"},
    {"column_name": "gender", "json_ext_key": "gender"},
    {"column_name": "City", "location_type": "D"},
    {"column_name": "Ward", "location_type": "W"},
    {"column_name": "Neighbourhood", "location_type": "V"},
]


class IndividualWorkbookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.IndividualSelectionDatabaseTests.setUpTestData.__func__(cls)

    def setUp(self):
        for key, value in (
            ("program_eligibility_rules", fixtures.RULES),
            ("program_specific_export_columns", {"UMEP": deepcopy(COLUMNS)}),
            ("export_column_options_overrides", {}),
            ("individual_workbook_fields", {"UMEP": deepcopy(DISPLAY_FIELDS)}),
        ):
            context = patch.object(HouseholdValidationConfig, key, deepcopy(value))
            context.start()
            self.addCleanup(context.stop)
        admin = patch.object(User, "is_imis_admin", new_callable=PropertyMock, return_value=True)
        admin.start()
        self.addCleanup(admin.stop)
        self.actor.has_perms = lambda perms: True
        self.service = IndividualValidationListService(self.actor)

    def generate(self, **kwargs):
        return self.service.generate("UMEP", village_code=self.village.code, **kwargs)

    @staticmethod
    def workbook(content):
        return load_workbook(BytesIO(content))

    @staticmethod
    def serialise(workbook):
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    @staticmethod
    def edit(workbook, **values):
        sheet = workbook[INDIVIDUAL_SHEET]
        headers = {cell.value: cell.column for cell in sheet[1]}
        for header, value in values.items():
            sheet.cell(2, headers[header], value)

    def submitted_workbook(self, **values):
        batch, content = self.generate(target_count=1)
        workbook = self.workbook(content)
        self.edit(workbook, decision="VERIFIED", Turnover=25, **{"Owns business": "No"})
        self.edit(workbook, **values)
        return batch, workbook

    def test_generation_persists_exact_selection_rule_columns_and_filters(self):
        with patch.object(HouseholdValidationConfig, "export_column_options_overrides", {"kind": ["Approved"]}):
            batch, content = self.generate(target_count=2, exclude_validated=True)
        batch.refresh_from_db()
        data = batch.json_ext
        self.assertEqual(data["subject"], "INDIVIDUAL")
        self.assertEqual(data["benefit_plan_code"], "UMEP")
        self.assertEqual(data["rule"], {"subject": "INDIVIDUAL", "data_source": "ubr"})
        self.assertTrue(data["filters"]["exclude_validated"])
        self.assertEqual(data["filters"]["village_code"], self.village.code)
        self.assertEqual(data["eligible_individuals"], 4)
        self.assertEqual(data["selected_individuals"], 2)
        self.assertEqual(data["columns"][2]["options"], ["Approved"])
        expected = sorted([str(self.standalone.pk), str(self.second.pk),
                           str(self.former_member.pk), str(self.deleted_group_member.pk)])[:2]
        self.assertNotIn("selection", data)
        self.assertEqual(data["display_fields"], DISPLAY_FIELDS)
        self.assertEqual([row["individual_uuid"] for row in iter_selection(batch)], expected)
        self.assertEqual(selection_rows(batch).count(), 2)
        self.assertEqual(selection_rows(batch).filter(upload_attempt_id__isnull=True, status="PENDING").count(), 2)
        self.assertEqual(HouseholdValidationBatchRow.history.filter(batch_id=batch.pk).count(), 2)
        sheet = self.workbook(content)[INDIVIDUAL_SHEET]
        self.assertEqual([sheet.cell(i, 2).value for i in (2, 3)], expected)

    def test_export_and_preview_stay_frozen_after_registry_and_config_changes(self):
        batch, content = self.generate(target_count=2)
        initial = self.service.preview(batch.pk)
        ids = [row["individual_uuid"] for row in initial["items"]]
        Individual.objects.filter(pk__in=ids).update(first_name="Changed", json_ext={"turnover": 99})
        self.village.name = "Changed village"
        self.village.save()
        with patch.object(HouseholdValidationConfig, "program_eligibility_rules", {}), patch.object(
            HouseholdValidationConfig, "program_specific_export_columns", {"UMEP": []},
        ), patch.object(EligibleIndividualSelectionService, "select", side_effect=AssertionError("Live reselection")):
            page = self.service.preview(batch.pk)
            _, exported = self.service.export(batch.pk)
        self.assertEqual(page, initial)
        old = list(self.workbook(content)[INDIVIDUAL_SHEET].values)
        new = list(self.workbook(exported)[INDIVIDUAL_SHEET].values)
        self.assertEqual(old, new)

    def test_saved_pagination_uses_database_pages_without_reselection(self):
        batch, _ = self.generate(target_count=3)
        with patch.object(EligibleIndividualSelectionService, "select", side_effect=AssertionError("Live selection")), \
                CaptureQueriesContext(connection) as queries:
            first = self.service.preview(batch.pk, first=1)
            second = self.service.preview(batch.pk, first=1, offset=1)
        self.assertEqual(first["eligible_individuals"], 4)
        self.assertEqual(second["selected_individuals"], 3)
        self.assertNotEqual(first["items"], second["items"])
        page_queries = [query["sql"] for query in queries
                        if '"household_validation_householdvalidationbatchrow"."raw_row"' in query["sql"]]
        self.assertEqual(len(page_queries), 2)
        self.assertIn("LIMIT 1", page_queries[0])
        self.assertIn("LIMIT 1 OFFSET 1", page_queries[1])

    def test_workbook_has_individual_fields_decisions_and_editable_configured_columns(self):
        _, content = self.generate(target_count=1)
        workbook = self.workbook(content)
        sheet = workbook[INDIVIDUAL_SHEET]
        headers = [cell.value for cell in sheet[1]]
        self.assertNotIn("group_uuid", headers)
        self.assertNotIn("primary_worker", headers)
        self.assertTrue(sheet.protection.sheet)
        for header in ("decision", "validation_notes", "Turnover"):
            self.assertFalse(sheet.cell(2, headers.index(header) + 1).protection.locked)
        self.assertTrue(sheet.cell(2, headers.index("first_name") + 1).protection.locked)
        self.assertEqual(workbook[METADATA_SHEET].sheet_state, "hidden")
        formulas = [value.formula1 for value in sheet.data_validations.dataValidation]
        self.assertIn('"VERIFIED,NOT_VERIFIED"', formulas)

    def test_blank_decisions_skip_required_and_invalid_extra_values_without_updates(self):
        _, content = self.generate(target_count=1)
        workbook = self.workbook(content)
        self.edit(workbook, Turnover="invalid", validation_notes="Ignored")
        result = self.service.parse(self.serialise(workbook))
        self.assertFalse(result.errors)
        self.assertFalse(result.rows[0].errors)
        self.assertIsNone(result.rows[0].decision)
        self.assertEqual(result.rows[0].updates, {})
        self.assertIsNone(result.rows[0].notes)

    def test_valid_decisions_roundtrip_and_parsing_never_writes(self):
        for decision in ("VERIFIED", " not_verified "):
            with self.subTest(decision=decision):
                batch, workbook = self.submitted_workbook(decision=decision, validation_notes="Checked")
                before = list(Individual.objects.values_list("pk", "json_ext"))
                row_history = HouseholdValidationBatchRow.history.count()
                result = self.service.parse(self.serialise(workbook))
                self.assertFalse(result.errors)
                self.assertFalse(result.rows[0].errors)
                self.assertEqual(result.rows[0].decision, decision.strip().upper())
                self.assertEqual(result.rows[0].updates["turnover"], 25.0)
                self.assertEqual(result.rows[0].updates["owns"], "No")
                self.assertIsNone(result.rows[0].updates["kind"])
                self.assertEqual(result.rows[0].notes, "Checked")
                self.assertEqual(before, list(Individual.objects.values_list("pk", "json_ext")))
                self.assertEqual(batch.rows.count(), 1)
                self.assertFalse(batch.rows.filter(upload_attempt_id__isnull=False).exists())
                self.assertEqual(HouseholdValidationBatchRow.history.count(), row_history)

    def test_snapshot_parser_does_not_accept_current_or_other_programmes_looser_rules(self):
        _, workbook = self.submitted_workbook(Turnover=500, **{"Owns business": "Yes", "Business kind": "Other"})
        loose = deepcopy(COLUMNS)
        loose[0]["max"] = 1000
        loose[2]["options"] = ["Other"]
        with patch.object(HouseholdValidationConfig, "program_specific_export_columns", {
            "UMEP": loose, "RMEP": loose,
        }), patch.object(HouseholdValidationConfig, "export_column_options_overrides", {"kind": ["Other"]}):
            result = self.service.parse(self.serialise(workbook))
        self.assertEqual(result.rows[0].updates, {})
        self.assertTrue(any("Turnover" in error for error in result.rows[0].errors))
        self.assertTrue(any("Business kind" in error for error in result.rows[0].errors))

    def test_required_dependency_options_and_numeric_errors(self):
        cases = [
            ({"Turnover": ""}, "required"), ({"Turnover": -1}, "valid number"),
            ({"Turnover": "NaN"}, "valid number"), ({"Owns business": "Maybe"}, "valid option"),
            ({"Owns business": "Yes"}, "Business kind is required"),
            ({"Business kind": "Retail"}, "only available"),
        ]
        for values, error in cases:
            with self.subTest(values=values):
                _, workbook = self.submitted_workbook(**values)
                result = self.service.parse(self.serialise(workbook))
                self.assertTrue(any(error in message for message in result.rows[0].errors))
                self.assertEqual(result.rows[0].updates, {})

    def test_decisions_reject_rejected_yes_no_and_formulas(self):
        for value in ("REJECTED", "YES", "NO", True, "=1+1"):
            with self.subTest(value=value):
                _, workbook = self.submitted_workbook(decision=value)
                result = self.service.parse(self.serialise(workbook))
                self.assertTrue(result.rows[0].errors)
                self.assertFalse(result.valid_rows)

    def test_protected_identity_and_out_of_selection_changes_are_row_errors(self):
        for values in ({"individual_uuid": str(uuid4())}, {"individual_uuid": "not-a-uuid"},
                       {"first_name": "Tampered"}, {"national_id": "Changed"}):
            with self.subTest(values=values):
                _, workbook = self.submitted_workbook(**values)
                result = self.service.parse(self.serialise(workbook))
                self.assertTrue(result.rows[0].errors)
                self.assertEqual(result.rows[0].updates, {})

    def test_duplicate_individuals_invalidate_both_rows(self):
        _, workbook = self.submitted_workbook()
        sheet = workbook[INDIVIDUAL_SHEET]
        sheet.append([cell.value for cell in sheet[2]])
        result = self.service.parse(self.serialise(workbook))
        self.assertEqual(result.rows_read, 2)
        self.assertFalse(result.valid_rows)
        self.assertTrue(all(row.errors and not row.updates for row in result.rows))

    def test_unknown_and_mixed_batches_rejected(self):
        _, workbook = self.submitted_workbook()
        workbook[METADATA_SHEET]["B1"] = str(uuid4())
        self.assertTrue(self.service.parse(self.serialise(workbook)).errors)
        _, workbook = self.submitted_workbook(batch_id=str(uuid4()))
        result = self.service.parse(self.serialise(workbook))
        self.assertTrue(result.errors)
        self.assertFalse(result.valid_rows)

    def test_missing_duplicate_extra_headers_and_metadata_are_file_errors(self):
        for operation in ("missing", "duplicate", "extra", "metadata", "subject", "programme"):
            with self.subTest(operation=operation):
                _, workbook = self.submitted_workbook()
                sheet = workbook[INDIVIDUAL_SHEET]
                if operation == "missing":
                    sheet.delete_cols(1)
                elif operation == "duplicate":
                    sheet.cell(1, 2, "batch_id")
                elif operation == "extra":
                    sheet.cell(1, sheet.max_column + 1, "Other programme column")
                elif operation == "metadata":
                    del workbook[METADATA_SHEET]
                elif operation == "subject":
                    workbook[METADATA_SHEET]["B2"] = "HOUSEHOLD"
                else:
                    workbook[METADATA_SHEET]["B3"] = "PWP"
                self.assertTrue(self.service.parse(self.serialise(workbook)).errors)

    def test_invalid_file_is_reported(self):
        self.assertTrue(self.service.parse(b"not an xlsx file").errors)

    def test_current_access_is_rechecked_and_unknown_or_household_batches_rejected(self):
        batch, content = self.generate(target_count=1)
        unrelated = User.objects.create(username="unrelated-list-reader")
        unrelated.has_perms = lambda perms: True
        service = IndividualValidationListService(unrelated)
        with patch.object(User, "is_imis_admin", new_callable=PropertyMock, return_value=False), patch.object(
            Individual, "get_queryset", side_effect=lambda qs, user: qs.none(),
        ):
            for action in (lambda: service.preview(batch.pk), lambda: service.export(batch.pk),
                           lambda: service.parse(content)):
                with self.assertRaises(PermissionDenied):
                    action()
        with self.assertRaises(ValidationError):
            self.service.preview(uuid4())
        batch.json_ext = {}
        batch.save(user=self.actor)
        with self.assertRaises(ValidationError):
            self.service.preview(batch.pk)

    def test_permissions_are_checked_for_generation_reads_and_parsing(self):
        batch, content = self.generate(target_count=1)
        self.actor.has_perms = lambda perms: False
        for action in (lambda: self.generate(), lambda: self.service.preview(batch.pk),
                       lambda: self.service.export(batch.pk), lambda: self.service.parse(content)):
            with self.assertRaises(PermissionDenied):
                action()
        with self.assertRaises(PermissionDenied):
            IndividualValidationListService(None).generate("UMEP")

    def test_individual_snapshots_are_not_exposed_by_legacy_household_history(self):
        batch, _ = self.generate(target_count=1)
        legacy = HouseholdValidationBatch(json_ext={"legacy": True})
        legacy.save(user=self.actor)
        info = SimpleNamespace(context=SimpleNamespace(user=self.actor))
        history = Query.resolve_household_validation_batches(None, info)
        self.assertEqual([item.pk for item in history.batches], [legacy.pk])
        # Also ensure future individual upload rows cannot leak through the
        # existing household row/report queries.
        attempt = uuid4()
        row = HouseholdValidationBatchRow(
            batch=batch, row_number=2, upload_attempt_id=attempt,
            individual_id=next(iter_selection(batch))["individual_uuid"],
            status="ERROR", raw_row={"first_name": "Private"},
        )
        row.save(user=self.actor)
        rows = Query.resolve_household_validation_batch_rows(None, info, batch_id=batch.pk)
        self.assertEqual(rows.rows, [])
        rejected = Query.resolve_household_validation_rejected_batch_rows(
            None, info, batch_id=batch.pk, upload_attempt_id=attempt,
        )
        self.assertEqual(rejected.rows, [])
        with self.assertRaises(HouseholdValidationBatch.DoesNotExist):
            Query.resolve_household_validation_batch_error_report(None, info, batch_id=batch.pk)

    def test_empty_list_roundtrip_and_pagination_limits(self):
        batch, content = self.generate(target_count=0)
        result = self.service.parse(content)
        self.assertFalse(result.errors)
        self.assertEqual(result.rows_read, 0)
        self.assertEqual(self.service.preview(batch.pk)["selected_individuals"], 0)
        for first, offset in ((101, 0), (-1, 0), (20, -1)):
            with self.assertRaises(ValidationError):
                self.service.preview(batch.pk, first, offset)

    def test_failed_export_rolls_back_batch_creation(self):
        with patch.object(IndividualValidationListExporter, "export_bytes", side_effect=ValueError("Failed")):
            with self.assertRaises(ValueError):
                self.generate()
        self.assertFalse(HouseholdValidationBatch.objects.exists())
        self.assertFalse(HouseholdValidationBatchRow.objects.exists())
        self.assertFalse(HouseholdValidationBatchRow.history.exists())

    def test_snapshot_data_is_exported_as_literal_text_not_formulas(self):
        Individual.objects.filter(pk=self.standalone.pk).update(first_name="=1+1", json_ext={"national_id": "00123"})
        batch, _ = self.generate()
        _, content = self.service.export(batch.pk)
        sheet = self.workbook(content)[INDIVIDUAL_SHEET]
        headers = [cell.value for cell in sheet[1]]
        row = next(row for row in sheet.iter_rows(min_row=2) if row[1].value == str(self.standalone.pk))
        self.assertEqual(row[headers.index("first_name")].data_type, "s")
        self.assertEqual(row[headers.index("national_id")].value, "00123")

    def test_invalid_configured_columns_fail_before_saving_batch(self):
        for change in ({"column_name": "decision"}, {"type": "unsupported"},
                       {"target_individual_json_ext_key": "validation_status"},
                       {"depends_on": {"key": "missing", "equals": "Yes"}}):
            columns = deepcopy(COLUMNS)
            columns[0].update(change)
            with self.subTest(change=change), patch.object(
                HouseholdValidationConfig, "program_specific_export_columns", {"UMEP": columns},
            ), self.assertRaises(ValidationError):
                self.generate()
        self.assertFalse(HouseholdValidationBatch.objects.exists())

    def test_individual_generation_cannot_use_household_programme(self):
        with self.assertRaises(ValidationError):
            self.service.generate("PWP")

    def test_graphql_generation_saved_preview_and_export_share_the_same_batch(self):
        schema = graphene.Schema(query=Query, mutation=Mutation)
        result = schema.execute('''mutation { generateIndividualValidationList(
          benefitPlanCode: "UMEP", villageCode: "UV-V", targetCount: 2, excludeValidated: true
        ) { batchId fileBase64 selectedIndividuals eligibleIndividuals } }''',
                                context_value=SimpleNamespace(user=self.actor))
        self.assertIsNone(result.errors)
        generated = result.data["generateIndividualValidationList"]
        result = schema.execute('''query($id: UUID!) {
          individualValidationList(batchId: $id, first: 1) {
            batchId eligibleIndividuals selectedIndividuals columns items hasNextPage
          }
          individualValidationListExport(batchId: $id) { batchId fileBase64 selectedIndividuals }
        }''', variable_values={"id": generated["batchId"]}, context_value=SimpleNamespace(user=self.actor))
        self.assertIsNone(result.errors)
        self.assertEqual(result.data["individualValidationList"]["selectedIndividuals"], 2)
        self.assertEqual(len(result.data["individualValidationList"]["items"]), 1)
        exported = base64.b64decode(result.data["individualValidationListExport"]["fileBase64"])
        original = base64.b64decode(generated["fileBase64"])
        self.assertEqual(list(self.workbook(exported)[INDIVIDUAL_SHEET].values),
                         list(self.workbook(original)[INDIVIDUAL_SHEET].values))

    def test_shared_column_validator_accepts_dependency_and_override_definitions(self):
        validate_individual_columns(deepcopy(COLUMNS))
        self.assertTrue(set(BASE_COLUMNS).isdisjoint({"group_uuid", "primary_worker"}))

    def test_one_deleted_person_does_not_block_preview_export_or_other_parse_rows(self):
        batch, content = self.generate(target_count=3)
        selected = list(iter_selection(batch))
        deleted = selected[1]["individual_uuid"]
        Individual.objects.filter(pk=deleted).update(is_deleted=True)
        page = self.service.preview(batch.pk)
        self.assertEqual(page["selected_individuals"], 3)
        self.assertEqual(page["available_individuals"], 2)
        self.assertEqual(page["deleted_individuals"], 1)
        self.assertEqual(page["inaccessible_individuals"], 0)
        self.assertNotIn(deleted, [row["individual_uuid"] for row in page["items"]])
        exported_batch, exported = self.service.export(batch.pk)
        self.assertEqual(exported_batch._individual_read_counts["deleted_individuals"], 1)
        self.assertEqual(self.workbook(exported)[INDIVIDUAL_SHEET].max_row, 3)
        self.assertFalse(self.service.parse(exported).errors)
        result = self.service.parse(content)
        self.assertFalse(result.errors)
        self.assertEqual(len(result.valid_rows), 2)
        self.assertIn("deleted", result.rows[1].errors[0])
        self.assertEqual(result.rows[1].updates, {})

    def test_partial_current_access_filters_rows_and_reports_omissions(self):
        batch, content = self.generate(target_count=3)
        selected = list(iter_selection(batch))
        hidden = selected[0]["individual_uuid"]
        with patch.object(Individual, "get_queryset", side_effect=lambda qs, user: qs.exclude(pk=hidden)):
            first = self.service.preview(batch.pk, first=1)
            second = self.service.preview(batch.pk, first=1, offset=1)
            self.assertEqual(first["available_individuals"], 2)
            self.assertEqual(first["inaccessible_individuals"], 1)
            self.assertEqual(first["deleted_individuals"], 0)
            self.assertTrue(first["has_next_page"])
            self.assertFalse(second["has_next_page"])
            self.assertEqual(first["items"][0]["individual_uuid"], selected[1]["individual_uuid"])
            _, exported = self.service.export(batch.pk)
            exported_rows = self.workbook(exported)[INDIVIDUAL_SHEET].iter_rows(min_row=2)
            self.assertNotIn(hidden, [row[1].value for row in exported_rows])
            result = self.service.parse(content)
            self.assertEqual(len(result.valid_rows), 2)
            self.assertIn("outside your current access", result.rows[0].errors[0])
            self.assertFalse(result.rows[0].updates)

    def test_creator_can_inspect_omission_counts_when_no_rows_remain_accessible(self):
        batch, content = self.generate(target_count=2)
        Individual.objects.filter(pk__in=selection_rows(batch).values("individual_id")).update(is_deleted=True)
        with patch.object(User, "is_imis_admin", new_callable=PropertyMock, return_value=False), patch.object(
            Individual, "get_queryset", side_effect=lambda qs, user: qs,
        ):
            page = self.service.preview(batch.pk)
            self.assertEqual(page["items"], [])
            self.assertEqual(page["deleted_individuals"], 2)
            self.assertEqual(page["available_individuals"], 0)
            self.assertFalse(page["has_next_page"])
            _, exported = self.service.export(batch.pk)
            self.assertEqual(self.workbook(exported)[INDIVIDUAL_SHEET].max_row, 1)
            result = self.service.parse(content)
            self.assertTrue(all("deleted" in row.errors[0] for row in result.rows))

    def test_unrelated_scoped_reader_can_access_only_visible_saved_rows(self):
        batch, _ = self.generate(target_count=2)
        selected = list(iter_selection(batch))
        reader = User.objects.create(username="partially-scoped-reader")
        reader.has_perms = lambda perms: True
        with patch.object(User, "is_imis_admin", new_callable=PropertyMock, return_value=False), patch.object(
            Individual, "get_queryset", side_effect=lambda qs, user: qs.filter(pk=selected[1]["individual_uuid"]),
        ):
            page = IndividualValidationListService(reader).preview(batch.pk)
        self.assertEqual(page["available_individuals"], 1)
        self.assertEqual(page["inaccessible_individuals"], 1)
        self.assertEqual(page["items"][0]["individual_uuid"], selected[1]["individual_uuid"])

    def test_saved_reads_require_history_right_and_parse_still_requires_upload_right(self):
        batch, content = self.generate(target_count=1)
        self.actor.has_perms = lambda perms: list(perms) == ["958001"]
        with self.assertRaises(PermissionDenied):
            self.service.preview(batch.pk)
        with self.assertRaises(PermissionDenied):
            self.service.export(batch.pk)
        self.actor.has_perms = lambda perms: list(perms) == ["958003"]
        self.assertEqual(self.service.preview(batch.pk)["available_individuals"], 1)
        self.service.export(batch.pk)
        with self.assertRaises(PermissionDenied):
            self.service.parse(content)
        self.actor.has_perms = lambda perms: list(perms) == ["958002"]
        self.assertFalse(self.service.parse(content).errors)

    def test_geography_labels_and_identity_source_keys_are_saved_per_programme(self):
        fields = [
            {"column_name": "Business city", "location_type": "D"},
            {"column_name": "Electoral ward", "location_type": "W"},
            {"column_name": "Neighbourhood", "location_type": "V"},
            {"column_name": "Identity number", "json_ext_key": "document_number"},
            {"column_name": "Sex", "json_ext_key": "sex"},
        ]
        Individual.objects.filter(pk=self.standalone.pk).update(
            json_ext={"document_number": "001234", "sex": "Female", "national_id": "WRONG", "gender": "WRONG"},
        )
        with patch.object(HouseholdValidationConfig, "individual_workbook_fields", {"umep": fields}):
            batch, content = self.generate()
        sheet = self.workbook(content)[INDIVIDUAL_SHEET]
        headers = [cell.value for cell in sheet[1]]
        for header in ("District", "TA", "GVH", "Village", "national_id", "gender"):
            self.assertNotIn(header, headers)
        row = next(row for row in iter_selection(batch) if row["individual_uuid"] == str(self.standalone.pk))
        self.assertEqual(row["Identity number"], "001234")
        self.assertEqual(row["Sex"], "Female")
        self.assertEqual(row["Business city"], self.ta.name)
        self.assertEqual(row["Electoral ward"], self.gvh.name)
        self.assertEqual(row["Neighbourhood"], self.village.name)
        _, exported = self.service.export(batch.pk)
        self.assertEqual(list(sheet.values), list(self.workbook(exported)[INDIVIDUAL_SHEET].values))
        self.assertFalse(self.service.parse(content).errors)
        workbook = self.workbook(content)
        workbook[INDIVIDUAL_SHEET].cell(2, headers.index("Business city") + 1, "Tampered")
        parsed = self.service.parse(self.serialise(workbook))
        self.assertTrue(any("protected field Business city" in error for error in parsed.rows[0].errors))

    def test_empty_display_configuration_uses_only_core_and_editable_columns(self):
        with patch.object(HouseholdValidationConfig, "individual_workbook_fields", {}):
            _, content = self.generate(target_count=1)
        self.assertEqual([cell.value for cell in self.workbook(content)[INDIVIDUAL_SHEET][1]],
                         [*BASE_COLUMNS, *(col["column_name"] for col in COLUMNS)])
        self.assertFalse(self.service.parse(content).errors)

    def test_invalid_display_fields_fail_before_issuing_a_list(self):
        for fields in ({}, [{"column_name": "City"}], [{"column_name": "City", "location_type": "X"}],
                       [{"column_name": "City", "location_type": "D", "json_ext_key": "city"}],
                       [{"column_name": "Decision", "json_ext_key": "foo"}],
                       [{"column_name": "Turnover", "json_ext_key": "foo"}],
                       [{"column_name": "ID", "json_ext_key": ""}]):
            with self.subTest(fields=fields), patch.object(
                HouseholdValidationConfig, "individual_workbook_fields", {"UMEP": fields},
            ), self.assertRaises(ValidationError):
                self.generate()
        self.assertFalse(HouseholdValidationBatch.objects.exists())

    def test_invalid_display_configuration_is_rejected_on_load_and_module_save(self):
        from django.apps import apps

        app = apps.get_app_config("household_validation")
        for value in ([], {"": []}, {"UMEP": [], "umep": []},
                      {"UMEP": [{"column_name": "City", "location_type": "INVALID"}]}):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    HouseholdValidationConfig._load_config({"individual_workbook_fields": value})
                with self.assertRaises(ValidationError):
                    app._validate_module_config(SimpleNamespace(_cfg={"individual_workbook_fields": value}))

    def test_location_move_outside_user_scope_does_not_block_the_other_saved_rows(self):
        batch, content = self.generate(target_count=2)
        moved = next(iter_selection(batch))["individual_uuid"]
        Individual.objects.filter(pk=moved).update(location=self.other)
        with patch.object(Individual, "get_queryset", side_effect=lambda qs, user: qs.filter(location=self.village)):
            page = self.service.preview(batch.pk)
            self.assertEqual(page["available_individuals"], 1)
            self.assertEqual(page["inaccessible_individuals"], 1)
            _, exported = self.service.export(batch.pk)
            self.assertEqual(self.workbook(exported)[INDIVIDUAL_SHEET].max_row, 2)
            result = self.service.parse(content)
            self.assertEqual(len(result.valid_rows), 1)
            self.assertIn("outside your current access", result.rows[0].errors[0])

    def test_selection_rows_are_immutable_and_exclude_upload_attempt_rows(self):
        batch, content = self.generate(target_count=2)
        expected = list(iter_selection(batch))
        HouseholdValidationBatchRow(
            batch=batch, row_number=2, upload_attempt_id=uuid4(), raw_row={"first_name": "Changed by upload"},
            json_ext={"kind": "INDIVIDUAL_SELECTION"},
        ).save(user=self.actor)
        self.assertEqual(list(iter_selection(batch)), expected)
        self.assertEqual(self.service.preview(batch.pk)["available_individuals"], 2)
        _, exported = self.service.export(batch.pk)
        self.assertEqual(list(self.workbook(content)[INDIVIDUAL_SHEET].values),
                         list(self.workbook(exported)[INDIVIDUAL_SHEET].values))

    def test_thousands_of_saved_rows_are_paged_in_sql_and_not_embedded_in_batch_json(self):
        Individual.objects.bulk_create([
            Individual(id=uuid4(), first_name=f"Large list {number}", last_name="Candidate", dob=self.standalone.dob,
                       location=self.village, user_created=self.actor, user_updated=self.actor)
            for number in range(2000)
        ], batch_size=500)
        batch, content = self.generate()
        self.assertEqual(batch.json_ext["selected_individuals"], 2004)
        self.assertNotIn("selection", batch.json_ext)
        self.assertNotIn("Large list", str(batch.json_ext))
        self.assertEqual(selection_rows(batch).count(), 2004)
        self.assertEqual(self.workbook(content)[INDIVIDUAL_SHEET].max_row, 2005)
        with CaptureQueriesContext(connection) as queries:
            page = self.service.preview(batch.pk, first=2, offset=1000)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["available_individuals"], 2004)
        page_queries = [query["sql"] for query in queries
                        if '"household_validation_householdvalidationbatchrow"."raw_row"' in query["sql"]]
        self.assertEqual(len(page_queries), 1)
        self.assertIn("LIMIT 2 OFFSET 1000", page_queries[0])

    def test_legacy_issued_lists_keep_their_original_headers_and_read_policy(self):
        legacy_fields = [{"column_name": name, "json_ext_key": name} for name in ("national_id", "gender")]
        legacy_fields += [{"column_name": name, "location_type": level}
                          for level, name in (("R", "District"), ("D", "TA"), ("W", "GVH"), ("V", "Village"))]
        with patch.object(HouseholdValidationConfig, "individual_workbook_fields", {"UMEP": legacy_fields}):
            batch, content = self.generate(target_count=2)
        batch.json_ext["selection"] = list(iter_selection(batch))
        batch.json_ext.pop("selection_storage")
        batch.json_ext.pop("display_fields")
        batch.save(user=self.actor)
        selection_rows(batch).delete()
        _, exported = self.service.export(batch.pk)
        self.assertEqual(list(self.workbook(content)[INDIVIDUAL_SHEET].values),
                         list(self.workbook(exported)[INDIVIDUAL_SHEET].values))
        self.assertFalse(self.service.parse(content).errors)
        Individual.objects.filter(pk=batch.json_ext["selection"][0]["individual_uuid"]).update(is_deleted=True)
        self.assertEqual(self.service.preview(batch.pk)["available_individuals"], 1)
        self.assertEqual(len(self.service.parse(content).valid_rows), 1)
