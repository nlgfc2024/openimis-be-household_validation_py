"""Saved individual validation lists: generation, authorized reads and pure parsing."""
import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from individual.data_source import DEFAULT_DATA_SOURCE
from individual.models import Individual

from household_validation.apps import HouseholdValidationConfig
from household_validation.gql_permissions import require_permissions
from household_validation.individual_snapshots import (
    SELECTION_ROW_KIND, SELECTION_STORAGE, is_individual_list, iter_selection,
    selected_individual_ids, selection_rows,
)
from household_validation.individual_workbook import (
    IndividualValidationListExporter, IndividualWorkbookParseResult, load_individual_workbook,
    parse_individual_validation_workbook, validate_individual_columns, workbook_batch_id,
)
from household_validation.models import HouseholdValidationBatch, HouseholdValidationBatchRow
from household_validation.programmes import (
    INDIVIDUAL, resolve_individual_workbook_fields, resolve_programme_columns,
    resolve_programme_rule, resolve_programme_subject, selection_service,
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
        fields = resolve_individual_workbook_fields(benefit_plan_code)
        validate_individual_columns(columns, fields)
        rule = resolve_programme_rule(benefit_plan_code)
        rule["data_source"] = rule.get("data_source", DEFAULT_DATA_SOURCE)
        with transaction.atomic():
            selection = selection_service(benefit_plan_code, self.user).select(
                benefit_plan_code, target_count=target_count, exclude_validated=exclude_validated, **geography,
            )
            payload = _json_snapshot({
                "subject": INDIVIDUAL,
                "benefit_plan_code": benefit_plan_code.strip().upper(),
                "rule": rule,
                "data_source": rule["data_source"],
                "columns": columns,
                "display_fields": fields,
                "filters": {**geography, "target_count": target_count, "exclude_validated": exclude_validated},
                "eligible_individuals": selection.eligible_individuals,
                "selected_individuals": selection.selected_individuals,
                "selection_storage": SELECTION_STORAGE,
            })
            batch = HouseholdValidationBatch(
                district_id=geography.get("district_id"), ta_id=geography.get("ta_id"),
                village_id=geography.get("village_id"), target_count=target_count,
                hotspot_code=geography.get("hotspot_code"), catchment_code=geography.get("catchment_code"),
                generated_at=timezone.now(), json_ext=payload,
            )
            batch.save(user=self.user)
            individuals = selection.individuals.select_related("location__parent__parent__parent")
            pending, count = [], 0
            for count, person in enumerate(individuals.iterator(chunk_size=500), start=1):
                pending.append({
                    "batch_id": batch.pk, "individual_id": person.pk, "row_number": count + 1,
                    "raw_row": _json_snapshot(self._snapshot_row(person, columns, fields)),
                    "json_ext": {"kind": SELECTION_ROW_KIND},
                })
                if len(pending) == 500:
                    HouseholdValidationBatchRow.bulk_save(pending, self.user, batch_size=500)
                    pending = []
            HouseholdValidationBatchRow.bulk_save(pending, self.user, batch_size=500)
            if count != selection.selected_individuals:
                raise ValidationError("Selected individuals disappeared during generation; generate the list again.")
            return batch, IndividualValidationListExporter(batch).export_bytes()

    @staticmethod
    def _snapshot_row(person, columns, fields):
        extra = person.json_ext if isinstance(person.json_ext, dict) else {}
        row = {
            "individual_uuid": str(person.pk), "first_name": person.first_name,
            "last_name": person.last_name, "dob": person.dob, "decision": None, "validation_notes": None,
        }
        location, seen, names = person.location, set(), {}
        while location is not None and location.pk not in seen:
            seen.add(location.pk)
            names[location.type] = location.name or location.code
            location = location.parent
        for col in fields:
            value = names.get(col["location_type"]) if "location_type" in col else extra.get(col["json_ext_key"])
            if value is not None and "json_ext_key" in col:
                value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            row[col["column_name"]] = value
        for col in columns:
            # Editable answers must be deliberate submissions, not stale registry
            # values silently echoed back after the workbook has been in the field.
            row[col["column_name"]] = None
        return row

    def scoped_individuals(self, batch):
        return Individual.get_queryset(Individual.objects.filter(
            is_deleted=False, pk__in=selected_individual_ids(batch),
        ), self.user).distinct()

    def get_batch(self, batch_id, permissions=None):
        self._require(
            HouseholdValidationConfig.gql_query_household_validation_history_perms
            if permissions is None else permissions,
        )
        try:
            batch = HouseholdValidationBatch.objects.get(pk=batch_id, is_deleted=False)
        except (HouseholdValidationBatch.DoesNotExist, ValueError, ValidationError) as exc:
            raise ValidationError("Unknown individual validation batch.") from exc
        if not is_individual_list(batch):
            raise ValidationError("Batch is not a saved individual validation list.")
        owner = batch.user_created_id == self.user.id
        if not owner and not getattr(self.user, "is_imis_admin", False) and not self.scoped_individuals(batch).exists():
            raise PermissionDenied("No currently accessible individuals in this saved list; creator or admin required.")
        return batch

    def _read_context(self, batch):
        visible_ids = self.scoped_individuals(batch).values_list("pk", flat=True)
        current = Individual.objects.filter(pk__in=selected_individual_ids(batch)).aggregate(
            total=Count("pk"), deleted=Count("pk", filter=Q(is_deleted=True)),
        )
        available = visible_ids.count()
        counts = {
            "available_individuals": available,
            "deleted_individuals": current["deleted"],
            "missing_individuals": batch.json_ext["selected_individuals"] - current["total"],
            "inaccessible_individuals": current["total"] - current["deleted"] - available,
        }
        return visible_ids, counts

    def preview(self, batch_id, first=20, offset=0):
        if type(first) is not int or not 0 <= first <= 100 or type(offset) is not int or offset < 0:
            raise ValidationError("first must be between 0 and 100 and offset must be non-negative.")
        batch = self.get_batch(batch_id)
        data = batch.json_ext
        visible_ids, counts = self._read_context(batch)
        rows = selection_rows(batch).filter(individual_id__in=visible_ids).values_list("raw_row", flat=True)
        page = rows[offset:offset + first]
        return {
            "batch_id": batch.pk, "eligible_individuals": data["eligible_individuals"],
            "selected_individuals": data["selected_individuals"], "columns": data["columns"],
            "display_fields": data["display_fields"], **counts,
            "items": [{"batch_id": str(batch.pk), **row} for row in page],
            "has_next_page": offset + first < counts["available_individuals"],
        }

    def export(self, batch_id):
        batch = self.get_batch(batch_id)
        visible_ids, counts = self._read_context(batch)
        batch._individual_read_counts = counts
        return batch, IndividualValidationListExporter(
            batch, rows=iter_selection(batch, visible_ids), availability=counts,
        ).export_bytes()

    def parse(self, file_or_bytes):
        self._require(HouseholdValidationConfig.gql_mutation_upload_household_validation_list_perms)
        workbook = None
        try:
            workbook = load_individual_workbook(file_or_bytes)
            batch_id, _ = workbook_batch_id(workbook)
            batch = self.get_batch(
                batch_id, HouseholdValidationConfig.gql_mutation_upload_household_validation_list_perms,
            )
            result = parse_individual_validation_workbook(workbook, batch)
            submitted_ids = {row.individual_id for row in result.rows if row.individual_id}
            current = {str(pk): deleted for pk, deleted in Individual.objects.filter(
                pk__in=selected_individual_ids(batch),
            ).filter(pk__in=submitted_ids).values_list("pk", "is_deleted")}
            visible = {str(pk) for pk in self.scoped_individuals(batch).filter(pk__in=submitted_ids)
                       .values_list("pk", flat=True)}
            for row in result.rows:
                if row.errors:
                    continue
                if row.individual_id not in current:
                    row.errors.append(f"Row {row.row_number}: individual no longer exists")
                elif current[row.individual_id]:
                    row.errors.append(f"Row {row.row_number}: individual has been deleted")
                elif row.individual_id not in visible:
                    row.errors.append(f"Row {row.row_number}: individual is outside your current access")
                if row.errors:
                    row.updates, row.notes = {}, None
            return result
        except ValidationError as exc:
            return IndividualWorkbookParseResult(errors=exc.messages)
        finally:
            if workbook is not None:
                workbook.close()
