"""Programme resolution shared by household and individual validation."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from individual.data_source import DEFAULT_DATA_SOURCE

from household_validation.apps import DEFAULT_CONFIG, HouseholdValidationConfig, apply_column_option_overrides
from household_validation.verification import INDIVIDUAL_DECISIONS


HOUSEHOLD = "HOUSEHOLD"
INDIVIDUAL = "INDIVIDUAL"


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
        subject = rule.get("subject", HOUSEHOLD)
        if subject not in (HOUSEHOLD, INDIVIDUAL):
            raise ValidationError(f"Unsupported validation subject for {code}: {subject}")
        # Individual v1 selects by source, geography and absence of active membership.
        # Never silently apply/ignore household member or quota rules here.
        if subject == INDIVIDUAL:
            if set(rule) - {"subject", "data_source"}:
                raise ValidationError(f"Individual validation rule for {code} supports only subject and data_source.")
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
