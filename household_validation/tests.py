from datetime import date
from types import SimpleNamespace
from unittest import TestCase

from household_validation.apps import (
    DEFAULT_CONFIG,
    GROUP_RIGHTS,
    HOUSEHOLD_VALIDATION_RIGHTS,
    RIGHT_GROUP_SEARCH,
    RIGHT_GROUP_UPDATE,
    RIGHT_HOUSEHOLD_VALIDATION_ERROR_REPORT,
    RIGHT_HOUSEHOLD_VALIDATION_HISTORY,
    RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT,
    RIGHT_HOUSEHOLD_VALIDATION_UPLOAD,
)
from household_validation.excel import (
    EXCEL_COLUMNS,
    PROJECT_OPTIONS_SHEET,
    ExcelValidationListExporter,
)
from household_validation.project_lookup import (
    ACTIVE_PROJECT_STATUSES,
    ProjectOption,
    project_option_from_project,
)
from household_validation.selection import (
    CATEGORY_FEMALE_HEADED,
    CATEGORY_OTHER,
    CATEGORY_YOUTH,
    ROW_TYPE_MAIN,
    ROW_TYPE_RESERVE,
    EligibleHousehold,
    EligibleMember,
    SelectedHousehold,
    SelectionResult,
    is_truthy,
    select_households,
)


class HouseholdValidationConfigTest(TestCase):
    def test_default_config_exposes_permission_lists(self):
        self.assertEqual(
            DEFAULT_CONFIG["gql_query_household_validation_rule_perms"],
            [str(RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT)],
        )
        self.assertEqual(
            DEFAULT_CONFIG["gql_mutation_generate_household_validation_list_perms"],
            [str(RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT)],
        )
        self.assertEqual(
            DEFAULT_CONFIG["gql_mutation_upload_household_validation_list_perms"],
            [str(RIGHT_HOUSEHOLD_VALIDATION_UPLOAD)],
        )
        self.assertEqual(
            DEFAULT_CONFIG["gql_query_household_validation_history_perms"],
            [str(RIGHT_HOUSEHOLD_VALIDATION_HISTORY)],
        )
        self.assertEqual(
            DEFAULT_CONFIG["gql_query_household_validation_error_report_perms"],
            [str(RIGHT_HOUSEHOLD_VALIDATION_ERROR_REPORT)],
        )
        self.assertEqual(
            DEFAULT_CONFIG["group_search_perms"],
            [str(RIGHT_GROUP_SEARCH)],
        )
        self.assertEqual(
            DEFAULT_CONFIG["group_update_perms"],
            [str(RIGHT_GROUP_UPDATE)],
        )

    def test_right_sets_match_default_config_scope(self):
        self.assertEqual(
            HOUSEHOLD_VALIDATION_RIGHTS,
            [
                RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT,
                RIGHT_HOUSEHOLD_VALIDATION_UPLOAD,
                RIGHT_HOUSEHOLD_VALIDATION_HISTORY,
                RIGHT_HOUSEHOLD_VALIDATION_ERROR_REPORT,
            ],
        )
        self.assertIn(RIGHT_GROUP_SEARCH, GROUP_RIGHTS)
        self.assertIn(RIGHT_GROUP_UPDATE, GROUP_RIGHTS)


def _dob_for_age(age):
    today = date.today()
    return date(today.year - age, today.month, today.day)


def _member(member_id, gender="Male", age=40, fit_for_work=True, role=None):
    return EligibleMember(
        id=member_id,
        gender=gender,
        dob=_dob_for_age(age),
        fit_for_work=fit_for_work,
        role=role,
    )


def _household(
    household_id,
    wealth_quintile,
    head_gender="Male",
    eligible_member_age=40,
    last_verified_date=None,
    eligible_members=None,
):
    head = _member(
        f"{household_id}-head",
        gender=head_gender,
        age=50,
        role="HEAD",
    )
    if eligible_members is None:
        eligible_members = [
            _member(
                f"{household_id}-worker",
                gender="Male",
                age=eligible_member_age,
            )
        ]
    return EligibleHousehold(
        id=household_id,
        code=str(household_id),
        wealth_quintile=wealth_quintile,
        last_verified_date=last_verified_date,
        head=head,
        eligible_members=eligible_members,
    )


class HouseholdSelectionTest(TestCase):
    def test_select_households_applies_quota_categories(self):
        households = [
            _household("female", "Poorest", head_gender="Female"),
            _household("youth", "Poorest", eligible_member_age=25),
            _household("other", "Poorest"),
        ]

        result = select_households(households, target_count=3, reserve_percentage=0)

        self.assertEqual([row.row_type for row in result.main], [ROW_TYPE_MAIN] * 3)
        self.assertEqual(
            [row.category for row in result.main],
            [CATEGORY_FEMALE_HEADED, CATEGORY_YOUTH, CATEGORY_OTHER],
        )

    def test_select_households_prioritizes_poorest_within_category(self):
        households = [
            _household("female-richer", "Richer", head_gender="Female"),
            _household("female-poorest", "Poorest", head_gender="Female"),
            _household("youth", "Poorest", eligible_member_age=20),
        ]

        result = select_households(households, target_count=1, reserve_percentage=0)

        self.assertEqual(result.main[0].household.id, "female-poorest")

    def test_select_households_defaults_target_to_all_eligible_and_caps_reserve(self):
        households = [
            _household("female", "Poorest", head_gender="Female"),
            _household("youth", "Poorer", eligible_member_age=20),
        ]

        result = select_households(households)

        self.assertEqual(len(result.main), 2)
        self.assertEqual(result.reserve, [])

    def test_select_households_adds_reserve_from_remaining_households(self):
        households = [
            _household("female", "Poorest", head_gender="Female"),
            _household("youth", "Poorer", eligible_member_age=20),
            _household("other", "Middle"),
        ]

        result = select_households(households, target_count=2, reserve_percentage=50)

        self.assertEqual(len(result.main), 2)
        self.assertEqual(len(result.reserve), 1)
        self.assertEqual(result.reserve[0].row_type, ROW_TYPE_RESERVE)
        self.assertEqual(result.reserve[0].household.id, "other")

    def test_select_households_excludes_recently_verified_households(self):
        households = [
            _household(
                "recent",
                "Poorest",
                last_verified_date=date(2026, 7, 2),
            ),
            _household(
                "old",
                "Poorer",
                last_verified_date=date(2026, 6, 1),
            ),
        ]

        result = select_households(
            households,
            exclude_verified_after=date(2026, 6, 30),
        )

        self.assertEqual([row.household.id for row in result.main], ["old"])

    def test_select_households_ignores_households_without_eligible_members(self):
        households = [
            EligibleHousehold(
                id="no-workers",
                code="no-workers",
                wealth_quintile="Poorest",
                eligible_members=[],
            ),
            _household("worker", "Poorer"),
        ]

        result = select_households(households)

        self.assertEqual([row.household.id for row in result.main], ["worker"])

    def test_select_households_backfills_quota_shortage_from_other_categories(self):
        households = [
            _household("female", "Poorest", head_gender="Female"),
            _household("other-1", "Poorer"),
            _household("other-2", "Middle"),
        ]

        result = select_households(households, target_count=3, reserve_percentage=0)

        self.assertEqual(len(result.main), 3)
        self.assertEqual(
            [row.household.id for row in result.main],
            ["female", "other-1", "other-2"],
        )

    def test_selection_result_expands_selected_households_to_member_rows(self):
        households = [
            _household(
                "household",
                "Poorest",
                eligible_members=[
                    _member("worker-1", age=24),
                    _member("worker-2", age=45),
                ],
            )
        ]

        result = select_households(households)

        self.assertEqual(len(result.member_rows), 2)
        self.assertEqual(
            [row.member.id for row in result.member_rows],
            ["worker-1", "worker-2"],
        )
        self.assertEqual(
            {row.household.id for row in result.member_rows},
            {"household"},
        )

    def test_is_truthy_accepts_ubr_boolean_shapes(self):
        for value in (True, 1, "1", "true", "TRUE", "yes", "Y"):
            self.assertTrue(is_truthy(value))
        for value in (False, 0, "0", "false", "no", None, ""):
            self.assertFalse(is_truthy(value))


class ProjectLookupTest(TestCase):
    def test_active_project_statuses_match_mvp_dropdown_scope(self):
        self.assertEqual(ACTIVE_PROJECT_STATUSES, ("PREPARATION", "IN_PROGRESS"))

    def test_project_option_from_project_serializes_project_fields(self):
        project = SimpleNamespace(
            id="project-1",
            name="Road Works",
            status="PREPARATION",
            location_id=12,
        )

        self.assertEqual(
            project_option_from_project(project),
            ProjectOption(
                id="project-1",
                name="Road Works",
                status="PREPARATION",
                location_id="12",
            ),
        )

    def test_project_option_from_project_falls_back_to_location_object(self):
        project = SimpleNamespace(
            id="project-2",
            name="Drainage",
            status="IN_PROGRESS",
            location=SimpleNamespace(id=21),
        )

        self.assertEqual(project_option_from_project(project).location_id, "21")


class ExcelValidationListExporterTest(TestCase):
    def test_export_workbook_writes_headers_and_member_rows(self):
        result = self._selection_result()

        workbook = ExcelValidationListExporter(result, batch_id="batch-1").export_workbook()
        worksheet = workbook["Validation List"]

        self.assertEqual(
            [worksheet.cell(row=1, column=index).value for index in range(1, len(EXCEL_COLUMNS) + 1)],
            EXCEL_COLUMNS,
        )
        self.assertEqual(worksheet["A2"].value, "batch-1")
        self.assertEqual(worksheet["B2"].value, ROW_TYPE_MAIN)
        self.assertEqual(worksheet["C2"].value, "District")
        self.assertEqual(worksheet["D2"].value, "Traditional Authority")
        self.assertEqual(worksheet["E2"].value, "Village")
        self.assertEqual(worksheet["F2"].value, "HH-001")
        self.assertEqual(worksheet["G2"].value, "group-1")
        self.assertEqual(worksheet["H2"].value, "member-1")
        self.assertEqual(worksheet["I2"].value, "Ada Worker")
        self.assertEqual(worksheet["M2"].value, "YES")
        self.assertEqual(worksheet["N2"].value, "YES")
        self.assertEqual(worksheet["O2"].value, "PRIMARY")

    def test_export_workbook_has_hidden_project_id_and_dropdowns(self):
        result = self._selection_result()
        projects = [SimpleNamespace(id="project-1", name="Road Works, Phase 1")]

        workbook = ExcelValidationListExporter(
            result,
            batch_id="batch-1",
            projects=projects,
        ).export_workbook()
        worksheet = workbook["Validation List"]
        project_options = workbook[PROJECT_OPTIONS_SHEET]
        project_id_letter = worksheet.cell(1, EXCEL_COLUMNS.index("project_id") + 1).column_letter

        self.assertTrue(worksheet.column_dimensions[project_id_letter].hidden)
        self.assertEqual(project_options.sheet_state, "hidden")
        self.assertEqual(project_options["A2"].value, "project-1")
        self.assertEqual(project_options["B2"].value, "Road Works, Phase 1")
        formulas = {validation.formula1 for validation in worksheet.data_validations.dataValidation}
        self.assertIn('"YES,NO"', formulas)
        self.assertIn("'Project Options'!$B$2:$B$2", formulas)

    def test_export_workbook_locks_structural_cells_and_unlocks_field_inputs(self):
        workbook = ExcelValidationListExporter(
            self._selection_result(),
            batch_id="batch-1",
        ).export_workbook()
        worksheet = workbook["Validation List"]

        self.assertTrue(worksheet.protection.sheet)
        self.assertTrue(worksheet["G2"].protection.locked)
        self.assertFalse(worksheet["P2"].protection.locked)
        self.assertFalse(worksheet["Q2"].protection.locked)
        self.assertFalse(worksheet["U2"].protection.locked)

    def test_export_workbook_writes_one_row_per_selected_eligible_member(self):
        result = self._selection_result(member_count=2)

        workbook = ExcelValidationListExporter(result, batch_id="batch-1").export_workbook()
        worksheet = workbook["Validation List"]

        self.assertEqual(worksheet.max_row, 3)
        self.assertEqual(worksheet["H2"].value, "member-1")
        self.assertEqual(worksheet["H3"].value, "member-2")

    def _selection_result(self, member_count=1):
        location = self._location_tree()
        group = SimpleNamespace(id="group-1", code="HH-001", location=location)
        selected_members = []
        for index in range(1, member_count + 1):
            individual = SimpleNamespace(
                first_name="Ada" if index == 1 else "Grace",
                last_name="Worker",
            )
            group_individual = SimpleNamespace(individual=individual)
            selected_members.append(
                EligibleMember(
                    id=f"member-{index}",
                    gender="Female",
                    dob=date(1990, 1, index),
                    fit_for_work=True,
                    role="HEAD" if index == 1 else None,
                    recipient_type="PRIMARY" if index == 1 else None,
                    source=group_individual,
                )
            )
        household = EligibleHousehold(
            id="group-1",
            code="HH-001",
            wealth_quintile="Poorest",
            head=selected_members[0],
            eligible_members=selected_members,
            source=group,
        )
        return SelectionResult(
            main=[
                SelectedHousehold(
                    household=household,
                    category=CATEGORY_FEMALE_HEADED,
                    row_type=ROW_TYPE_MAIN,
                )
            ],
            reserve=[],
        )

    def _location_tree(self):
        district = SimpleNamespace(type="D", name="District", code="D01", parent=None)
        ta = SimpleNamespace(type="W", name="Traditional Authority", code="TA01", parent=district)
        return SimpleNamespace(type="V", name="Village", code="V01", parent=ta)
