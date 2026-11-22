import logging

from django.apps import AppConfig
from django.core.exceptions import ValidationError

from core.module_config_registry import register_validator

logger = logging.getLogger(__name__)

MODULE_NAME = "household_validation"

# Config keys that used to be flat HouseholdValidationConfig class attributes.
#
# Kept only so _load_config can warn a deployment whose saved ModuleConfiguration
# still sets them that the override no longer does anything.
RETIRED_CONFIG_KEYS = (
    "female_headed_percentage",
    "youth_percentage",
    "reserve_percentage",
    "business_columns_enabled",
    "business_type_options",
)

REMOVED_PROGRAM_EXPORT_COLUMN_KEYS = frozenset({
    "business_type",
    "business_duration",
})

RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT = 958001
RIGHT_HOUSEHOLD_VALIDATION_UPLOAD = 958002
RIGHT_HOUSEHOLD_VALIDATION_HISTORY = 958003
RIGHT_HOUSEHOLD_VALIDATION_ERROR_REPORT = 958004

RIGHT_GROUP_SEARCH = 180001
RIGHT_GROUP_CREATE = 180002
RIGHT_GROUP_UPDATE = 180003
RIGHT_GROUP_DELETE = 180004

ROLE_DISTRICT_ADMINISTRATOR = "District Administrator"
ROLE_DISTRICT_PROGRAM_MANAGER = "District Program Manager"
ROLE_DISTRICT_USER = "District User"

HOUSEHOLD_VALIDATION_RIGHTS = [
    RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT,
    RIGHT_HOUSEHOLD_VALIDATION_UPLOAD,
    RIGHT_HOUSEHOLD_VALIDATION_HISTORY,
    RIGHT_HOUSEHOLD_VALIDATION_ERROR_REPORT,
]

GROUP_RIGHTS = [
    RIGHT_GROUP_SEARCH,
    RIGHT_GROUP_CREATE,
    RIGHT_GROUP_UPDATE,
    RIGHT_GROUP_DELETE,
]

DISTRICT_VALIDATION_ROLES = [
    ROLE_DISTRICT_ADMINISTRATOR,
    ROLE_DISTRICT_PROGRAM_MANAGER,
    ROLE_DISTRICT_USER,
]

DISTRICT_VALIDATION_ROLE_RIGHTS = {
    ROLE_DISTRICT_ADMINISTRATOR: HOUSEHOLD_VALIDATION_RIGHTS + [
        RIGHT_GROUP_SEARCH,
        RIGHT_GROUP_UPDATE,
    ],
    ROLE_DISTRICT_PROGRAM_MANAGER: HOUSEHOLD_VALIDATION_RIGHTS + [
        RIGHT_GROUP_SEARCH,
        RIGHT_GROUP_UPDATE,
    ],
    ROLE_DISTRICT_USER: HOUSEHOLD_VALIDATION_RIGHTS + [
        RIGHT_GROUP_SEARCH,
        RIGHT_GROUP_UPDATE,
    ],
}

DEFAULT_CONFIG = {
    "gql_query_household_validation_rule_perms": [
        str(RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT),
    ],
    "gql_mutation_generate_household_validation_list_perms": [
        str(RIGHT_HOUSEHOLD_VALIDATION_QUERY_EXPORT),
    ],
    "gql_mutation_upload_household_validation_list_perms": [
        str(RIGHT_HOUSEHOLD_VALIDATION_UPLOAD),
    ],
    "gql_query_household_validation_history_perms": [
        str(RIGHT_HOUSEHOLD_VALIDATION_HISTORY),
    ],
    "gql_query_household_validation_error_report_perms": [
        str(RIGHT_HOUSEHOLD_VALIDATION_ERROR_REPORT),
    ],
    "group_search_perms": [str(RIGHT_GROUP_SEARCH)],
    "group_update_perms": [str(RIGHT_GROUP_UPDATE)],
    # Program Specific Eligibility + selection-strategy rules.
    #
    # - subject: HOUSEHOLD when omitted; INDIVIDUAL enables the individual
    #   candidate service for that programme (see README's UMEP section).
    # - data_source: optional individual source key; defaults to the same
    #   legacy UBR source as enrolment when omitted.
    # - selection_strategy: presence/absence picks the algorithm. Omitted
    #   (the default), all eligible households are selected, up to
    #   target_count.
    # - requires_data_source: a member must have this value in their
    #   Individual.json_ext "data_source" key.
    # - requires_recipient_type: a member's GroupIndividual.recipient_type
    #   must match this value.
    # - member_flag: a member must have this Individual.json_ext boolean
    #   key to count towards the household's eligible members (a hard
    #   requirement, unlike priority_flag below).
    # - member_min_age / member_max_age: inclusive age bounds a member must
    #   also fall within to be eligible.
    # - priority_flag: a boolean key that ranks eligible households ahead
    #   of the rest of the pool when capping at target_count.
    "program_eligibility_rules": {
        "PWP": {
            "member_flag": "fit_for_work",
            "selection_strategy": {
                "female_headed_percentage": 40,
                "youth_headed_percentage": 40,
                "reserve_percentage": 20,
                "allocate_by_village": True,
            },
        },
    },
    # Per-program Excel export/upload columns, entirely config-driven.
    #
    # Each entry describes one extra column beyond the base schema:
    # - key: stable id, referenced by depends_on and used as the upload-side
    #   registry lookup key. Not shown to the field officer.
    # - column_name: the Excel header text.
    # - target_individual_json_ext_key: where the value is stored/read on
    #   Individual.json_ext.
    # - type: "select" (dropdown, needs options), "number" (needs optional
    #   min/max), or "text" (freeform, no validation).
    # - required: if depends_on is absent or satisfied for a row, a blank
    #   value is an upload row-error -- but this never affects VERIFIED
    #   status, which depends only on primary_worker.
    # - depends_on (optional): {key, equals} -- the cell is only
    #   editable/checked when the referenced column's value on that row
    #   equals `equals`.
    # Protected individual workbook fields, keyed by programme. Each entry has
    # column_name and exactly one of json_ext_key or location_type (R/D/W/V).
    # Deployments choose their own identity keys and geography labels.
    "individual_workbook_fields": {},
    "program_specific_export_columns": {
        "PWP": [
            {
                "key": "has_business",
                "column_name": "Does member have a business",
                "target_individual_json_ext_key": "business_experience",
                "type": "select",
                "options": ["Yes", "No"],
                "required": False,
            },
        ],
    },
    # Deployment override for just a column's `options` list, keyed by the
    # column's `key` -- not by program. Applies
    # wherever that key appears, across every program's column list, in both
    # export (ExcelValidationListExporter) and upload
    # (upload.py::_column_registry) so the two stay consistent.
    #
    # Exists so a deployment can swap a select column's options without
    # having to redeclare
    # program_specific_export_columns (or program_eligibility_rules) at all:
    #
    #   {"export_column_options_overrides": {"custom_choice": ["Option A", "Option B"]}}
    #
    # is a complete, valid ModuleConfiguration override on its own. Applied
    # via apps.py::apply_column_option_overrides.
    "export_column_options_overrides": {},
}


class HouseholdValidationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME

    gql_query_household_validation_rule_perms = None
    gql_mutation_generate_household_validation_list_perms = None
    gql_mutation_upload_household_validation_list_perms = None
    gql_query_household_validation_history_perms = None
    gql_query_household_validation_error_report_perms = None
    group_search_perms = None
    group_update_perms = None
    program_eligibility_rules = None
    program_specific_export_columns = None
    individual_workbook_fields = None
    export_column_options_overrides = None

    @classmethod
    def _load_config(cls, cfg):
        """
        Load config fields that match current AppConfig class fields.
        """
        from household_validation.programmes import individual_workbook_fields, programme_rules

        if "program_eligibility_rules" in cfg:
            programme_rules(cfg["program_eligibility_rules"])
        if "individual_workbook_fields" in cfg:
            individual_workbook_fields(cfg["individual_workbook_fields"])
        stale_keys = [key for key in RETIRED_CONFIG_KEYS if key in cfg]
        if stale_keys:
            logger.warning(
                "household_validation ModuleConfiguration still sets %s, "
                "which no longer has any effect. "
                "Refer to the household_validation ModuleConfiguration "
                "documentation for the current config keys.",
                ", ".join(stale_keys),
            )
        for field in cfg:
            if hasattr(cls, field):
                setattr(cls, field, cfg[field])

    def ready(self):
        from core.models import ModuleConfiguration
        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self._load_config(cfg)
        register_validator(self.name, self._validate_module_config)

    def _validate_module_config(self, instance):
        """Reject invalid rules on save, before the next application restart."""
        from household_validation.programmes import individual_workbook_fields, programme_rules

        saved = instance._cfg
        if not isinstance(saved, dict):
            raise ValidationError({"config": "Module configuration must be an object."})
        config = {**DEFAULT_CONFIG, **saved}
        try:
            programme_rules(config["program_eligibility_rules"])
            individual_workbook_fields(config["individual_workbook_fields"])
        except ValidationError as exc:
            raise ValidationError({"config": exc.messages}) from exc


def apply_column_option_overrides(columns):
    """Apply HouseholdValidationConfig.export_column_options_overrides on top of a
    resolved list of column definitions (see DEFAULT_CONFIG's
    "export_column_options_overrides" entry above for the override's shape/intent).

    Called by both the export path (services.py::_resolve_export_columns)
    and the upload path (upload.py::_column_registry) so a deployment's
    override is honored consistently by both.
    """
    overrides = getattr(HouseholdValidationConfig, "export_column_options_overrides", None) or {}
    if not overrides:
        return columns
    return [
        {**col, "options": overrides[col["key"]]}
        if col.get("type") == "select" and col["key"] in overrides
        else col
        for col in columns
    ]


def remove_retired_program_export_columns(columns):
    return [
        column for column in columns
        if column.get("key") not in REMOVED_PROGRAM_EXPORT_COLUMN_KEYS
    ]
