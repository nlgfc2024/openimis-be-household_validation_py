"""Apply saved-list decisions with per-row checks and existing batch/row history."""
import csv
import math
from io import StringIO
from types import SimpleNamespace
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from individual.data_source import DEFAULT_DATA_SOURCE, data_source_q, record_data_source
from individual.models import GroupIndividual, Individual

from household_validation.apps import HouseholdValidationConfig
from household_validation.gql_permissions import require_permissions
from household_validation.individual_lists import _json_snapshot
from household_validation.individual_selection import EligibleIndividualSelectionService
from household_validation.individual_snapshots import (
    SELECTION_ROW_KIND, SELECTION_STORAGE, is_individual_list, selected_individual_ids,
)
from household_validation.individual_workbook import (
    IndividualValidationListExporter, ParsedIndividualValidationRow, load_individual_workbook,
    parse_individual_validation_workbook, workbook_batch_id,
)
from household_validation.models import HouseholdValidationBatch, HouseholdValidationBatchRow
from household_validation.programmes import json_ext_requirement_q
from household_validation.verification import VERIFIED, NOT_VERIFIED


def _audit_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _audit_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_audit_value(item) for item in value]
    return value


class IndividualValidationUploadService:
    def __init__(self, user):
        self.user = user

    def batches(self, first=20, offset=0):
        """Recent issued batches, with the same metadata access as attempt history."""
        require_permissions(self.user, HouseholdValidationConfig.gql_query_household_validation_history_perms,
                            error_class=PermissionDenied)
        if type(first) is not int or not 0 <= first <= 100 or type(offset) is not int or offset < 0:
            raise ValidationError("first must be between 0 and 100 and offset must be non-negative.")
        batches = HouseholdValidationBatch.objects.filter(
            is_deleted=False, generated_at__isnull=False,
            json_ext__subject="INDIVIDUAL", json_ext__selection_storage=SELECTION_STORAGE,
        )
        if not getattr(self.user, "is_imis_admin", False):
            visible = Individual.get_queryset(Individual.objects.filter(is_deleted=False), self.user)
            visible_rows = HouseholdValidationBatchRow.objects.filter(
                batch_id=OuterRef("pk"), is_deleted=False, upload_attempt_id__isnull=True,
                json_ext__kind=SELECTION_ROW_KIND, individual_id__in=visible.values("pk"),
            )
            batches = batches.annotate(_visible_selection=Exists(visible_rows)).filter(
                Q(user_created_id=self.user.id) | Q(_visible_selection=True),
            )
        count = batches.count()
        page = batches.order_by("-generated_at", "-date_created", "-pk").values(
            "pk", "generated_at", "uploaded_at", "source_file_name", "status",
            "json_ext__benefit_plan_code", "json_ext__selected_individuals",
        )[offset:offset + first]
        return {
            "count": count, "has_next_page": offset + first < count,
            "items": [{
                "batch_id": row["pk"], "benefit_plan_code": row["json_ext__benefit_plan_code"],
                "generated_at": row["generated_at"], "uploaded_at": row["uploaded_at"],
                "source_file_name": row["source_file_name"], "status": row["status"],
                "selected_individuals": row["json_ext__selected_individuals"],
            } for row in page],
        }

    def _context(self, batch_id, permissions, for_update=False):
        require_permissions(self.user, permissions, error_class=PermissionDenied)
        try:
            batches = HouseholdValidationBatch.objects
            if for_update:
                batches = batches.select_for_update()
            batch = batches.get(pk=batch_id, is_deleted=False)
        except (HouseholdValidationBatch.DoesNotExist, ValueError, ValidationError) as exc:
            raise ValidationError("Unknown individual validation batch.") from exc
        if not is_individual_list(batch):
            raise ValidationError("Batch is not a generated individual list.")
        selected = {str(pk) for pk in selected_individual_ids(batch)}
        existing = {str(pk) for pk in Individual.objects.filter(pk__in=selected).values_list("pk", flat=True)}
        scoped = Individual.get_queryset(Individual.objects.filter(pk__in=selected, is_deleted=False), self.user)
        # Lock base individual rows, not the joins/distinct used for row security.
        # A stable order also prevents overlapping batches locking people in reverse order.
        people = Individual.objects.filter(pk__in=scoped.values("pk")).order_by("pk")
        if for_update:
            people = people.select_for_update(of=("self",))
        visible = {str(person.pk): person for person in people}
        owner = batch.user_created_id == self.user.id
        if not visible and not owner and not getattr(self.user, "is_imis_admin", False):
            raise PermissionDenied("You cannot access this individual validation batch.")
        return batch, selected, existing, visible, scoped

    def _apply_saved_geography(self, queryset, geography):
        """Intersect saved scopes; a matching child must not bypass a changed parent."""
        scopes = (
            ("region_id", "region_code"),
            ("district_id", "district_code"),
            ("ta_id", "ta_code", "ta_codes"),
            ("gvh_codes",),
            ("village_id", "village_code", "village_codes"),
            ("hotspot_id", "hotspot_code"),
            ("catchment_id", "catchment_code"),
        )
        supported = {key for scope in scopes for key in scope}
        if set(geography) - supported:
            raise ValidationError("Batch contains unsupported saved geographic filters.")
        selector = EligibleIndividualSelectionService(self.user)
        for scope in scopes:
            values = {key: geography[key] for key in scope if geography.get(key)}
            if values:
                # Reuse the same ID/code alternatives within a level, while
                # enforcing each saved level independently during upload.
                queryset = selector._apply_location_filters(queryset, **values)
        return queryset

    def upload(self, file_or_bytes, dry_run=False, source_file_name=None):
        require_permissions(self.user, HouseholdValidationConfig.gql_mutation_upload_household_validation_list_perms,
                            error_class=PermissionDenied)
        if type(dry_run) is not bool:
            raise ValidationError("dry_run must be a boolean.")
        if source_file_name is not None and (not isinstance(source_file_name, str) or len(source_file_name) > 255):
            raise ValidationError("source_file_name must be at most 255 characters.")
        workbook = load_individual_workbook(file_or_bytes)
        try:
            batch_id, _ = workbook_batch_id(workbook)
            with transaction.atomic():
                batch, selected, existing, visible, scoped = self._context(
                    batch_id, HouseholdValidationConfig.gql_mutation_upload_household_validation_list_perms,
                    for_update=not dry_run,
                )
                parsed = parse_individual_validation_workbook(workbook, batch)
                geography = {key: value for key, value in batch.json_ext["filters"].items()
                             if key not in ("exclude_validated", "target_count")}
                source = batch.json_ext["rule"].get("data_source", DEFAULT_DATA_SOURCE)
                active = GroupIndividual.objects.filter(
                    individual_id__in=selected, is_deleted=False, group__is_deleted=False,
                    individual__is_deleted=False,
                ).values("individual_id")
                eligible = scoped.filter(data_source_q(source), json_ext_requirement_q(batch.json_ext["rule"]))
                eligible = eligible.exclude(pk__in=active)
                eligible = self._apply_saved_geography(eligible, geography)
                eligible_ids = {str(pk) for pk in eligible.values_list("pk", flat=True)}
                deleted_ids = {str(pk) for pk in Individual.objects.filter(
                    pk__in=selected, is_deleted=True,
                ).values_list("pk", flat=True)}
                rows = parsed.rows
                if parsed.errors and not rows:
                    rows = [ParsedIndividualValidationRow(1, None, {}, errors=list(parsed.errors))]
                prepared = []
                totals = dict(batch_id=batch.pk, upload_attempt_id=None, dry_run=dry_run, rows_read=parsed.rows_read,
                              rows_applied=0, rows_skipped=0, errors=0, error_messages=[], individuals_verified=0,
                              individuals_not_verified=0, individuals_updated=0, would_update=0)
                for row in rows:
                    errors = list(dict.fromkeys([*parsed.errors, *row.errors]))
                    person = visible.get(row.individual_id)
                    if row.individual_id in selected:
                        if row.individual_id not in existing:
                            errors.append("Individual no longer exists.")
                        elif row.individual_id in deleted_ids:
                            errors.append("Individual has been deleted.")
                        elif person is None:
                            errors.append("Individual is outside your current access.")
                        elif row.individual_id not in eligible_ids:
                            errors.append(
                                "Individual no longer meets the saved source, programme requirement, household or "
                                "geographic scope."
                            )
                    current = person.json_ext if person is not None and isinstance(person.json_ext, dict) else {}
                    updates = {}
                    if not errors and row.decision is not None:
                        updates = {**row.updates, "validation_status": row.decision}
                        notes = row.notes if row.notes and row.notes.strip() else None
                        if notes is not None or "validation_notes" in current:
                            updates["validation_notes"] = notes
                        prospective = dict(current)
                        prospective.update(updates)
                        if record_data_source(SimpleNamespace(json_ext=prospective)) != source:
                            errors.append("Configured fields cannot move the individual to another source.")
                    changed = bool(updates) and person is not None and any(
                        key not in current or current[key] != value for key, value in updates.items()
                    )
                    if errors:
                        status = HouseholdValidationBatchRow.Status.ERROR
                        totals["errors"] += 1
                        totals["error_messages"].extend(
                            error if error.startswith("Row ") else f"Row {row.row_number}: {error}" for error in errors
                        )
                    elif row.decision is None:
                        status = HouseholdValidationBatchRow.Status.SKIPPED
                        totals["rows_skipped"] += 1
                    else:
                        status = HouseholdValidationBatchRow.Status.APPLIED
                        totals["rows_applied"] += 1
                        totals["individuals_verified" if row.decision == VERIFIED else "individuals_not_verified"] += 1
                        totals["would_update"] += int(changed)
                    prepared.append((row, person, status, errors, updates, changed))
                totals["status"] = (
                    HouseholdValidationBatch.Status.PARTIAL_SUCCESS if totals["errors"] and totals["rows_applied"]
                    else HouseholdValidationBatch.Status.FAILED if totals["errors"]
                    else HouseholdValidationBatch.Status.PROCESSED
                )
                if dry_run:
                    return totals
                attempt = uuid4()
                uploaded_at = timezone.now()
                existing_selected = selected & existing
                for row, person, status, errors, updates, changed in prepared:
                    if status == HouseholdValidationBatchRow.Status.APPLIED and changed:
                        extra = dict(person.json_ext) if isinstance(person.json_ext, dict) else {}
                        extra.update(updates)
                        person.json_ext = extra
                        person.save(user=self.user)
                        totals["individuals_updated"] += 1
                    record = HouseholdValidationBatchRow(
                        batch=batch, upload_attempt_id=attempt, row_number=row.row_number,
                        individual_id=row.individual_id if row.individual_id in existing_selected else None,
                        verified=(row.decision == VERIFIED) if row.decision in (VERIFIED, NOT_VERIFIED) else None,
                        status=status, error_message="\n".join(errors) or None,
                        raw_row=_json_snapshot(_audit_value(row.values)),
                        json_ext=_json_snapshot({"decision": row.decision, "changed": changed if not errors else False,
                                                "updates": updates if not errors else {}}),
                    )
                    record.save(user=self.user)
                totals["upload_attempt_id"] = attempt
                data = dict(batch.json_ext)
                attempts = dict(data.get("upload_attempts", {}))
                attempts[str(attempt)] = _json_snapshot({
                    **{key: value for key, value in totals.items() if key != "error_messages"},
                    "uploaded_at": uploaded_at, "actor_id": str(self.user.id),
                    "source_file_name": source_file_name,
                })
                data["upload_attempts"] = attempts
                data["last_upload_attempt_id"] = str(attempt)
                data["last_upload_status"] = totals["status"]
                batch.json_ext = data
                successful = (
                    HouseholdValidationBatch.Status.PROCESSED, HouseholdValidationBatch.Status.PARTIAL_SUCCESS,
                )
                if totals["rows_applied"] or batch.status not in successful:
                    batch.status = totals["status"]
                batch.uploaded_at = uploaded_at
                batch.source_file_name = source_file_name
                batch.error_summary = (
                    f"{totals['errors']} row(s) failed validation in attempt {attempt}. See attempt rows for details."
                    if totals["errors"] else None
                )
                batch.save(user=self.user)
                return totals
        finally:
            workbook.close()

    def history(self, batch_id):
        batch, _, _, _, _ = self._context(
            batch_id, HouseholdValidationConfig.gql_query_household_validation_history_perms,
        )
        return sorted((batch.json_ext.get("upload_attempts") or {}).values(),
                      key=lambda item: (item["uploaded_at"], item["upload_attempt_id"]), reverse=True)

    def _attempt_rows(self, batch_id, attempt_id, permissions):
        batch, _, _, visible, _ = self._context(batch_id, permissions)
        try:
            attempt_id = str(UUID(str(attempt_id)))
        except ValueError as exc:
            raise ValidationError("Invalid upload attempt ID.") from exc
        if attempt_id not in (batch.json_ext.get("upload_attempts") or {}):
            raise ValidationError("Unknown upload attempt for this batch.")
        rows = batch.rows.filter(upload_attempt_id=attempt_id, is_deleted=False).order_by("row_number", "pk")
        return batch, rows, visible

    @staticmethod
    def _serialize_rows(rows, visible):
        for row in rows:
            allowed = str(row.individual_id) in visible
            yield _json_snapshot({
                "id": row.pk, "row_number": row.row_number, "status": row.status,
                "individual_id": row.individual_id if allowed else None,
                "raw_row": row.raw_row if allowed else {}, "json_ext": row.json_ext if allowed else {},
                "error_message": row.error_message if allowed or row.individual_id is None or not row.raw_row
                else "Row data is not currently accessible.", "redacted": not allowed,
            })

    def attempt_rows(self, batch_id, attempt_id, first=20, offset=0):
        if type(first) is not int or not 0 <= first <= 100 or type(offset) is not int or offset < 0:
            raise ValidationError("first must be between 0 and 100 and offset must be non-negative.")
        _, rows, visible = self._attempt_rows(
            batch_id, attempt_id, HouseholdValidationConfig.gql_query_household_validation_history_perms,
        )
        count = rows.count()
        return {
            "items": list(self._serialize_rows(rows[offset:offset + first], visible)),
            "count": count, "has_next_page": offset + first < count,
        }

    def error_report(self, batch_id, attempt_id):
        batch, rows, visible = self._attempt_rows(
            batch_id, attempt_id, HouseholdValidationConfig.gql_query_household_validation_error_report_perms,
        )
        headers = ["row_number", "status", "error_message", *IndividualValidationListExporter(batch).columns]
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        count = 0
        error_rows = rows.filter(status=HouseholdValidationBatchRow.Status.ERROR).iterator(chunk_size=500)
        for row in self._serialize_rows(error_rows, visible):
            values = {**row["raw_row"], **{key: row[key] for key in headers[:3]}}
            cells = [values.get(key, "") for key in headers]
            writer.writerow(["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                             else value for value in cells])
            count += 1
        return output.getvalue(), count
