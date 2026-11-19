"""Saved individual validation lists: generation, authorized reads and pure parsing."""
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from individual.data_source import DEFAULT_DATA_SOURCE
from individual.models import Individual

from household_validation.apps import HouseholdValidationConfig
from household_validation.gql_permissions import require_permissions
from household_validation.individual_workbook import (
    IndividualValidationListExporter, IndividualWorkbookParseResult, load_individual_workbook,
    parse_individual_validation_workbook, validate_individual_columns, workbook_batch_id,
)
from household_validation.models import HouseholdValidationBatch
from household_validation.programmes import (
    INDIVIDUAL, resolve_programme_columns, resolve_programme_rule, resolve_programme_subject, selection_service,
)


def _json_snapshot(value):
    try:
        return json.loads(json.dumps(value, cls=DjangoJSONEncoder, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Individual snapshot contains an unsupported value.") from exc


class IndividualValidationListService:
    def __init__(self, user):
        self.user = user

    def _require(self, permissions):
        require_permissions(self.user, permissions, error_class=PermissionDenied)

    def generate(self, benefit_plan_code, target_count=None, exclude_validated=False, **geography):
        self._require(HouseholdValidationConfig.gql_mutation_generate_household_validation_list_perms)
        if resolve_programme_subject(benefit_plan_code) != INDIVIDUAL:
            raise ValidationError("Individual generation requires an INDIVIDUAL programme.")
        columns = resolve_programme_columns(benefit_plan_code)
        validate_individual_columns(columns)
        rule = resolve_programme_rule(benefit_plan_code)
        rule["data_source"] = rule.get("data_source", DEFAULT_DATA_SOURCE)
        with transaction.atomic():
            selection = selection_service(benefit_plan_code, self.user).select(
                benefit_plan_code, target_count=target_count, exclude_validated=exclude_validated, **geography,
            )
            individuals = selection.individuals.select_related("location__parent__parent__parent")
            rows = [self._snapshot_row(person, columns) for person in individuals]
            payload = _json_snapshot({
                "subject": INDIVIDUAL,
                "benefit_plan_code": benefit_plan_code.strip().upper(),
                "rule": rule,
                "data_source": rule["data_source"],
                "columns": columns,
                "filters": {**geography, "target_count": target_count, "exclude_validated": exclude_validated},
                "eligible_individuals": selection.eligible_individuals,
                "selected_individuals": len(rows),
                "selection": rows,
            })
            batch = HouseholdValidationBatch(
                district_id=geography.get("district_id"), ta_id=geography.get("ta_id"),
                village_id=geography.get("village_id"), target_count=target_count,
                hotspot_code=geography.get("hotspot_code"), catchment_code=geography.get("catchment_code"),
                generated_at=timezone.now(), json_ext=payload,
            )
            batch.save(user=self.user)
            return batch, IndividualValidationListExporter(batch).export_bytes()

    @staticmethod
    def _snapshot_row(person, columns):
        extra = person.json_ext if isinstance(person.json_ext, dict) else {}
        row = {
            "individual_uuid": str(person.pk), "first_name": person.first_name,
            "last_name": person.last_name, "dob": person.dob,
            "national_id": str(extra["national_id"]) if extra.get("national_id") is not None else None,
            "gender": extra.get("gender"), "decision": None, "validation_notes": None,
            "District": None, "TA": None, "GVH": None, "Village": None,
        }
        location, seen = person.location, set()
        names = {"R": "District", "D": "TA", "W": "GVH", "V": "Village"}
        while location is not None and location.pk not in seen:
            seen.add(location.pk)
            if location.type in names:
                row[names[location.type]] = location.name or location.code
            location = location.parent
        for col in columns:
            value = extra.get(col["target_individual_json_ext_key"])
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            row[col["column_name"]] = value
        return row

    def get_batch(self, batch_id, permissions=None):
        self._require(
            HouseholdValidationConfig.gql_query_household_validation_rule_perms if permissions is None else permissions,
        )
        try:
            batch = HouseholdValidationBatch.objects.get(pk=batch_id, is_deleted=False)
        except (HouseholdValidationBatch.DoesNotExist, ValueError, ValidationError) as exc:
            raise ValidationError("Unknown individual validation batch.") from exc
        data = batch.json_ext or {}
        if data.get("subject") != INDIVIDUAL or not isinstance(data.get("selection"), list):
            raise ValidationError("Batch is not a saved individual validation list.")
        ids = {row["individual_uuid"] for row in data["selection"]}
        if ids:
            scoped = Individual.get_queryset(Individual.objects.filter(is_deleted=False, pk__in=ids), self.user)
            accessible = {str(pk) for pk in scoped.values_list("pk", flat=True).distinct()}
            if accessible != ids:
                raise PermissionDenied("The saved list contains individuals outside your current access.")
        elif batch.user_created_id != self.user.id and not getattr(self.user, "is_imis_admin", False):
            raise PermissionDenied("You cannot access this empty validation list.")
        return batch

    def preview(self, batch_id, first=20, offset=0):
        if type(first) is not int or not 0 <= first <= 100 or type(offset) is not int or offset < 0:
            raise ValidationError("first must be between 0 and 100 and offset must be non-negative.")
        batch = self.get_batch(batch_id)
        data = batch.json_ext
        return {
            "batch_id": batch.pk, "eligible_individuals": data["eligible_individuals"],
            "selected_individuals": data["selected_individuals"], "columns": data["columns"],
            "items": [{"batch_id": str(batch.pk), **row} for row in data["selection"][offset:offset + first]],
            "has_next_page": offset + first < data["selected_individuals"],
        }

    def export(self, batch_id):
        batch = self.get_batch(batch_id)
        return batch, IndividualValidationListExporter(batch).export_bytes()

    def parse(self, file_or_bytes):
        self._require(HouseholdValidationConfig.gql_mutation_upload_household_validation_list_perms)
        workbook = None
        try:
            workbook = load_individual_workbook(file_or_bytes)
            batch_id, _ = workbook_batch_id(workbook)
            batch = self.get_batch(
                batch_id, HouseholdValidationConfig.gql_mutation_upload_household_validation_list_perms,
            )
            return parse_individual_validation_workbook(workbook, batch)
        except ValidationError as exc:
            return IndividualWorkbookParseResult(errors=exc.messages)
        finally:
            if workbook is not None:
                workbook.close()
