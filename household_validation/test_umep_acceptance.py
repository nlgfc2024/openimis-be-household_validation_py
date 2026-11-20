"""Assembly acceptance: issued workbook -> registry -> enrolment -> ordinary update.

Defaults are small synthetic configurations, not deployment business rules. Set both
UMEP_VALIDATION_CONFIG and UMEP_ENROLMENT_CONFIG to repeat against deployment JSON.
Requires the Malawi assembly's enrolment and social_protection applications.
"""
import base64
import csv
import json
import os
from copy import deepcopy
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

import graphene
from django.apps import apps
from django.db import connection
from django.test import TestCase, tag

from individual.models import Group, GroupIndividual, Individual, IndividualDataSource, IndividualDataSourceUpload
from location.models import Location
from social_protection.models import BenefitPlan, Beneficiary

from household_validation import test_individual_workbook as fixtures
from household_validation.apps import DEFAULT_CONFIG, HouseholdValidationConfig
from household_validation.individual_lists import IndividualValidationListService
from household_validation.individual_selection import EligibleIndividualSelectionService
from household_validation.individual_snapshots import iter_selection
from household_validation.individual_upload import IndividualValidationUploadService
from household_validation.individual_workbook import INDIVIDUAL_SHEET, validate_individual_columns
from household_validation.programmes import resolve_programme_columns
from household_validation.schema import Mutation, Query


SYNTHETIC_COLUMNS = [
    {"key": "turnover", "column_name": "Monthly turnover", "target_individual_json_ext_key": "monthly_turnover",
     "type": "number", "min": 0, "required": True},
    {"key": "youth", "column_name": "Youth owned", "target_individual_json_ext_key": "youth_owned_managed",
     "type": "select", "options": ["Yes", "No"], "required": True},
    {"key": "inclusion", "column_name": "Inclusion", "target_individual_json_ext_key": "inclusion_contribution",
     "type": "select", "options": ["Yes", "No"], "required": True},
]
SYNTHETIC_PROFILE = {"ranking": {"*": {
    "weighted_score": {"normalise_to": 100, "components": [
        {"field": "monthly_turnover", "weight": 50,
         "scoring": {"type": "numeric_bands", "bands": [{"min": 40000000, "points": 50}]}},
        {"field": "youth_owned_managed", "weight": 25,
         "scoring": {"type": "mapping", "values": {"Yes": 25, "No": 0}}},
        {"field": "inclusion_contribution", "weight": 25,
         "scoring": {"type": "mapping", "values": {"Yes": 25, "No": 0}}},
    ]},
    "minimum_score": 60, "order_by": [{"field": "_weighted_score", "direction": "desc"}],
}}}


@tag("umep_acceptance")
@skipUnless(apps.is_installed("enrolment"), "UMEP acceptance requires the Malawi enrolment assembly.")
@skipUnless(connection.vendor == "postgresql", "Ordinary beneficiary updates require PostgreSQL procedures.")
class UmepValidationAcceptanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.IndividualWorkbookTests.setUpTestData.__func__(cls)

    workbook = staticmethod(fixtures.IndividualWorkbookTests.workbook)
    serialise = staticmethod(fixtures.IndividualWorkbookTests.serialise)

    def setUp(self):
        from enrolment.apps import DEFAULT_CONFIG as ENROLMENT_DEFAULTS, EnrolmentConfig

        fixtures.IndividualWorkbookTests.setUp(self)
        paths = [os.environ.get(key) for key in ("UMEP_VALIDATION_CONFIG", "UMEP_ENROLMENT_CONFIG")]
        self.assertEqual(bool(paths[0]), bool(paths[1]), "Supply both UMEP configuration file paths, or neither.")
        validation = {
            "program_eligibility_rules": {**DEFAULT_CONFIG["program_eligibility_rules"],
                                          "UMEP": {"subject": "INDIVIDUAL", "data_source": "ubr"}},
            "program_specific_export_columns": {"UMEP": SYNTHETIC_COLUMNS},
            "individual_workbook_fields": {"UMEP": fixtures.DISPLAY_FIELDS},
            "export_column_options_overrides": {"youth": ["Yes", "No", "Not assessed"]},
        }
        enrolment = {"programme_profiles": {"UMEP": SYNTHETIC_PROFILE}, "require_programme_profile": True}
        if paths[0]:
            validation, enrolment = [json.loads(Path(path).read_text()) for path in paths]
        self.validation_config = {**deepcopy(DEFAULT_CONFIG), **deepcopy(validation)}
        self.enrolment_config = {**deepcopy(ENROLMENT_DEFAULTS), **deepcopy(enrolment)}
        for config, values in ((HouseholdValidationConfig, self.validation_config),
                               (EnrolmentConfig, self.enrolment_config)):
            for key, value in values.items():
                if hasattr(config, key):
                    setting = patch.object(config, key, deepcopy(value))
                    setting.start()
                    self.addCleanup(setting.stop)
        self.district.name = "Blantyre"
        self.district.save()
        for name in ("Lilongwe", "Mzuzu", "Zomba"):
            Location.objects.create(name=name, code=f"ACCEPT-{name}", type="R", audit_user_id=1)
        # Exactly two people in the selected neighbourhood, outside active households.
        Individual.objects.filter(pk__in=[self.former_member.pk, self.deleted_group_member.pk]).update(is_deleted=True)
        self.source = self.validation_config["program_eligibility_rules"]["UMEP"].get("data_source", "ubr")
        Individual.objects.filter(pk__in=[self.standalone.pk, self.second.pk]).update(json_ext={
            "data_source": self.source, "untouched": {"nested": True}, "business_name": "Original business",
            "validation_notes": "Before visit", "report_synch": "true",
        })
        apps.get_app_config("household_validation")._validate_module_config(SimpleNamespace(_cfg=validation))
        self.columns = resolve_programme_columns("UMEP")
        validate_individual_columns(self.columns, self.validation_config["individual_workbook_fields"]["UMEP"])
        EnrolmentConfig._validate_config(self.enrolment_config)
        properties = {column["target_individual_json_ext_key"]: {
            "type": "number" if column["type"] == "number" else "string",
        } for column in self.columns}
        properties.update({"business_name": {"type": "string"}, "validation_status": {"type": "string"}})
        self.plan = BenefitPlan(code="UMEP", name="Synthetic UMEP acceptance Phase", type="INDIVIDUAL",
                                max_beneficiaries=1000, beneficiary_data_schema={"properties": properties},
                                json_ext={"data_source": self.source, "advanced_criteria": {"ACTIVE": []}})
        self.plan.save(user=self.actor)
        self.schema = graphene.Schema(query=Query, mutation=Mutation)
        self.uploader = IndividualValidationUploadService(self.actor)

    def gql(self, query, variables=None):
        result = self.schema.execute(query, variable_values=variables,
                                     context_value=SimpleNamespace(user=self.actor))
        self.assertIsNone(result.errors, result.errors)
        return result.data

    def enrolment_ids(self, selected_only=False):
        from enrolment.services import build_individual_enrolment_selection

        selection = build_individual_enrolment_selection([], str(self.plan.pk), "ACTIVE", self.actor)
        key = "individuals_not_assigned_to_selected_programme" if selected_only else "individual_query_with_filters"
        return set(selection[key].values_list("pk", flat=True))

    def ranking(self):
        from enrolment.profiles import profile_ranking_spec

        return profile_ranking_spec(self.plan, "ACTIVE")

    def fill_answers(self, workbook, row_number=2):
        """Synthetic high-scoring answers derived from the supplied configuration."""
        components = {item["field"]: item["scoring"] for item in
                      self.ranking()["weighted_score"]["components"] if "field" in item}
        headers = {cell.value: cell.column for cell in workbook[INDIVIDUAL_SHEET][1]}
        for column in self.columns:
            scoring = components.get(column["target_individual_json_ext_key"], {})
            if column["type"] == "select":
                points = scoring.get("values", {})
                value = max(column["options"], key=lambda option: points.get(option, 0))
            elif column["type"] == "number":
                value = max([1, column.get("min", 0), *[band.get("min", 0)
                            for band in scoring.get("bands", [])]])
                value = min(value, column.get("max", value))
            else:
                value = "Synthetic checked answer"
            workbook[INDIVIDUAL_SHEET].cell(row_number, headers[column["column_name"]], value)
        workbook[INDIVIDUAL_SHEET].cell(row_number, headers["decision"], "VERIFIED")
        workbook[INDIVIDUAL_SHEET].cell(row_number, headers["validation_notes"], "Checked in acceptance test")

    @staticmethod
    def household_state():
        return (list(Group.objects.order_by("pk").values("pk", "json_ext", "version", "is_deleted")),
                list(GroupIndividual.objects.order_by("pk").values(
                    "pk", "json_ext", "version", "recipient_type", "role", "is_deleted",
                )))

    def issue_and_validate_one(self):
        before_households = self.household_state()
        generated = self.gql('''mutation($city: String!, $ward: [String], $village: String!) {
          generateIndividualValidationList(benefitPlanCode: "UMEP", districtCode: $city,
            gvhCodes: $ward, villageCode: $village, targetCount: 2) {
            batchId fileBase64 selectedIndividuals eligibleIndividuals
          }
        }''', {"city": self.district.code, "ward": [self.gvh.code], "village": self.village.code})[
            "generateIndividualValidationList"]
        self.assertEqual(generated["eligibleIndividuals"], 2)
        self.assertEqual(generated["selectedIndividuals"], 2)
        batch_id = generated["batchId"]
        saved = self.gql('''query($batch: UUID!) {
          individualValidationList(batchId: $batch) {
            selectedIndividuals availableIndividuals items
          }
          individualValidationListExport(batchId: $batch) { fileBase64 }
        }''', {"batch": batch_id})
        preview = saved["individualValidationList"]
        self.assertEqual(preview["availableIndividuals"], 2)
        issued = self.workbook(base64.b64decode(generated["fileBase64"]))
        reexported = self.workbook(base64.b64decode(saved["individualValidationListExport"]["fileBase64"]))
        self.assertEqual(list(issued[INDIVIDUAL_SHEET].values), list(reexported[INDIVIDUAL_SHEET].values))
        selected = [row["individual_uuid"] for row in preview["items"]]
        self.assertEqual(selected, [row[1] for row in list(issued[INDIVIDUAL_SHEET].values)[1:]])
        before_blank = deepcopy(Individual.objects.get(pk=selected[1]).json_ext)
        self.assertEqual(self.enrolment_ids(), set())
        self.fill_answers(issued)
        content = self.serialise(issued)
        individual_history = Individual.history.count()
        dry = self.uploader.upload(content, dry_run=True)
        self.assertEqual((dry["rows_applied"], dry["rows_skipped"], dry["would_update"]), (1, 1, 1))
        self.assertEqual(Individual.history.count(), individual_history)
        self.assertEqual(self.uploader.history(batch_id), [])
        committed = self.uploader.upload(content)
        self.assertEqual((committed["rows_applied"], committed["rows_skipped"], committed["errors"]), (1, 1, 0))
        self.assertEqual(self.enrolment_ids(), {Individual.objects.get(pk=selected[0]).pk})
        self.assertEqual(self.enrolment_ids(selected_only=True), {Individual.objects.get(pk=selected[0]).pk})
        self.assertEqual(Individual.objects.get(pk=selected[1]).json_ext, before_blank)
        self.assertEqual(self.household_state(), before_households)
        rows = self.uploader.attempt_rows(batch_id, committed["upload_attempt_id"])["items"]
        self.assertEqual([row["status"] for row in rows], ["APPLIED", "SKIPPED"])
        self.assertEqual(self.uploader.error_report(batch_id, committed["upload_attempt_id"])[1], 0)
        history = Individual.history.count()
        self.assertEqual(self.uploader.upload(content)["individuals_updated"], 0)
        self.assertEqual(Individual.history.count(), history)
        return selected, before_households

    def ordinary_update(self, selected, partial):
        from social_protection.workflows.beneficiary_update_valid import process_update_valid_beneficiaries_workflow

        upload = IndividualDataSourceUpload(
            source_name="synthetic-acceptance.csv", source_type="update", status="PENDING",
        )
        upload.save(user=self.actor)
        rows = []
        for number, pk in enumerate(selected):
            beneficiary = Beneficiary(individual_id=pk, benefit_plan=self.plan, status="ACTIVE",
                                      json_ext={"business_name": "Original beneficiary business"})
            beneficiary.save(user=self.actor)
            source = IndividualDataSource(upload=upload, validations={"validation_errors": []}, json_ext={
                "ID": str(beneficiary.pk), "first_name": "Ordinary update", "last_name": "Acceptance",
                "dob": "1990-01-01", "location_name": None, "location_code": None,
                "business_name": "Corrected business",
                "validation_status": "NOT_VERIFIED" if number == 0 else "VERIFIED",
            })
            source.save(user=self.actor)
            rows.append(source)
        process_update_valid_beneficiaries_workflow(
            self.actor.pk, self.plan.pk, upload.pk, [rows[0].pk] if partial else None,
        )

    def assert_update_preserves_validation(self, partial):
        selected, before_households = self.issue_and_validate_one()
        before = [deepcopy(Individual.objects.get(pk=pk).json_ext) for pk in selected]
        self.ordinary_update(selected, partial)
        for number, pk in enumerate(selected):
            current = Individual.objects.get(pk=pk).json_ext
            self.assertEqual(current.get("validation_status"), "VERIFIED" if number == 0 else None)
            self.assertEqual(current["untouched"], {"nested": True})
            self.assertEqual(current["data_source"], self.source)
            for column in self.columns:
                key = column["target_individual_json_ext_key"]
                self.assertEqual(current.get(key), before[number].get(key))
            self.assertEqual(current["validation_notes"], before[number]["validation_notes"])
            self.assertEqual(current["business_name"],
                             "Original business" if partial and number else "Corrected business")
        self.assertEqual(self.enrolment_ids(), {Individual.objects.get(pk=selected[0]).pk})
        self.assertEqual(self.household_state(), before_households)

    def test_full_beneficiary_update_preserves_the_decision_consumed_by_enrolment(self):
        self.assert_update_preserves_validation(partial=False)

    def test_partial_beneficiary_update_preserves_validation_and_unaccepted_business_fields(self):
        self.assert_update_preserves_validation(partial=True)

    def test_enrolment_and_validation_use_the_same_active_household_definition(self):
        # Remove ranking only for this membership/source comparison.
        from enrolment.apps import EnrolmentConfig

        Individual.objects.filter(pk__in=[self.former_member.pk, self.deleted_group_member.pk]).update(is_deleted=False)
        Individual.objects.filter(is_deleted=False).update(json_ext={
            "data_source": self.source, "validation_status": "VERIFIED",
        })
        with patch.object(EnrolmentConfig, "programme_profiles", {"UMEP": {}}):
            expected = set(EligibleIndividualSelectionService(self.actor).candidates("UMEP").values_list(
                "pk", flat=True,
            ))
            self.assertEqual(self.enrolment_ids(), expected)
            self.assertNotIn(self.active_member.pk, expected)
            self.assertIn(self.former_member.pk, expected)
            self.assertIn(self.deleted_group_member.pk, expected)
            GroupIndividual.objects.filter(individual=self.active_member).update(is_deleted=True)
            expected.add(self.active_member.pk)
            self.assertEqual(self.enrolment_ids(), expected)
            self.assertEqual(set(EligibleIndividualSelectionService(self.actor).candidates("UMEP").values_list(
                "pk", flat=True,
            )), expected)

    def test_not_verified_and_invalid_business_answers_do_not_make_enrolment_candidates(self):
        batch, content = IndividualValidationListService(self.actor).generate("UMEP", village_code=self.village.code)
        workbook = self.workbook(content)
        selected = [row["individual_uuid"] for row in iter_selection(batch)]
        self.fill_answers(workbook)
        headers = {cell.value: cell.column for cell in workbook[INDIVIDUAL_SHEET][1]}
        first_column = next(column for column in self.columns if column["type"] == "number")
        workbook[INDIVIDUAL_SHEET].cell(2, headers[first_column["column_name"]], -1)
        workbook[INDIVIDUAL_SHEET].cell(3, headers["decision"], "NOT_VERIFIED")
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual((result["status"], result["errors"], result["rows_applied"]), ("PARTIAL_SUCCESS", 1, 1))
        self.assertIsNone(Individual.objects.get(pk=selected[0]).json_ext.get("validation_status"))
        self.assertEqual(Individual.objects.get(pk=selected[1]).json_ext["validation_status"], "NOT_VERIFIED")
        self.assertEqual(self.enrolment_ids(), set())
        report, count = self.uploader.error_report(batch.pk, result["upload_attempt_id"])
        self.assertEqual(count, 1)
        [error] = list(csv.DictReader(StringIO(report)))
        self.assertIn(first_column["column_name"], error)
        self.assertEqual(error[first_column["column_name"]], "-1")

    def test_configured_columns_cover_the_enrolment_score_fields_and_options(self):
        by_target = {column["target_individual_json_ext_key"]: column for column in self.columns}
        for component in self.ranking()["weighted_score"]["components"]:
            if "field" not in component:
                continue  # Geography is derived from Individual.location, not an editable answer.
            column = by_target[component["field"]]
            scoring = component["scoring"]
            if scoring["type"] == "numeric_bands":
                self.assertEqual(column["type"], "number")
            elif column["type"] == "select":
                self.assertTrue(set(scoring["values"]) <= set(column["options"]))
        self.assertEqual(self.enrolment_config["registry_individual_verification_field"], "validation_status")
        self.assertEqual(self.enrolment_config["registry_verified_individual_value"], "VERIFIED")

    def test_verified_answers_below_the_profile_threshold_do_not_guarantee_enrolment(self):
        _, content = IndividualValidationListService(self.actor).generate("UMEP", village_code=self.village.code)
        workbook = self.workbook(content)
        self.fill_answers(workbook)
        sheet = workbook[INDIVIDUAL_SHEET]
        headers = {cell.value: cell.column for cell in sheet[1]}
        components = {item["field"]: item["scoring"] for item in
                      self.ranking()["weighted_score"]["components"] if "field" in item}
        for column in self.columns:
            if column["type"] == "number":
                value = column.get("min", 0)
            elif column["type"] == "select":
                points = components.get(column["target_individual_json_ext_key"], {}).get("values", {})
                value = min(column["options"], key=lambda option: points.get(option, 0))
            else:
                continue
            sheet.cell(2, headers[column["column_name"]], value)
        result = self.uploader.upload(self.serialise(workbook))
        self.assertEqual(result["errors"], 0, result["error_messages"])
        self.assertEqual(result["individuals_verified"], 1)
        # Status/source/membership eligibility precedes profile ranking and capacity.
        self.assertEqual(len(self.enrolment_ids()), 1)
        self.assertEqual(self.enrolment_ids(selected_only=True), set())
