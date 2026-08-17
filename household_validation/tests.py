from datetime import date, datetime, timezone as datetime_timezone
from io import BytesIO
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, call, patch

from django.utils import timezone
from openpyxl import Workbook, load_workbook

from household_validation.apps import (
    DEFAULT_CONFIG,
    DISTRICT_VALIDATION_ROLE_RIGHTS,
    DISTRICT_VALIDATION_ROLES,
    GROUP_RIGHTS,
    HOUSEHOLD_VALIDATION_RIGHTS,
    ROLE_DISTRICT_ADMINISTRATOR,
    ROLE_DISTRICT_PROGRAM_MANAGER,
    ROLE_DISTRICT_USER,
    RIGHT_GROUP_SEARCH,
    RIGHT_GROUP_UPDATE,
    RIGHT_HOUSEHOLD_VALIDATION_ERROR_REPORT,
    RIGHT_HOUSEHOLD_VALIDATION_HISTORY,
    RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT,
    RIGHT_HOUSEHOLD_VALIDATION_UPLOAD,
    HouseholdValidationConfig,
)
from household_validation.excel import (
    EXCEL_COLUMNS,
    PROJECT_OPTIONS_SHEET,
    ExcelValidationListExporter,
)
from household_validation.gql_permissions import (
    HouseholdValidationPermissionError,
    require_permissions,
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
from household_validation.services import (
    EligibleHouseholdSelectionService,
    HouseholdValidationUploadService,
    _json_safe,
    _local_date,
)
from household_validation.upload import (
    PROJECT_SELECTION_TYPE_INTENT,
    UploadedValidationRow,
    VALIDATION_STATUS_NOT_VERIFIED,
    VALIDATION_LIST_SHEET,
    build_validation_error_report_csv,
    build_validation_json_ext,
    member_structural_errors,
    parse_validation_workbook,
)
from household_validation.wealth import get_household_wealth_quintile


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

    def test_default_config_exposes_selection_percentages(self):
        self.assertEqual(DEFAULT_CONFIG["gql_mutation_female_headed_percentage"], 40)
        self.assertEqual(DEFAULT_CONFIG["gql_mutation_youth_percentage"], 40)
        self.assertEqual(DEFAULT_CONFIG["gql_mutation_reserve_percentage"], 20)

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

    def test_district_validation_roles_have_validation_and_group_update_rights(self):
        self.assertEqual(
            DISTRICT_VALIDATION_ROLES,
            [
                ROLE_DISTRICT_ADMINISTRATOR,
                ROLE_DISTRICT_PROGRAM_MANAGER,
                ROLE_DISTRICT_USER,
            ],
        )
        for role_name in DISTRICT_VALIDATION_ROLES:
            role_rights = DISTRICT_VALIDATION_ROLE_RIGHTS[role_name]
            for right in HOUSEHOLD_VALIDATION_RIGHTS:
                self.assertIn(right, role_rights)
            self.assertIn(RIGHT_GROUP_SEARCH, role_rights)
            self.assertIn(RIGHT_GROUP_UPDATE, role_rights)


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

        result = select_households(households, target_count=3)

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

        result = select_households(households, target_count=1)

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

        with patch.object(HouseholdValidationConfig, "gql_mutation_reserve_percentage", 50):
            result = select_households(households, target_count=2)

        self.assertEqual(len(result.main), 2)
        self.assertEqual(len(result.reserve), 1)
        self.assertEqual(result.reserve[0].row_type, ROW_TYPE_RESERVE)
        self.assertEqual(result.reserve[0].household.id, "other")

    def test_select_households_adds_default_twenty_percent_reserve(self):
        households = [
            _household(index, "Poorest")
            for index in range(1, 21)
        ]

        result = select_households(households, target_count=10)

        self.assertEqual(len(result.main), 10)
        self.assertEqual(len(result.reserve), 2)
        self.assertEqual(result.reserve[0].row_type, ROW_TYPE_RESERVE)

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

        result = select_households(households, target_count=3)

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

    def test_select_households_treats_ages_18_to_35_as_youth(self):
        households = [
            _household("age-18", "Poorest", eligible_member_age=18),
            _household("age-35", "Poorest", eligible_member_age=35),
            _household("age-36", "Poorest", eligible_member_age=36),
        ]

        result = select_households(households, target_count=3)

        categories = {
            row.household.id: row.category
            for row in result.main
        }
        self.assertEqual(categories["age-18"], CATEGORY_YOUTH)
        self.assertEqual(categories["age-35"], CATEGORY_YOUTH)
        self.assertEqual(categories["age-36"], CATEGORY_OTHER)

    def test_is_truthy_accepts_ubr_boolean_shapes(self):
        for value in (True, 1, "1", "true", "TRUE", "yes", "Y"):
            self.assertTrue(is_truthy(value))
        for value in (False, 0, "0", "false", "no", None, ""):
            self.assertFalse(is_truthy(value))

    def test_select_households_uses_configured_percentages(self):
        households = [
            _household("female", "Poorest", head_gender="Female"),
            _household("youth", "Poorest", eligible_member_age=25),
            _household("other", "Poorest"),
        ]

        with (
            patch.object(HouseholdValidationConfig, "gql_mutation_female_headed_percentage", 0),
            patch.object(HouseholdValidationConfig, "gql_mutation_youth_percentage", 100),
        ):
            result = select_households(households, target_count=2)

        self.assertEqual(
            [row.category for row in result.main],
            [CATEGORY_YOUTH, CATEGORY_FEMALE_HEADED],
        )


class HouseholdValidationPreviewServiceTest(TestCase):
    def test_summary_and_preview_share_selection_result(self):
        service = _FakeSelectionService(
            [
                _fake_group("group-1", "HH-001", "Female", 30, "Poorest"),
                _fake_group("group-2", "HH-002", "Male", 22, "Poorer"),
                _fake_group("group-3", "HH-003", "Male", 45, "Middle"),
            ]
        )

        filters = {
            "target_count": 2,
        }
        summary = service.summary(**filters)
        preview_rows = service.preview(**filters)

        self.assertEqual(summary["total_households"], 3)
        self.assertEqual(summary["total_individuals"], 3)
        self.assertEqual(summary["eligible_households"], 3)
        self.assertEqual(summary["selected_households"], 2)
        self.assertEqual(summary["selected_individuals"], 2)
        self.assertEqual(summary["reserve_households"], 1)
        self.assertEqual(len(preview_rows), 3)
        self.assertEqual(
            len([row for row in preview_rows if row.row_type == ROW_TYPE_MAIN]),
            summary["selected_individuals"],
        )

    def test_preview_rows_include_location_and_validation_fields(self):
        service = _FakeSelectionService(
            [
                _fake_group(
                    "group-1",
                    "HH-001",
                    "Female",
                    30,
                    "Poorest",
                    validation_status="VERIFIED",
                )
            ]
        )

        row = service.preview(target_count=1)[0]

        self.assertEqual(row.group_uuid, "group-1")
        self.assertEqual(row.group_code, "HH-001")
        self.assertEqual(row.head_name, "Head Person")
        self.assertEqual(row.individual_first_name, "Head")
        self.assertEqual(row.region, "Region")
        self.assertEqual(row.district, "District")
        self.assertEqual(row.municipality, "Traditional Authority")
        self.assertEqual(row.village, "Village")
        self.assertEqual(row.wealth_quintile, "Poorest")
        self.assertEqual(row.validation_status, "VERIFIED")


class _FakeSelectionService(EligibleHouseholdSelectionService):
    def __init__(self, groups):
        super().__init__(user=None)
        self.groups = groups

    def _base_queryset(self):
        return _FakeQuerySet(self.groups)

    def _project_names(self, location):
        return []


class _FakeQuerySet:
    def __init__(self, groups):
        self.groups = groups

    def __iter__(self):
        return iter(self.groups)

    def filter(self, *args, **kwargs):
        return self

    def select_related(self, *args, **kwargs):
        return self

    def prefetch_related(self, *args, **kwargs):
        return self


class _FakeRelatedManager:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


def _fake_group(
    group_id,
    code,
    head_gender,
    worker_age,
    wealth_quintile,
    validation_status=None,
):
    location = _fake_location_tree()
    individual = SimpleNamespace(
        id=f"{group_id}-individual",
        first_name="Head",
        last_name="Person",
        dob=_dob_for_age(worker_age),
        json_ext={
            "gender": head_gender,
            "fit_for_work": True,
            "household_wealth_quintile": wealth_quintile,
        },
    )
    group_individual = SimpleNamespace(
        id=f"{group_id}-member",
        is_deleted=False,
        role="HEAD",
        recipient_type="PRIMARY",
        individual=individual,
        individual_id=individual.id,
    )
    json_ext = {
        "head_id": individual.id,
        "household_wealth_quintile": wealth_quintile,
    }
    if validation_status:
        json_ext["validation_status"] = validation_status
    return SimpleNamespace(
        id=group_id,
        code=code,
        json_ext=json_ext,
        location=location,
        groupindividuals=_FakeRelatedManager([group_individual]),
    )


def _fake_location_tree():
    region = SimpleNamespace(type="R", name="Region", code="R01", parent=None, id=1)
    district = SimpleNamespace(type="D", name="District", code="D01", parent=region, id=2)
    ta = SimpleNamespace(type="W", name="Traditional Authority", code="TA01", parent=district, id=3)
    return SimpleNamespace(type="V", name="Village", code="V01", parent=ta, id=4)


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


class GraphQLPermissionTest(TestCase):
    def test_require_permissions_allows_user_with_required_rights(self):
        user = SimpleNamespace(
            id="user-1",
            is_anonymous=False,
            has_perms=lambda perms: perms == ["958001"],
        )

        self.assertIsNone(require_permissions(user, ["958001"]))

    def test_require_permissions_rejects_missing_rights(self):
        user = SimpleNamespace(
            id="user-1",
            is_anonymous=False,
            has_perms=lambda perms: False,
        )

        with self.assertRaises(HouseholdValidationPermissionError):
            require_permissions(user, ["958002"])

    def test_require_permissions_rejects_anonymous_or_missing_user(self):
        anonymous_user = SimpleNamespace(
            id=None,
            is_anonymous=True,
            has_perms=lambda perms: True,
        )

        with self.assertRaises(HouseholdValidationPermissionError):
            require_permissions(anonymous_user, ["958001"])
        with self.assertRaises(HouseholdValidationPermissionError):
            require_permissions(None, ["958001"])


class ValidationUploadParserTest(TestCase):
    def test_parse_validation_workbook_resolves_project_name_to_hidden_project_id(self):
        workbook = self._upload_workbook()
        worksheet = workbook[VALIDATION_LIST_SHEET]
        project_column = EXCEL_COLUMNS.index("project") + 1
        verified_column = EXCEL_COLUMNS.index("verified") + 1
        primary_worker_column = EXCEL_COLUMNS.index("primary_worker") + 1
        worksheet.cell(row=2, column=project_column, value="Road Works")
        worksheet.cell(row=2, column=verified_column, value="YES")
        worksheet.cell(row=2, column=primary_worker_column, value="YES")

        parsed = parse_validation_workbook(self._workbook_bytes(workbook))

        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.rows_read, 1)
        self.assertEqual(parsed.rows[0].project_id, "project-1")
        self.assertEqual(parsed.rows[0].verified, True)
        self.assertEqual(parsed.rows[0].primary_worker, True)
        self.assertEqual(PROJECT_SELECTION_TYPE_INTENT, "INTENT")

    def test_parse_validation_workbook_reports_missing_required_columns(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = VALIDATION_LIST_SHEET
        worksheet.append(["batch_id", "group_uuid"])

        parsed = parse_validation_workbook(self._workbook_bytes(workbook))

        self.assertEqual(parsed.rows, [])
        self.assertTrue(parsed.errors[0].startswith("Missing required columns:"))
        self.assertIn("member_uuid", parsed.errors[0])

    def test_parse_validation_workbook_preserves_primary_worker_no_and_blank(self):
        workbook = self._upload_workbook()
        worksheet = workbook[VALIDATION_LIST_SHEET]
        primary_worker_column = EXCEL_COLUMNS.index("primary_worker") + 1
        worksheet.cell(row=2, column=primary_worker_column, value="NO")
        worksheet.append(
            [
                "batch-1" if column == "batch_id"
                else "group-2" if column == "group_uuid"
                else "member-2" if column == "member_uuid"
                else None
                for column in EXCEL_COLUMNS
            ]
        )

        parsed = parse_validation_workbook(self._workbook_bytes(workbook))

        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.rows[0].primary_worker, False)
        self.assertIsNone(parsed.rows[1].primary_worker)

    def test_parse_validation_workbook_rejects_invalid_editable_values(self):
        workbook = self._upload_workbook()
        worksheet = workbook[VALIDATION_LIST_SHEET]
        verified_column = EXCEL_COLUMNS.index("verified") + 1
        primary_worker_column = EXCEL_COLUMNS.index("primary_worker") + 1
        date_column = EXCEL_COLUMNS.index("validation_date") + 1
        worksheet.cell(row=2, column=verified_column, value="MAYBE")
        worksheet.cell(row=2, column=primary_worker_column, value="LATER")
        worksheet.cell(row=2, column=date_column, value="not-a-date")

        parsed = parse_validation_workbook(self._workbook_bytes(workbook))

        self.assertEqual(parsed.rows, [])
        self.assertIn("Row 2: verified must be YES or NO", parsed.errors)
        self.assertIn("Row 2: primary_worker must be YES or NO", parsed.errors)
        self.assertIn("Row 2: validation_date is invalid", parsed.errors)

    def test_parse_validation_workbook_rejects_unknown_project_names(self):
        workbook = self._upload_workbook()
        worksheet = workbook[VALIDATION_LIST_SHEET]
        project_column = EXCEL_COLUMNS.index("project") + 1
        worksheet.cell(row=2, column=project_column, value="Unknown Project")

        parsed = parse_validation_workbook(self._workbook_bytes(workbook))

        self.assertEqual(parsed.rows, [])
        self.assertIn("Row 2: project is not in the project options", parsed.errors)

    def test_parse_validation_workbook_rejects_hidden_project_id_without_project(self):
        workbook = self._upload_workbook()
        worksheet = workbook[VALIDATION_LIST_SHEET]
        project_id_column = EXCEL_COLUMNS.index("project_id") + 1
        worksheet.cell(row=2, column=project_id_column, value="project-1")

        parsed = parse_validation_workbook(self._workbook_bytes(workbook))

        self.assertEqual(parsed.rows, [])
        self.assertIn("Row 2: project_id cannot be set without project", parsed.errors)

    def test_parse_validation_workbook_rejects_hidden_project_id_mismatch(self):
        workbook = self._upload_workbook()
        worksheet = workbook[VALIDATION_LIST_SHEET]
        project_column = EXCEL_COLUMNS.index("project") + 1
        project_id_column = EXCEL_COLUMNS.index("project_id") + 1
        worksheet.cell(row=2, column=project_column, value="Road Works")
        worksheet.cell(row=2, column=project_id_column, value="different-project")

        parsed = parse_validation_workbook(self._workbook_bytes(workbook))

        self.assertEqual(parsed.rows, [])
        self.assertIn("Row 2: project is not in the project options", parsed.errors)

    def test_parse_validation_workbook_resolves_duplicate_project_name_labels(self):
        workbook = self._upload_workbook()
        worksheet = workbook[VALIDATION_LIST_SHEET]
        project_options = workbook[PROJECT_OPTIONS_SHEET]
        project_options.cell(row=1, column=3, value="project_label")
        project_options.cell(row=2, column=3, value="Road Works (project-1)")
        project_options.append(["project-2", "Road Works", "Road Works (project-2)"])
        project_column = EXCEL_COLUMNS.index("project") + 1
        worksheet.cell(row=2, column=project_column, value="Road Works (project-2)")

        parsed = parse_validation_workbook(self._workbook_bytes(workbook))

        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.rows[0].project_id, "project-2")
        self.assertEqual(parsed.rows[0].project_name, "Road Works")

    def _upload_workbook(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = VALIDATION_LIST_SHEET
        worksheet.append(EXCEL_COLUMNS)
        values = {column: None for column in EXCEL_COLUMNS}
        values.update(
            {
                "batch_id": "batch-1",
                "group_uuid": "group-1",
                "member_uuid": "member-1",
                "District": "District",
                "TA": "Traditional Authority",
                "GVH": "Group Village Head",
                "Village": "Village",
            }
        )
        worksheet.append([values[column] for column in EXCEL_COLUMNS])
        project_options = workbook.create_sheet(PROJECT_OPTIONS_SHEET)
        project_options.append(["project_id", "project"])
        project_options.append(["project-1", "Road Works"])
        return workbook

    def _workbook_bytes(self, workbook):
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()


class ExcelValidationListExporterTest(TestCase):
    def test_export_workbook_writes_headers_and_member_rows(self):
        result = self._selection_result()

        workbook = ExcelValidationListExporter(result, batch_id="batch-1").export_workbook()
        worksheet = workbook["Validation List"]

        self.assertEqual(
            [worksheet.cell(row=1, column=index).value for index in range(1, len(EXCEL_COLUMNS) + 1)],
            EXCEL_COLUMNS,
        )
        self.assertIn("primary_worker", EXCEL_COLUMNS)
        self.assertNotIn("participant", EXCEL_COLUMNS)
        self.assertNotIn("group_code", EXCEL_COLUMNS)
        self.assertEqual(worksheet["A2"].value, "batch-1")
        self.assertEqual(worksheet["B2"].value, ROW_TYPE_MAIN)
        self.assertEqual(self._value(worksheet, "District"), "District")
        self.assertEqual(self._value(worksheet, "TA"), "Traditional Authority")
        self.assertEqual(self._value(worksheet, "GVH"), "Group Village Head")
        self.assertEqual(self._value(worksheet, "Village"), "Village")
        self.assertEqual(self._value(worksheet, "form_number"), "FORM-001")
        self.assertEqual(self._value(worksheet, "group_uuid"), "group-1")
        self.assertEqual(self._value(worksheet, "member_uuid"), "member-1")
        self.assertEqual(self._value(worksheet, "member_name"), "Ada Worker")
        self.assertEqual(self._value(worksheet, "national_id"), "NAT-001")
        self.assertEqual(self._value(worksheet, "fit_for_work"), "YES")
        self.assertEqual(self._value(worksheet, "relationship"), "HEAD")
        self.assertEqual(self._value(worksheet, "head"), "YES")
        self.assertEqual(
            self._value(worksheet, "household_wealth_quintile"),
            "Poorest",
        )
        self.assertEqual(self._value(worksheet, "current_recipient_type"), "PRIMARY")

        group = result.main[0].household.source
        self.assertEqual(group.code, "HH-001")

    def test_export_workbook_writes_exact_relationship_roles(self):
        workbook = ExcelValidationListExporter(
            self._selection_result(member_count=3),
            batch_id="batch-1",
        ).export_workbook()
        worksheet = workbook["Validation List"]
        relationship_column = EXCEL_COLUMNS.index("relationship") + 1

        self.assertEqual(worksheet.cell(2, relationship_column).value, "HEAD")
        self.assertEqual(worksheet.cell(3, relationship_column).value, "SPOUSE")
        self.assertEqual(worksheet.cell(4, relationship_column).value, "SON")

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
        self.assertEqual(project_options["C2"].value, "Road Works, Phase 1")
        formulas = {validation.formula1 for validation in worksheet.data_validations.dataValidation}
        self.assertIn('"YES,NO"', formulas)
        self.assertIn("'Project Options'!$C$2:$C$2", formulas)

    def test_export_workbook_disambiguates_duplicate_project_names(self):
        result = self._selection_result()
        projects = [
            SimpleNamespace(id="project-1", name="Road Works"),
            SimpleNamespace(id="project-2", name="Road Works"),
        ]

        workbook = ExcelValidationListExporter(
            result,
            batch_id="batch-1",
            projects=projects,
        ).export_workbook()
        project_options = workbook[PROJECT_OPTIONS_SHEET]

        self.assertEqual(project_options["C2"].value, "Road Works (project-1)")
        self.assertEqual(project_options["C3"].value, "Road Works (project-2)")

    def test_export_workbook_locks_structural_cells_and_unlocks_field_inputs(self):
        workbook = ExcelValidationListExporter(
            self._selection_result(),
            batch_id="batch-1",
        ).export_workbook()
        worksheet = workbook["Validation List"]

        self.assertTrue(worksheet.protection.sheet)
        for column in (
            "group_uuid",
            "national_id",
            "relationship",
            "household_wealth_quintile",
        ):
            self.assertTrue(self._cell(worksheet, column).protection.locked)
        for column in ("primary_worker", "verified", "validation_notes"):
            self.assertFalse(self._cell(worksheet, column).protection.locked)

    def test_export_workbook_writes_one_row_per_selected_eligible_member(self):
        result = self._selection_result(member_count=2)

        workbook = ExcelValidationListExporter(result, batch_id="batch-1").export_workbook()
        worksheet = workbook["Validation List"]

        self.assertEqual(worksheet.max_row, 3)
        self.assertEqual(self._value(worksheet, "member_uuid", 2), "member-1")
        self.assertEqual(self._value(worksheet, "member_uuid", 3), "member-2")

    def test_export_workbook_preserves_identifier_columns_as_text(self):
        result = self._selection_result()
        member = result.main[0].household.eligible_members[0]
        individual = member.source.individual
        individual.json_ext["form_number"] = "001234"
        individual.json_ext["national_id"] = "00123456"

        exported = ExcelValidationListExporter(
            result,
            batch_id="batch-1",
        ).export_bytes()
        worksheet = load_workbook(BytesIO(exported))["Validation List"]

        for column, expected in (
            ("form_number", "001234"),
            ("national_id", "00123456"),
        ):
            cell = self._cell(worksheet, column)
            self.assertEqual(cell.value, expected)
            self.assertEqual(cell.data_type, "s")
            self.assertEqual(cell.number_format, "@")

    def test_export_workbook_writes_stored_primary_worker_values(self):
        result = self._selection_result(member_count=3)
        stored_values = (True, False, None)
        for selected_member, stored_value in zip(
            result.main[0].household.eligible_members,
            stored_values,
        ):
            selected_member.source.json_ext = (
                {"primary_worker": stored_value}
                if stored_value is not None
                else {}
            )

        worksheet = ExcelValidationListExporter(
            result,
            batch_id="batch-1",
        ).export_workbook()["Validation List"]

        self.assertEqual(self._value(worksheet, "primary_worker", 2), "YES")
        self.assertEqual(self._value(worksheet, "primary_worker", 3), "NO")
        self.assertIsNone(self._value(worksheet, "primary_worker", 4))

    def _selection_result(self, member_count=1):
        location = self._location_tree()
        group = SimpleNamespace(id="group-1", code="HH-001", location=location)
        selected_members = []
        for index in range(1, member_count + 1):
            individual = SimpleNamespace(
                first_name="Ada" if index == 1 else "Grace",
                last_name="Worker",
                json_ext={
                    "form_number": "FORM-001",
                    "national_id": f"NAT-{index:03d}",
                },
            )
            group_individual = SimpleNamespace(individual=individual)
            selected_members.append(
                EligibleMember(
                    id=f"member-{index}",
                    gender="Female",
                    dob=date(1990, 1, index),
                    fit_for_work=True,
                    role=("HEAD" if index == 1 else "SPOUSE" if index == 2 else "SON"),
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

    def _cell(self, worksheet, column_name, row_number=2):
        return worksheet.cell(row_number, EXCEL_COLUMNS.index(column_name) + 1)

    def _value(self, worksheet, column_name, row_number=2):
        return self._cell(worksheet, column_name, row_number).value

    def _location_tree(self):
        district = SimpleNamespace(type="R", name="District", code="D01", parent=None)
        ta = SimpleNamespace(type="D", name="Traditional Authority", code="TA01", parent=district)
        gvh = SimpleNamespace(type="W", name="Group Village Head", code="GVH01", parent=ta)
        return SimpleNamespace(type="V", name="Village", code="V01", parent=gvh)


class UploadHardeningTest(TestCase):
    @patch("household_validation.services.GroupIndividualService")
    def test_primary_worker_update_preserves_json_and_recipient_type(
        self,
        service_class_mock,
    ):
        group_individual = SimpleNamespace(
            id="membership-1",
            group_id="group-1",
            recipient_type="SECONDARY",
            json_ext={"existing": "value"},
        )

        service = HouseholdValidationUploadService()
        service._apply_primary_worker(group_individual, True)
        service._apply_primary_worker(group_individual, False)

        service_class_mock.return_value.update.assert_has_calls(
            [
                call(
                    {
                        "id": "membership-1",
                        "group_id": "group-1",
                        "json_ext": {
                            "existing": "value",
                            "primary_worker": True,
                        },
                    }
                ),
                call(
                    {
                        "id": "membership-1",
                        "group_id": "group-1",
                        "json_ext": {
                            "existing": "value",
                            "primary_worker": False,
                        },
                    }
                ),
            ]
        )
        self.assertEqual(group_individual.recipient_type, "SECONDARY")

    @patch("household_validation.services.Group.objects.filter")
    def test_group_lookup_prefetches_members_and_individuals(self, filter_mock):
        group = SimpleNamespace(id="group-1")
        filtered_queryset = MagicMock()
        location_queryset = MagicMock()
        prefetched_queryset = MagicMock()
        filter_mock.return_value = filtered_queryset
        filtered_queryset.select_related.return_value = location_queryset
        location_queryset.prefetch_related.return_value = prefetched_queryset
        prefetched_queryset.first.return_value = group

        service = HouseholdValidationUploadService()
        result = service._group("group-1")
        cached_result = service._group("group-1")

        self.assertIs(result, group)
        self.assertIs(cached_result, group)
        filter_mock.assert_called_once_with(id="group-1")
        filtered_queryset.select_related.assert_called_once_with("location")
        location_queryset.prefetch_related.assert_called_once_with(
            "groupindividuals__individual"
        )

    def test_group_individual_lookup_reuses_prefetched_members(self):
        member = SimpleNamespace(
            individual_id="individual-1",
            is_deleted=False,
        )
        group = SimpleNamespace(
            _prefetched_objects_cache={"groupindividuals": [member]},
        )
        service = HouseholdValidationUploadService()

        with patch(
            "household_validation.services.GroupIndividual.objects.filter"
        ) as filter_mock:
            result = service._group_individual("individual-1", group)

        self.assertIs(result, member)
        filter_mock.assert_not_called()

    @patch("household_validation.services.GroupIndividual.objects.filter")
    def test_group_individual_lookup_uses_exported_individual_id(
        self,
        filter_mock,
    ):
        group = SimpleNamespace(id="group-1")
        group_individual = SimpleNamespace(
            id="membership-1",
            individual_id="individual-1",
            group=group,
        )
        queryset = MagicMock()
        queryset.select_related.return_value.first.return_value = group_individual
        filter_mock.return_value = queryset

        result = HouseholdValidationUploadService()._group_individual(
            "individual-1",
            group=group,
        )

        self.assertIs(result, group_individual)
        filter_mock.assert_called_once_with(
            individual_id="individual-1",
            group=group,
            is_deleted=False,
        )
        queryset.select_related.assert_called_once_with("individual")

    def test_json_safe_converts_nested_dates_to_iso_strings(self):
        raw_row = {
            "member_dob": date(1963, 10, 21),
            "validation": {
                "validation_date": date(2026, 7, 29),
            },
        }

        serialized = _json_safe(raw_row)

        self.assertEqual(serialized["member_dob"], "1963-10-21")
        self.assertEqual(
            serialized["validation"]["validation_date"],
            "2026-07-29",
        )

    def test_build_validation_json_ext_persists_not_verified_status(self):
        uploaded_row = UploadedValidationRow(
            row_number=2,
            values={},
            verified=False,
            primary_worker=None,
            validation_date=date(2026, 7, 3),
            project_name="Road Works",
            project_id="project-1",
            notes="Not available",
        )

        json_ext = build_validation_json_ext(
            uploaded_row=uploaded_row,
            project=None,
            upload_date=date(2026, 7, 4),
            uploaded_at=SimpleNamespace(isoformat=lambda: "2026-07-04T09:00:00+00:00"),
            user_id="user-1",
        )

        self.assertEqual(json_ext["validation_status"], VALIDATION_STATUS_NOT_VERIFIED)
        self.assertEqual(json_ext["last_verified_date"], "2026-07-03")
        self.assertEqual(json_ext["validation_project_selection_type"], PROJECT_SELECTION_TYPE_INTENT)

    def test_member_structural_errors_detect_protected_field_tampering(self):
        group = SimpleNamespace(
            json_ext={
                "form_number": "FORM-001",
                "household_wealth_quintile": "Poorest",
            },
        )
        group_individual = SimpleNamespace(
            role="HEAD",
            recipient_type="PRIMARY",
            group=group,
            individual=SimpleNamespace(
                first_name="Ada",
                last_name="Worker",
                dob=date(1990, 1, 1),
                json_ext={
                    "gender": "Female",
                    "fit_for_work": True,
                    "national_id": "NAT-001",
                },
            ),
        )
        uploaded_row = UploadedValidationRow(
            row_number=2,
            values={
                "form_number": "FORM-999",
                "member_name": "Grace Worker",
                "national_id": "NAT-999",
                "member_gender": "Male",
                "member_dob": "1991-01-01",
                "member_age": "18",
                "fit_for_work": "NO",
                "relationship": "SPOUSE",
                "head": "NO",
                "household_wealth_quintile": "Richest",
                "current_recipient_type": "SECONDARY",
            },
            verified=None,
            primary_worker=None,
            validation_date=None,
            project_name=None,
            project_id=None,
            notes=None,
        )

        errors = member_structural_errors(uploaded_row, group_individual)

        self.assertIn("Row 2: form_number does not match the household member", errors)
        self.assertIn("Row 2: member_name does not match the household member", errors)
        self.assertIn("Row 2: national_id does not match the household member", errors)
        self.assertIn("Row 2: member_gender does not match the household member", errors)
        self.assertIn("Row 2: fit_for_work does not match the household member", errors)
        self.assertIn("Row 2: relationship does not match the household member", errors)
        self.assertIn(
            "Row 2: household_wealth_quintile does not match the household member",
            errors,
        )
        self.assertIn("Row 2: current_recipient_type does not match the household member", errors)

    def test_structural_errors_accept_integer_like_excel_ids(self):
        group = SimpleNamespace(
            json_ext={
                "form_number": "123456",
                "household_wealth_quintile": "Poorest",
            },
        )
        group_individual = SimpleNamespace(
            role="HEAD",
            recipient_type=None,
            group=group,
            individual=SimpleNamespace(
                first_name="Ada",
                last_name="Worker",
                dob=None,
                json_ext={
                    "fit_for_work": True,
                    "national_id": "123456",
                },
            ),
        )
        uploaded_row = UploadedValidationRow(
            row_number=2,
            values={
                "form_number": 123456.0,
                "national_id": 123456.0,
                "relationship": "HEAD",
                "household_wealth_quintile": "Poorest",
            },
            verified=None,
            primary_worker=None,
            validation_date=None,
            project_name=None,
            project_id=None,
            notes=None,
        )

        errors = member_structural_errors(uploaded_row, group_individual)

        self.assertNotIn(
            "Row 2: form_number does not match the household member",
            errors,
        )
        self.assertNotIn(
            "Row 2: national_id does not match the household member",
            errors,
        )

    def test_form_number_maps_to_group_without_using_group_code(self):
        group = SimpleNamespace(
            code="INTERNAL-GROUP-CODE",
            json_ext={},
        )
        group_individual = SimpleNamespace(
            role="HEAD",
            recipient_type=None,
            group=group,
            individual=SimpleNamespace(
                first_name="Ada",
                last_name="Worker",
                dob=None,
                json_ext={
                    "fit_for_work": True,
                    "form_number": "FORM-001",
                    "national_id": "NAT-001",
                },
            ),
        )
        group.groupindividuals = _FakeRelatedManager([group_individual])
        uploaded_row = UploadedValidationRow(
            row_number=2,
            values={
                "form_number": "FORM-001",
                "national_id": "NAT-001",
                "relationship": "HEAD",
            },
            verified=None,
            primary_worker=None,
            validation_date=None,
            project_name=None,
            project_id=None,
            notes=None,
        )

        errors = member_structural_errors(
            uploaded_row,
            group_individual,
            group=group,
        )

        self.assertNotIn(
            "Row 2: form_number does not match the household member",
            errors,
        )
        self.assertEqual(group.code, "INTERNAL-GROUP-CODE")

    def test_upload_wealth_validation_uses_head_before_first_member(self):
        other_member = self._wealth_member(
            "other",
            role="SPOUSE",
            fit_for_work=True,
            wealth_quintile="Richest",
        )
        head = self._wealth_member(
            "head",
            role="HEAD",
            fit_for_work=True,
            wealth_quintile="Poorer",
        )
        group = SimpleNamespace(
            json_ext={},
            groupindividuals=_FakeRelatedManager([other_member, head]),
        )
        other_member.group = group
        uploaded_row = UploadedValidationRow(
            row_number=2,
            values={
                "relationship": "SPOUSE",
                "household_wealth_quintile": "Poorer",
            },
            verified=None,
            primary_worker=None,
            validation_date=None,
            project_name=None,
            project_id=None,
            notes=None,
        )

        errors = member_structural_errors(uploaded_row, other_member)

        self.assertNotIn(
            "Row 2: household_wealth_quintile does not match "
            "the household member",
            errors,
        )

    def test_wealth_resolver_ignores_members_who_are_not_fit_for_work(self):
        ineligible_member = self._wealth_member(
            "ineligible",
            role="SPOUSE",
            fit_for_work=False,
            wealth_quintile="Richest",
        )
        eligible_member = self._wealth_member(
            "eligible",
            role="SON",
            fit_for_work=True,
            wealth_quintile="Middle",
        )
        group = SimpleNamespace(
            json_ext={},
            groupindividuals=_FakeRelatedManager(
                [ineligible_member, eligible_member]
            ),
        )

        self.assertEqual(get_household_wealth_quintile(group), "Middle")

    def test_export_selection_uses_shared_wealth_precedence(self):
        other_member = self._wealth_member(
            "other",
            role="SPOUSE",
            fit_for_work=True,
            wealth_quintile="Richest",
        )
        head = self._wealth_member(
            "head",
            role="HEAD",
            fit_for_work=True,
            wealth_quintile="Poorest",
        )
        group = SimpleNamespace(
            id="group-1",
            code="HH-001",
            json_ext={},
            groupindividuals=_FakeRelatedManager([other_member, head]),
        )

        household = EligibleHouseholdSelectionService()._build_household(group)

        self.assertEqual(household.wealth_quintile, "Poorest")

    def _wealth_member(self, member_id, role, fit_for_work, wealth_quintile):
        individual = SimpleNamespace(
            id=f"individual-{member_id}",
            dob=date(1990, 1, 1),
            json_ext={
                "fit_for_work": fit_for_work,
                "household_wealth_quintile": wealth_quintile,
            },
        )
        return SimpleNamespace(
            id=f"membership-{member_id}",
            individual_id=individual.id,
            individual=individual,
            role=role,
            recipient_type=None,
        )

    def test_build_validation_error_report_writes_error_rows_csv(self):
        batch = SimpleNamespace(
            id="batch-1",
            rows=_FakeRows(
                [
                    SimpleNamespace(
                        row_number=2,
                        status="ERROR",
                        raw_row={
                            "form_number": "FORM-001",
                            "group_uuid": "group-1",
                            "member_uuid": "member-1",
                        },
                        error_message="form_number does not match",
                    )
                ]
            ),
        )

        report = build_validation_error_report_csv(batch.id, batch.rows.order_by("row_number"))

        self.assertIn("batch_id,row_number,status,form_number,group_uuid,member_uuid,error_message", report)
        self.assertIn("batch-1,2,ERROR,FORM-001,group-1,member-1,form_number does not match", report)

    def test_error_report_uses_legacy_group_code_as_form_number(self):
        row = SimpleNamespace(
            row_number=2,
            status="ERROR",
            raw_row={
                "group_code": "LEGACY-001",
                "group_uuid": "group-1",
                "member_uuid": "member-1",
            },
            error_message="legacy upload error",
        )

        report = build_validation_error_report_csv("batch-1", [row])

        self.assertIn(
            "batch-1,2,ERROR,LEGACY-001,group-1,member-1,legacy upload error",
            report,
        )


class LocalDateTests(TestCase):
    """openIMIS runs with USE_TZ = False, so timezone.now() is naive.

    Passing a naive datetime to timezone.localdate()/localtime() raises
    ValueError("localtime() cannot be applied to a naive datetime"), which
    previously aborted every non-dry-run validation-list upload.
    """

    def test_naive_datetime_returns_its_date(self):
        naive = datetime(2026, 7, 29, 12, 27)
        self.assertIsNone(naive.tzinfo)
        self.assertEqual(_local_date(naive), date(2026, 7, 29))

    def test_aware_datetime_is_converted_to_local_date(self):
        aware = datetime(2026, 7, 29, 12, 27, tzinfo=datetime_timezone.utc)
        self.assertEqual(_local_date(aware), timezone.localtime(aware).date())


class _FakeRows:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, **kwargs):
        return self

    def order_by(self, *fields):
        return self.rows
