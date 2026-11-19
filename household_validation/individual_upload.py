"""Apply saved-list decisions with per-row checks and existing batch/row history."""
import csv
import math
from io import StringIO
from types import SimpleNamespace
from uuid import UUID, uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from individual.data_source import DEFAULT_DATA_SOURCE, data_source_q, record_data_source
from individual.models import GroupIndividual, Individual

from household_validation.apps import HouseholdValidationConfig
from household_validation.gql_permissions import require_permissions
from household_validation.individual_lists import _json_snapshot
from household_validation.individual_selection import EligibleIndividualSelectionService
from household_validation.individual_snapshots import is_individual_list, selected_individual_ids
from household_validation.individual_workbook import (
    IndividualValidationListExporter, ParsedIndividualValidationRow, load_individual_workbook,
    parse_individual_validation_workbook, workbook_batch_id,
)
from household_validation.models import HouseholdValidationBatch, HouseholdValidationBatchRow
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

    def _context(self, batch_id, permissions):
        require_permissions(self.user, permissions, error_class=PermissionDenied)
        try:
            batch = HouseholdValidationBatch.objects.get(pk=batch_id, is_deleted=False)
        except (HouseholdValidationBatch.DoesNotExist, ValueError, ValidationError) as exc:
            raise ValidationError("Unknown individual validation batch.") from exc
        if not is_individual_list(batch):
            raise ValidationError("Batch is not a generated individual list.")
        selected = {str(pk) for pk in selected_individual_ids(batch)}
        existing = {str(pk) for pk in Individual.objects.filter(pk__in=selected).values_list("pk", flat=True)}
        scoped = Individual.get_queryset(Individual.objects.filter(pk__in=selected, is_deleted=False), self.user)
        visible = {str(person.pk): person for person in scoped.distinct()}
        owner = batch.user_created_id == self.user.id
        if not visible and not owner and not getattr(self.user, "is_imis_admin", False):
            raise PermissionDenied("You cannot access this individual validation batch.")
        return batch, selected, existing, visible, scoped

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
                )
                parsed = parse_individual_validation_workbook(workbook, batch)
                geography = {key: value for key, value in batch.json_ext["filters"].items()
                             if key not in ("exclude_validated", "target_count")}
                source = batch.json_ext["rule"].get("data_source", DEFAULT_DATA_SOURCE)
                active = GroupIndividual.objects.filter(
                    individual_id__in=selected, is_deleted=False, group__is_deleted=False,
                    individual__is_deleted=False,
                ).values("individual_id")
                eligible = scoped.filter(data_source_q(source)).exclude(pk__in=active)
                eligible = EligibleIndividualSelectionService(self.user)._apply_location_filters(eligible, **geography)
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
                            errors.append("Individual no longer meets the saved source, household or geographic scope.")
                    updates = {}
                    if not errors and row.decision is not None:
                        updates = {**row.updates, "validation_status": row.decision}
                        if row.notes is not None:
                            updates["validation_notes"] = row.notes
                        prospective = dict(person.json_ext) if isinstance(person.json_ext, dict) else {}
                        prospective.update(updates)
                        if record_data_source(SimpleNamespace(json_ext=prospective)) != source:
                            errors.append("Configured fields cannot move the individual to another source.")
                    current = person.json_ext if person is not None and isinstance(person.json_ext, dict) else {}
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
                for row, person, status, errors, updates, changed in prepared:
                    if status == HouseholdValidationBatchRow.Status.APPLIED and changed:
                        extra = dict(person.json_ext) if isinstance(person.json_ext, dict) else {}
                        extra.update(updates)
                        person.json_ext = extra
                        person.save(user=self.user)
                        totals["individuals_updated"] += 1
                    record = HouseholdValidationBatchRow(
                        batch=batch, upload_attempt_id=attempt, row_number=row.row_number,
                        individual_id=row.individual_id if row.individual_id in selected & existing else None,
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
                    **totals, "uploaded_at": uploaded_at, "actor_id": str(self.user.id),
                    "source_file_name": source_file_name,
                })
                data["upload_attempts"] = attempts
                batch.json_ext = data
                batch.status = totals["status"]
                batch.uploaded_at = uploaded_at
                batch.source_file_name = source_file_name
                batch.error_summary = "\n".join(totals["error_messages"]) or None
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
        rows = []
        for row in batch.rows.filter(upload_attempt_id=attempt_id, is_deleted=False).order_by("row_number"):
            allowed = str(row.individual_id) in visible
            rows.append(_json_snapshot({
                "id": row.pk, "row_number": row.row_number, "status": row.status,
                "individual_id": row.individual_id if allowed else None,
                "raw_row": row.raw_row if allowed else {}, "json_ext": row.json_ext if allowed else {},
                "error_message": row.error_message if allowed or not row.raw_row
                else "Row data is not currently accessible.", "redacted": not allowed,
            }))
        return batch, rows

    def attempt_rows(self, batch_id, attempt_id):
        return self._attempt_rows(
            batch_id, attempt_id, HouseholdValidationConfig.gql_query_household_validation_history_perms,
        )[1]

    def error_report(self, batch_id, attempt_id):
        batch, rows = self._attempt_rows(
            batch_id, attempt_id, HouseholdValidationConfig.gql_query_household_validation_error_report_perms,
        )
        headers = ["row_number", "status", "error_message", *IndividualValidationListExporter(batch).columns]
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        count = 0
        for row in rows:
            if row["status"] != HouseholdValidationBatchRow.Status.ERROR:
                continue
            values = {**row["raw_row"], **{key: row[key] for key in headers[:3]}}
            cells = [values.get(key, "") for key in headers]
            writer.writerow(["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                             else value for value in cells])
            count += 1
        return output.getvalue(), count
