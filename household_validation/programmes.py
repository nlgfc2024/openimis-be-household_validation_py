"""Programme resolution shared by household and individual validation."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db.models import Q
from individual.data_source import DEFAULT_DATA_SOURCE

from household_validation.apps import DEFAULT_CONFIG, HouseholdValidationConfig, apply_column_option_overrides
from household_validation.verification import INDIVIDUAL_DECISIONS


HOUSEHOLD = "HOUSEHOLD"
INDIVIDUAL = "INDIVIDUAL"
INDIVIDUAL_RULE_KEYS = {"subject", "data_source", "requires_json_ext"}


def _json_ext_requirement(code, required):
    # Top-level json_ext keys with the exact scalar each candidate must carry, e.g. a desk appraisal result
    if not isinstance(required, dict) or not required:
        raise ValidationError(f"requires_json_ext for {code} must be a non-empty object.")
    for key, value in required.items():
        if not isinstance(key, str) or not key.strip():
            raise ValidationError(f"requires_json_ext keys for {code} must be non-empty strings.")
        if value is None or not isinstance(value, (str, int, float, bool)):
            raise ValidationError(f"requires_json_ext values for {code} must be strings, numbers or booleans.")


def json_ext_requirement_q(rule):
    """The rule's requires_json_ext as a filter on Individual (json_ext contains every key and value)."""
    required = rule.get("requires_json_ext")
    return Q(json_ext__contains=required) if required else Q()


def programme_rules(rules):
    if not isinstance(rules, dict):
        raise ValidationError("program_eligibility_rules must be an object.")
    normalised = {}
    for code, rule in rules.items():
        if not isinstance(code, str) or not code.strip():
            raise ValidationError("Programme codes must be non-empty strings.")
        code = code.strip().upper()
        if code in normalised:
            raise ValidationError(f"Duplicate validation programme code: {code}")
        if not isinstance(rule, dict):
            raise ValidationError(f"Validation rule for {code} must be an object.")
        if "requires_primary_worker" in rule and not isinstance(rule["requires_primary_worker"], bool):
            raise ValidationError(f"requires_primary_worker for {code} must be a boolean.")
        if (
            "require_primary_worker_national_id" in rule
            and not isinstance(rule["require_primary_worker_national_id"], bool)
        ):
            raise ValidationError(
                f"require_primary_worker_national_id for {code} must be a boolean."
            )
        subject = rule.get("subject", HOUSEHOLD)
        if subject not in (HOUSEHOLD, INDIVIDUAL):
            raise ValidationError(f"Unsupported validation subject for {code}: {subject}")
        # Individual v1 selects by source, geography and absence of active membership.
        # Never silently apply/ignore household member or quota rules here.
        if subject == INDIVIDUAL:
            if set(rule) - INDIVIDUAL_RULE_KEYS:
                raise ValidationError(
                    f"Individual validation rule for {code} supports only subject, data_source and requires_json_ext."
                )
            if "requires_json_ext" in rule:
                _json_ext_requirement(code, rule["requires_json_ext"])
            source = rule.get("data_source", DEFAULT_DATA_SOURCE)
            if not isinstance(source, str) or not source.strip():
                raise ValidationError(f"Individual validation data_source for {code} must be a non-empty string.")
            if "data_source" in rule:
                # Normalise on read without changing stored configuration or
                # case-sensitive source keys shared with enrolment.
                rule = {**rule, "data_source": source.strip()}
        normalised[code] = rule
    return normalised


def resolve_programme_rule(benefit_plan_code=None):
    rules = programme_rules(HouseholdValidationConfig.program_eligibility_rules or {})
    if not benefit_plan_code:
        return deepcopy(rules.get("PWP") or DEFAULT_CONFIG["program_eligibility_rules"]["PWP"])
    code = benefit_plan_code.strip().upper()
    if code not in rules:
        raise ValidationError(f"No eligibility rule configured for benefit plan code '{benefit_plan_code}'")
    return deepcopy(rules[code])


def requires_primary_worker_national_id(benefit_plan_code=None):
    """Whether the programme requires an ID for its selected primary worker.

    An omitted programme code follows the module's normal PWP fallback.  An
    explicitly unknown code remains an error so a configuration problem is not
    silently converted into a less strict upload.
    """
    rule = resolve_programme_rule(benefit_plan_code)
    return bool(rule.get("require_primary_worker_national_id", False))


def resolve_programme_subject(benefit_plan_code=None):
    return resolve_programme_rule(benefit_plan_code).get("subject", HOUSEHOLD)


def resolve_programme_columns(benefit_plan_code=None):
    columns = HouseholdValidationConfig.program_specific_export_columns or {}
    columns = {str(code).strip().upper(): value for code, value in columns.items()}
    if not benefit_plan_code:
        resolved = columns.get("PWP") or DEFAULT_CONFIG["program_specific_export_columns"]["PWP"]
    else:
        resolved = columns.get(benefit_plan_code.strip().upper(), [])
    return deepcopy(apply_column_option_overrides(resolved))


def individual_workbook_fields(config):
    from household_validation.individual_workbook import validate_display_fields

    if not isinstance(config, dict):
        raise ValidationError("individual_workbook_fields must be a programme-keyed object.")
    resolved = {}
    for code, fields in config.items():
        if not isinstance(code, str) or not code.strip() or code.strip().upper() in resolved:
            raise ValidationError("Individual workbook programme codes must be non-empty and unique.")
        validate_display_fields(fields)
        resolved[code.strip().upper()] = deepcopy(fields)
    return resolved


def resolve_individual_workbook_fields(benefit_plan_code):
    fields = individual_workbook_fields(HouseholdValidationConfig.individual_workbook_fields or {})
    return fields.get(benefit_plan_code.strip().upper(), [])


def configured_programmes():
    rules = programme_rules(HouseholdValidationConfig.program_eligibility_rules or {})
    return [
        {"benefit_plan_code": code, "subject": rule.get("subject", HOUSEHOLD),
         "data_source": rule.get("data_source", DEFAULT_DATA_SOURCE) if rule.get("subject") == INDIVIDUAL else None,
         "columns": resolve_programme_columns(code),
         "decision_values": list(INDIVIDUAL_DECISIONS) if rule.get("subject") == INDIVIDUAL else None,
         "blank_decision_behavior": "SKIP_ROW" if rule.get("subject") == INDIVIDUAL else None}
        for code, rule in sorted(rules.items())
    ]


def selection_service(benefit_plan_code, user):
    """Select the subject service; batch generation/upload are separate work."""
    if resolve_programme_subject(benefit_plan_code) == INDIVIDUAL:
        from household_validation.individual_selection import EligibleIndividualSelectionService
        return EligibleIndividualSelectionService(user)
    from household_validation.services import EligibleHouseholdSelectionService
    return EligibleHouseholdSelectionService(user)
