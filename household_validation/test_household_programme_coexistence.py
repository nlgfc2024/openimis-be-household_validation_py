"""Real database/GraphQL household round trips with individual validation enabled."""
import base64
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import graphene
from django.test import TestCase, tag

from individual.models import Group, GroupIndividual, Individual

from household_validation import test_individual_workbook as fixtures
from household_validation import tests as household_fixtures
from household_validation.apps import DEFAULT_CONFIG, HouseholdValidationConfig
from household_validation.individual_lists import IndividualValidationListService
from household_validation.individual_selection import EligibleIndividualSelectionService
from household_validation.models import HouseholdValidationBatch, HouseholdValidationBatchRow
from household_validation.schema import Mutation, Query
from household_validation.upload import VALIDATION_LIST_SHEET, parse_validation_workbook


@tag("umep_acceptance")
class HouseholdProgrammeCoexistenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.IndividualWorkbookTests.setUpTestData.__func__(cls)

    workbook = staticmethod(fixtures.IndividualWorkbookTests.workbook)
    serialise = staticmethod(fixtures.IndividualWorkbookTests.serialise)

    def setUp(self):
        fixtures.IndividualWorkbookTests.setUp(self)
        # Synthetic household entries exercise each algorithm without defining deployment rules.
        rules = {
            **deepcopy(DEFAULT_CONFIG["program_eligibility_rules"]),
            "RMEP": {"requires_data_source": "PWP", "priority_flag": "business_experience"},
            "UPG": {"requires_data_source": "SCTP", "member_flag": "fit_for_work",
                    "member_min_age": 18, "member_max_age": 60},
            "UMEP": {"subject": "INDIVIDUAL", "data_source": "ubr"},
        }
        columns = deepcopy(DEFAULT_CONFIG["program_specific_export_columns"])
        for code in ("RMEP", "UPG"):
            columns[code] = [{"key": f"{code.lower()}_answer", "column_name": f"{code} answer",
                              "target_individual_json_ext_key": f"{code.lower()}_answer", "type": "select",
                              "options": ["Yes", "No"], "required": True}]
        columns["UMEP"] = deepcopy(fixtures.COLUMNS)
        for field, value in (("program_eligibility_rules", rules), ("program_specific_export_columns", columns)):
            setting = patch.object(HouseholdValidationConfig, field, value)
            setting.start()
            self.addCleanup(setting.stop)
        Group.objects.update(is_deleted=True)
        self.group = Group(code="COEX-HH", location=self.village)
        self.group.save(user=self.actor)
        self.person = Individual(
            first_name="Synthetic", last_name="Household head", dob=date(1995, 1, 1), location=self.village,
            json_ext={"gender": "Male", "fit_for_work": True, "unrelated": "keep",
                      "national_id": "NAT-COEX-001"},
        )
        self.person.save(user=self.actor)
        self.membership = GroupIndividual(group=self.group, individual=self.person, role="HEAD")
        self.membership.save(user=self.actor)
        self.schema = graphene.Schema(query=Query, mutation=Mutation)

    def gql(self, query, variables=None):
        result = self.schema.execute(query, variable_values=variables,
                                     context_value=SimpleNamespace(user=self.actor))
        self.assertIsNone(result.errors, result.errors)
        return result.data

    def generate_workbook(self, code=None):
        self.person.json_ext["data_source"] = "sctp" if code == "UPG" else "pwp"
        self.person.save(user=self.actor)
        if code is None:
            # The PWP screen omits benefitPlanCode entirely.
            query = '''mutation($village: String!) {
              generateHouseholdValidationList(villageCode: $village, targetCount: 1) {
                batchId fileBase64 selectedHouseholds selectedIndividuals
              }
            }'''
            variables = {"village": self.village.code}
        else:
            query = '''mutation($code: String!, $village: String!) {
              generateHouseholdValidationList(benefitPlanCode: $code, villageCode: $village, targetCount: 1) {
                batchId fileBase64 selectedHouseholds selectedIndividuals
              }
            }'''
            variables = {"code": code, "village": self.village.code}
        generated = self.gql(query, variables)["generateHouseholdValidationList"]
        self.assertEqual((generated["selectedHouseholds"], generated["selectedIndividuals"]), (1, 1))
        workbook = self.workbook(base64.b64decode(generated["fileBase64"]))
        sheet = workbook[VALIDATION_LIST_SHEET]
        headers = {cell.value: cell.column for cell in sheet[1]}
        return generated, workbook, headers

    def round_trip(self, code=None):
        generated, workbook, headers = self.generate_workbook(code)
        batch = HouseholdValidationBatch.objects.get(pk=generated["batchId"])
        self.assertEqual(batch.json_ext["benefit_plan_code"], code)
        code = code or "PWP"
        sheet = workbook[VALIDATION_LIST_SHEET]
        self.assertIn("primary_worker", headers)
        self.assertNotIn("decision", headers)
        if code != "PWP":
            self.assertIn(f"{code} answer", headers)
            sheet.cell(2, headers[f"{code} answer"], "Yes")
        sheet.cell(2, headers["primary_worker"], "Yes")
        encoded = base64.b64encode(self.serialise(workbook)).decode("ascii")
        query = '''mutation($file: String!, $dry: Boolean!) {
          uploadHouseholdValidationList(fileBase64: $file, dryRun: $dry) {
            householdsVerified participantsVerified errors errorMessages uploadAttemptId
          }
        }'''
        # Stale frontend rights must not authorize either household or individual uploads.
        self.actor.has_perms = lambda perms: all(str(perm) == "953002" for perm in perms)
        denied = self.schema.execute(query, variable_values={"file": encoded, "dry": False},
                                     context_value=SimpleNamespace(user=self.actor))
        self.assertTrue(denied.errors)
        self.actor.has_perms = lambda perms: True
        before = deepcopy(Individual.objects.get(pk=self.person.pk).json_ext)
        dry = self.gql(query, {"file": encoded, "dry": True})["uploadHouseholdValidationList"]
        self.assertEqual(dry["errors"], 0, dry["errorMessages"])
        self.assertEqual(Individual.objects.get(pk=self.person.pk).json_ext, before)
        committed = self.gql(query, {"file": encoded, "dry": False})["uploadHouseholdValidationList"]
        self.assertEqual(committed["errors"], 0, committed["errorMessages"])
        self.assertEqual((committed["householdsVerified"], committed["participantsVerified"]), (1, 1))
        self.person.refresh_from_db()
        self.group.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(self.group.json_ext["validation_status"], "VERIFIED")
        self.assertEqual(self.person.json_ext["validation_status"], "VERIFIED")
        self.assertTrue(self.membership.json_ext["primary_worker"])
        self.assertEqual(self.person.json_ext["unrelated"], "keep")
        if code != "PWP":
            self.assertEqual(self.person.json_ext[f"{code.lower()}_answer"], "Yes")
        batch = HouseholdValidationBatch.objects.get(pk=generated["batchId"])
        self.assertTrue(batch.rows.filter(upload_attempt_id=committed["uploadAttemptId"]).exists())
        self.assertNotIn(self.person.pk, set(EligibleIndividualSelectionService(self.actor).candidates(
            "UMEP",
        ).values_list("pk", flat=True)))
        individual_batch, _ = IndividualValidationListService(self.actor).generate(
            "UMEP", village_code=self.village.code, target_count=1,
        )
        history = self.gql('''{ householdValidationBatches { count batches { id } } }''')[
            "householdValidationBatches"]
        self.assertEqual(history["count"], 1)
        self.assertEqual([item["id"] for item in history["batches"]], [str(batch.pk)])
        self.assertNotEqual(str(individual_batch.pk), str(batch.pk))

    def test_pwp_round_trip_with_individual_programme_enabled(self):
        self.round_trip("PWP")

    def test_pwp_round_trip_without_benefit_plan_code_with_individual_programme_enabled(self):
        self.round_trip()

    def test_pwp_round_trip_with_empty_benefit_plan_code_with_individual_programme_enabled(self):
        self.round_trip("")

    def test_pwp_batch_resolution_preserves_primary_worker_national_id_requirement(self):
        self.person.json_ext.pop("national_id")
        self.person.save(user=self.actor)
        query = '''mutation($file: String!, $dry: Boolean!) {
          uploadHouseholdValidationList(fileBase64: $file, dryRun: $dry) {
            batchId errors errorMessages householdsVerified
          }
        }'''
        for code in (None, "", "PWP"):
            generated, workbook, headers = self.generate_workbook(code)
            workbook[VALIDATION_LIST_SHEET].cell(2, headers["primary_worker"], "Yes")
            encoded = base64.b64encode(self.serialise(workbook)).decode("ascii")
            batch_count = HouseholdValidationBatch.objects.count()
            for dry_run in (True, False):
                with self.subTest(code=code, dry_run=dry_run):
                    result = self.gql(query, {"file": encoded, "dry": dry_run})["uploadHouseholdValidationList"]
                    self.assertEqual(result["errors"], 1)
                    self.assertIn("national_id is required", result["errorMessages"][0])
                    self.assertEqual(result["householdsVerified"], 0)
                    if not dry_run:
                        self.assertEqual(result["batchId"], generated["batchId"])
                    self.assertEqual(HouseholdValidationBatch.objects.count(), batch_count)
            self.person.refresh_from_db()
            self.group.refresh_from_db()
            self.membership.refresh_from_db()
            self.assertNotIn("validation_status", self.person.json_ext)
            self.assertNotIn("validation_status", self.group.json_ext)
            self.assertFalse((self.membership.json_ext or {}).get("primary_worker", False))

    def test_invalid_programme_answers_retain_the_issued_batch(self):
        generated, workbook, headers = self.generate_workbook("UPG")
        workbook[VALIDATION_LIST_SHEET].cell(2, headers["primary_worker"], "Yes")
        content = self.serialise(workbook)
        parsed = parse_validation_workbook(content)
        self.assertEqual(parsed.rows, [])
        self.assertEqual(len(parsed.invalid_rows), 1)
        query = '''mutation($file: String!) {
          uploadHouseholdValidationList(fileBase64: $file) { batchId errors }
        }'''
        batch_count = HouseholdValidationBatch.objects.count()
        result = self.gql(query, {"file": base64.b64encode(content).decode("ascii")})["uploadHouseholdValidationList"]
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["batchId"], generated["batchId"])
        self.assertEqual(HouseholdValidationBatch.objects.count(), batch_count)

    def test_upg_round_trip_with_individual_programme_enabled(self):
        self.round_trip("UPG")

    def test_rmep_round_trip_with_individual_programme_enabled(self):
        self.round_trip("RMEP")

    def test_legacy_household_workbook_never_requires_or_merges_individual_columns(self):
        columns = {"PWP": deepcopy(DEFAULT_CONFIG["program_specific_export_columns"]["PWP"]),
                   "UMEP": deepcopy(fixtures.COLUMNS) + [{
                       "key": "has_business", "column_name": "Does member have a business",
                       "target_individual_json_ext_key": "has_business", "type": "select", "options": ["Maybe"],
                   }]}
        workbook = household_fixtures.ValidationUploadParserTest()._upload_workbook()
        headers = {cell.value: cell.column for cell in workbook[VALIDATION_LIST_SHEET][1]}
        with patch.object(HouseholdValidationConfig, "program_specific_export_columns", columns):
            parsed = parse_validation_workbook(self.serialise(workbook))
            self.assertEqual(parsed.errors, [])
            self.assertEqual(len(parsed.rows), 1)
            workbook[VALIDATION_LIST_SHEET].cell(2, headers["Does member have a business"], "Maybe")
            parsed = parse_validation_workbook(self.serialise(workbook))
            self.assertTrue(any("not a valid option" in error for error in parsed.errors))
            self.assertEqual(parsed.rows, [])

    def test_known_household_batch_still_rejects_a_removed_required_programme_column(self):
        from django.utils import timezone

        workbook = household_fixtures.ValidationUploadParserTest()._upload_workbook(extra_columns=[{
            "key": "upg_answer", "column_name": "UPG answer", "target_individual_json_ext_key": "upg_answer",
            "type": "select", "options": ["Yes", "No"], "required": True,
        }])
        batch = HouseholdValidationBatch(generated_at=timezone.now(), json_ext={"benefit_plan_code": "UPG"})
        batch.save(user=self.actor)
        sheet = workbook[VALIDATION_LIST_SHEET]
        headers = {cell.value: cell.column for cell in sheet[1]}
        sheet.cell(2, headers["batch_id"], str(batch.pk))
        sheet.cell(2, headers["UPG answer"], "Yes")
        self.assertEqual(parse_validation_workbook(self.serialise(workbook)).errors, [])
        sheet.delete_cols(headers["UPG answer"])
        parsed = parse_validation_workbook(self.serialise(workbook))
        self.assertIn("Row 2: UPG answer is required", parsed.errors)
        self.assertEqual(parsed.rows, [])

    def test_issued_household_workbook_rejects_missing_programme_config_without_writes(self):
        query = """mutation($file: String!, $dry: Boolean!) {
          uploadHouseholdValidationList(fileBase64: $file, dryRun: $dry) { errors }
        }"""
        for code in ("PWP", "UPG", "RMEP"):
            generated, workbook, headers = self.generate_workbook(code)
            sheet = workbook[VALIDATION_LIST_SHEET]
            sheet.cell(2, headers["primary_worker"], "Yes")
            if code != "PWP":
                sheet.cell(2, headers[f"{code} answer"], "Yes")
            content = self.serialise(workbook)
            self.assertEqual(parse_validation_workbook(content).errors, [])
            encoded = base64.b64encode(content).decode("ascii")
            before = {
                "batches": list(HouseholdValidationBatch.objects.values()),
                "rows": list(HouseholdValidationBatchRow.objects.values()),
                "people": list(Individual.objects.values()),
                "groups": list(Group.objects.values()),
                "memberships": list(GroupIndividual.objects.values()),
                "history": (HouseholdValidationBatch.history.count(), HouseholdValidationBatchRow.history.count(),
                            Individual.history.count(), Group.history.count(), GroupIndividual.history.count()),
            }
            for drift in ("removed", "renamed", "empty"):
                columns = deepcopy(HouseholdValidationConfig.program_specific_export_columns)
                old = columns.pop(code)
                if drift == "renamed":
                    columns[f"{code}_RENAMED"] = old
                elif drift == "empty":
                    columns[code] = []
                with patch.object(HouseholdValidationConfig, "program_specific_export_columns", columns):
                    for dry_run in (True, False):
                        with self.subTest(code=code, drift=drift, dry_run=dry_run):
                            result = self.schema.execute(
                                query, variable_values={"file": encoded, "dry": dry_run},
                                context_value=SimpleNamespace(user=self.actor),
                            )
                            self.assertTrue(result.errors)
                            self.assertIn(
                                f"No household upload column configuration for issued benefit plan code '{code}'",
                                str(result.errors[0]),
                            )
                            self.assertIsNone(result.data["uploadHouseholdValidationList"])
            self.assertEqual(list(HouseholdValidationBatch.objects.values()), before["batches"])
            self.assertEqual(list(HouseholdValidationBatchRow.objects.values()), before["rows"])
            self.assertEqual(list(Individual.objects.values()), before["people"])
            self.assertEqual(list(Group.objects.values()), before["groups"])
            self.assertEqual(list(GroupIndividual.objects.values()), before["memberships"])
            self.assertEqual(
                (HouseholdValidationBatch.history.count(), HouseholdValidationBatchRow.history.count(),
                 Individual.history.count(), Group.history.count(), GroupIndividual.history.count()),
                before["history"],
            )
