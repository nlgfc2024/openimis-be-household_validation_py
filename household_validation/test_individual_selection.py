"""Configuration and database-backed UMEP candidate contract tests."""
import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import graphene
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase as DjangoTestCase

from core.models import User
from individual.models import Group, GroupIndividual, Individual
from location.models import Location

from household_validation.apps import DEFAULT_CONFIG, HouseholdValidationConfig
from household_validation.individual_selection import EligibleIndividualSelectionService
from household_validation.programmes import (
    configured_programmes, programme_rules, resolve_programme_columns,
    resolve_programme_rule, resolve_programme_subject, selection_service,
)
from household_validation.schema import Query
from household_validation.services import EligibleHouseholdSelectionService
from household_validation.verification import parse_individual_decision


RULES = {"PWP": DEFAULT_CONFIG["program_eligibility_rules"]["PWP"], "UMEP": {"subject": "INDIVIDUAL"}}


class ProgrammeSubjectTests(TestCase):
    def setUp(self):
        self.rules = patch.object(HouseholdValidationConfig, "program_eligibility_rules", deepcopy(RULES))
        self.rules.start()
        self.addCleanup(self.rules.stop)

    def test_missing_subject_preserves_household_programmes(self):
        with patch.object(HouseholdValidationConfig, "program_eligibility_rules", {"PWP": {}, "RMEP": {}, "UPG": {}}):
            for code in ("PWP", "RMEP", "UPG"):
                self.assertEqual(resolve_programme_subject(code), "HOUSEHOLD")

    def test_missing_programme_keeps_existing_pwp_rule(self):
        self.assertEqual(resolve_programme_rule(), DEFAULT_CONFIG["program_eligibility_rules"]["PWP"])

    def test_programme_codes_are_case_insensitive(self):
        self.assertEqual(resolve_programme_subject("umep"), "INDIVIDUAL")

    def test_unknown_programme_does_not_fall_back(self):
        with self.assertRaises(ValidationError):
            resolve_programme_subject("UNKNOWN")

    def test_unsupported_subject_rejected_before_config_is_loaded(self):
        with self.assertRaises(ValidationError):
            HouseholdValidationConfig._load_config({"program_eligibility_rules": {"UMEP": {"subject": "PERSON"}}})
        self.assertEqual(HouseholdValidationConfig.program_eligibility_rules, RULES)

    def test_malformed_rules_and_duplicate_codes_rejected(self):
        for rules in ([], {"UMEP": []}, {"": {}}, {"UMEP": {}, "umep": {}}, {"UMEP": {"subject": None}}):
            with self.subTest(rules=rules), self.assertRaises(ValidationError):
                programme_rules(rules)

    def test_individual_rule_cannot_silently_apply_household_criteria(self):
        with self.assertRaises(ValidationError):
            programme_rules({"UMEP": {"subject": "INDIVIDUAL", "selection_strategy": {}}})

    def test_subject_dispatch_and_household_guard(self):
        self.assertIsInstance(selection_service("UMEP", None), EligibleIndividualSelectionService)
        self.assertIsInstance(selection_service("PWP", None), EligibleHouseholdSelectionService)
        with self.assertRaises(ValidationError):
            EligibleHouseholdSelectionService()._resolve_eligibility_rule("UMEP")

    def test_example_columns_and_overrides_are_resolved_without_mutating_config(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "umep.config.json"
        example = json.loads(path.read_text())
        columns = example["program_specific_export_columns"]
        with patch.object(HouseholdValidationConfig, "program_specific_export_columns", columns), patch.object(
            HouseholdValidationConfig, "export_column_options_overrides",
            {"youth_owned_managed": ["Yes", "No", "Unknown"]},
        ):
            resolved = resolve_programme_columns("umep")
            self.assertEqual(resolved[1]["options"], ["Yes", "No", "Unknown"])
            self.assertEqual(columns["UMEP"][1]["options"], ["Yes", "No"])
            resolved[0]["required"] = False
            self.assertTrue(columns["UMEP"][0]["required"])
            programmes = configured_programmes()
        self.assertEqual([p["benefit_plan_code"] for p in programmes], ["PWP", "UMEP"])
        self.assertEqual(programmes[1]["decision_values"], ["VERIFIED", "NOT_VERIFIED"])
        self.assertEqual(programmes[1]["blank_decision_behavior"], "SKIP_ROW")

    def test_configuration_replacement_does_not_deep_merge_programmes(self):
        with patch.object(HouseholdValidationConfig, "program_eligibility_rules", {"UMEP": {"subject": "INDIVIDUAL"}}):
            self.assertEqual([p["benefit_plan_code"] for p in configured_programmes()], ["UMEP"])
            # Preserve legacy omitted-programme fallback, not an explicit PWP lookup.
            self.assertEqual(resolve_programme_rule(), DEFAULT_CONFIG["program_eligibility_rules"]["PWP"])
            with self.assertRaises(ValidationError):
                resolve_programme_rule("PWP")

    def test_only_two_individual_decisions_are_accepted(self):
        for raw, expected in (("VERIFIED", "VERIFIED"), (" not_verified ", "NOT_VERIFIED"), (None, None), ("  ", None)):
            self.assertEqual(parse_individual_decision(raw), expected)
        for raw in ("REJECTED", "Yes", "No", True, 1, [], {}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                parse_individual_decision(raw)

    def test_programme_contract_requires_query_right(self):
        for user in (None, SimpleNamespace(id=1, is_anonymous=True),
                     SimpleNamespace(id=1, is_anonymous=False, has_perms=lambda perms: False)):
            with self.subTest(user=user), self.assertRaises(PermissionDenied):
                Query.resolve_validation_programmes(None, SimpleNamespace(context=SimpleNamespace(user=user)))


class IndividualSelectionDatabaseTests(DjangoTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create(username="umep-selection-test")
        cls.district = Location.objects.create(code="UV-D", name="UMEP city", type="R", audit_user_id=1)
        cls.ta = Location.objects.create(code="UV-TA", name="UMEP TA", type="D", parent=cls.district, audit_user_id=1)
        cls.gvh = Location.objects.create(code="UV-G", name="UMEP GVH", type="W", parent=cls.ta, audit_user_id=1)
        cls.village = Location.objects.create(
            code="UV-V", name="UMEP village", type="V", parent=cls.gvh, audit_user_id=1,
        )
        cls.other = Location.objects.create(code="UV-O", name="Other city", type="R", audit_user_id=1)

        def person(name, location, deleted=False):
            item = Individual(first_name=name, last_name="Candidate", dob=date(1990, 1, 1),
                              location=location, is_deleted=deleted)
            item.save(user=cls.actor)
            return item

        cls.standalone = person("Standalone", cls.village)
        cls.second = person("Second", cls.village)
        cls.outside = person("Outside", cls.other)
        cls.no_location = person("No location", None)
        cls.deleted = person("Deleted", cls.village, True)
        cls.active_member = person("Active member", cls.village)
        cls.former_member = person("Deleted membership", cls.village)
        cls.deleted_group_member = person("Deleted household", cls.village)
        cls.group = Group(code="UV-ACTIVE", location=cls.village)
        cls.group.save(user=cls.actor)
        cls.old_group = Group(code="UV-DELETED", location=cls.village, is_deleted=True)
        cls.old_group.save(user=cls.actor)
        GroupIndividual.objects.bulk_create([
            GroupIndividual(id=uuid4(), group=group, individual=individual, is_deleted=deleted,
                            user_created=cls.actor, user_updated=cls.actor)
            for individual, group, deleted in (
                (cls.active_member, cls.group, False), (cls.active_member, cls.group, False),
                (cls.former_member, cls.group, True), (cls.deleted_group_member, cls.old_group, False),
            )
        ])

    def setUp(self):
        self.rules = patch.object(HouseholdValidationConfig, "program_eligibility_rules", RULES)
        self.rules.start()
        self.addCleanup(self.rules.stop)
        self.user = SimpleNamespace(id=self.actor.id, is_anonymous=False, is_imis_admin=True,
                                    has_perms=lambda perms: list(perms) == ["958001"])
        self.service = EligibleIndividualSelectionService(self.user)

    def ids(self, **filters):
        return set(self.service.candidates("UMEP", **filters).values_list("pk", flat=True))

    def test_active_households_excluded_deleted_memberships_and_groups_allowed(self):
        ids = self.ids()
        self.assertNotIn(self.active_member.pk, ids)
        self.assertNotIn(self.deleted.pk, ids)
        self.assertIn(self.standalone.pk, ids)
        self.assertIn(self.former_member.pk, ids)
        self.assertIn(self.deleted_group_member.pk, ids)

    def test_geography_uses_individual_location_hierarchy(self):
        expected = {self.standalone.pk, self.second.pk, self.former_member.pk, self.deleted_group_member.pk}
        for filters in ({"district_code": self.district.code}, {"district_id": self.district.pk},
                        {"ta_code": self.ta.code}, {"gvh_codes": [self.gvh.code]},
                        {"village_code": self.village.code}):
            with self.subTest(filters=filters):
                self.assertEqual(self.ids(**filters), expected)

    def test_deleted_membership_never_substitutes_household_location(self):
        Individual.objects.filter(pk=self.former_member.pk).update(location=self.other)
        self.assertNotIn(self.former_member.pk, self.ids(district_code=self.district.code))
        self.assertIn(self.former_member.pk, self.ids(district_code=self.other.code))

    def test_candidates_do_not_require_existing_verified_status(self):
        Individual.objects.filter(pk=self.standalone.pk).update(json_ext={"validation_status": "NOT_VERIFIED"})
        self.assertIn(self.standalone.pk, self.ids())

    def test_target_counts_people_and_order_is_stable(self):
        expected = sorted(self.ids(village_code=self.village.code))
        result = self.service.select("UMEP", target_count=2, village_code=self.village.code)
        self.assertEqual(result.eligible_individuals, 4)
        self.assertEqual(result.selected_individuals, 2)
        self.assertEqual([item.pk for item in result.individuals], expected[:2])
        self.assertEqual(list(result.individuals), list(self.service.select(
            "UMEP", target_count=2, village_code=self.village.code,
        ).individuals))

    def test_zero_and_invalid_targets(self):
        self.assertEqual(self.service.select("UMEP", target_count=0).selected_individuals, 0)
        for count in (-1, True, "2", 1.5):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                self.service.select("UMEP", target_count=count)

    def test_user_scoping_is_applied_before_selection_and_counting(self):
        with patch.object(
            Individual, "get_queryset", side_effect=lambda qs, user: qs.filter(pk=self.second.pk),
        ) as scope:
            result = self.service.select("UMEP")
        scope.assert_called_once()
        self.assertEqual(result.eligible_individuals, 1)
        self.assertEqual([item.pk for item in result.individuals], [self.second.pk])

    def test_anonymous_or_missing_user_cannot_select(self):
        for user in (None, SimpleNamespace(id=1, is_anonymous=True)):
            with self.subTest(user=user), self.assertRaises(PermissionDenied):
                EligibleIndividualSelectionService(user).select("UMEP")

    def test_household_programme_cannot_use_individual_selection(self):
        with self.assertRaises(ValidationError):
            self.service.select("PWP")

    def test_graphql_contract_and_candidate_pagination(self):
        schema = graphene.Schema(query=Query)
        query = '''{ validationProgrammes { benefitPlanCode subject decisionValues blankDecisionBehavior }
          individualValidationCandidates(benefitPlanCode: "UMEP", targetCount: 3, first: 1, offset: 1) {
            eligibleIndividuals selectedIndividuals hasNextPage items { id firstName locationId }
          } }'''
        result = schema.execute(query, context_value=SimpleNamespace(user=self.user))
        self.assertIsNone(result.errors)
        programmes = result.data["validationProgrammes"]
        self.assertEqual(programmes[1]["decisionValues"], ["VERIFIED", "NOT_VERIFIED"])
        page = result.data["individualValidationCandidates"]
        self.assertEqual(page["selectedIndividuals"], 3)
        self.assertTrue(page["hasNextPage"])
        self.assertEqual(len(page["items"]), 1)

    def test_query_right_and_pagination_boundaries(self):
        schema = graphene.Schema(query=Query)
        self.user.has_perms = lambda perms: False
        result = schema.execute('{ individualValidationCandidates(benefitPlanCode: "UMEP") { eligibleIndividuals } }',
                                context_value=SimpleNamespace(user=self.user))
        self.assertIsNotNone(result.errors)
        self.user.has_perms = lambda perms: True
        for args in ("first: 101", "first: -1", "offset: -1"):
            result = schema.execute(
                '{ individualValidationCandidates(benefitPlanCode: "UMEP", ' + args + ') { eligibleIndividuals } }',
                context_value=SimpleNamespace(user=self.user),
            )
            self.assertIsNotNone(result.errors)
