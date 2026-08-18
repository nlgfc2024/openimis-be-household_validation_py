import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from individual.models import Group, GroupIndividual
from individual.services import GroupIndividualService
from location.models import Hotspot, MicroCatchment
from project_social_protection.models import Project

from household_validation.excel import LOCATION_COLUMN_TYPES
from household_validation.models import (
    HouseholdValidationBatch,
    HouseholdValidationBatchRow,
)
from household_validation.project_lookup import (
    ACTIVE_PROJECT_STATUSES,
    project_option_from_project,
)
from household_validation.selection import (
    EligibleHousehold,
    EligibleMember,
    is_truthy,
    select_households,
)
from household_validation.upload import (
    build_validation_error_report_csv,
    build_validation_json_ext,
    member_structural_errors,
    parse_validation_workbook,
)
from household_validation.wealth import get_household_wealth_quintile


def _local_date(value):
    """Local date of ``value``, tolerating naive datetimes.

    openIMIS runs with ``USE_TZ = False``, so ``timezone.now()`` is naive and
    ``timezone.localdate()``/``timezone.localtime()`` raise ValueError. Mirrors
    the guard used in ``core.services.userServices``.
    """
    return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()


def _json_safe(value):
    """Return a JSON-native copy suitable for storage in a JSONField."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


class HouseholdValidationUploadService:
    def __init__(self, user=None):
        self.user = user
        self._group_cache = {}

    def upload(self, file_or_bytes, dry_run=False, source_file_name=None):
        self._group_cache = {}
        parsed = parse_validation_workbook(file_or_bytes)
        totals = {
            "rows_read": parsed.rows_read,
            "households_verified": 0,
            "households_not_verified": 0,
            "participant_updates": 0,
            "errors": len(parsed.errors),
            "error_messages": list(parsed.errors),
        }
        if dry_run:
            return totals

        batch = self._get_or_create_batch(parsed, source_file_name=source_file_name)
        uploaded_at = timezone.now()
        upload_date = _local_date(uploaded_at)

        with transaction.atomic():
            for uploaded_row in parsed.rows:
                row_errors = self._apply_row(
                    uploaded_row,
                    batch=batch,
                    upload_date=upload_date,
                    uploaded_at=uploaded_at,
                )
                if row_errors:
                    totals["errors"] += len(row_errors)
                    totals["error_messages"].extend(row_errors)
                    continue
                if uploaded_row.verified is True:
                    totals["households_verified"] += 1
                elif uploaded_row.verified is False:
                    totals["households_not_verified"] += 1
                if uploaded_row.primary_worker is not None:
                    totals["participant_updates"] += 1

            batch.uploaded_at = uploaded_at
            batch.status = self._batch_status(totals)
            batch.error_summary = "\n".join(totals["error_messages"]) or None
            batch.save(user=self.user)
        return totals

    def _get_or_create_batch(self, parsed, source_file_name=None):
        batch_id = self._batch_id(parsed)
        if batch_id:
            batch = HouseholdValidationBatch.objects.filter(id=batch_id).first()
            if batch:
                return batch
        batch = HouseholdValidationBatch(
            source_file_name=source_file_name,
            status=HouseholdValidationBatch.Status.PENDING,
        )
        if batch_id:
            batch.id = batch_id
        batch.save(user=self.user)
        return batch

    def _batch_id(self, parsed):
        batch_ids = {
            row.values.get("batch_id")
            for row in parsed.rows
            if row.values.get("batch_id")
        }
        if len(batch_ids) != 1:
            return None
        batch_id = next(iter(batch_ids))
        try:
            return UUID(str(batch_id))
        except ValueError:
            return None

    def _apply_row(self, uploaded_row, batch, upload_date, uploaded_at):
        errors = []
        group = self._group(uploaded_row.values["group_uuid"])
        group_individual = self._group_individual(
            uploaded_row.values["member_uuid"],
            group=group,
        )
        project = self._project(uploaded_row.project_id)

        if group is None:
            errors.append(f"Row {uploaded_row.row_number}: group was not found")
        else:
            errors.extend(
                self._structural_errors(
                    uploaded_row,
                    group,
                    group_individual=group_individual,
                )
            )
        if group_individual is None:
            errors.append(f"Row {uploaded_row.row_number}: member was not found in group")
        if uploaded_row.project_id and project is None:
            errors.append(f"Row {uploaded_row.row_number}: project was not found")

        if errors:
            self._save_batch_row(
                batch=batch,
                uploaded_row=uploaded_row,
                group=group,
                group_individual=group_individual,
                project=project,
                status=HouseholdValidationBatchRow.Status.ERROR,
                error_message="\n".join(errors),
            )
            return errors

        if uploaded_row.verified is not None:
            self._apply_group_validation(
                group=group,
                uploaded_row=uploaded_row,
                project=project,
                upload_date=upload_date,
                uploaded_at=uploaded_at,
            )
        if uploaded_row.primary_worker is not None:
            self._apply_primary_worker(
                group_individual,
                uploaded_row.primary_worker,
            )

        self._save_batch_row(
            batch=batch,
            uploaded_row=uploaded_row,
            group=group,
            group_individual=group_individual,
            project=project,
            status=(
                HouseholdValidationBatchRow.Status.APPLIED
                if (
                    uploaded_row.verified is not None
                    or uploaded_row.primary_worker is not None
                )
                else HouseholdValidationBatchRow.Status.SKIPPED
            ),
        )
        return []

    def _apply_group_validation(self, group, uploaded_row, project, upload_date, uploaded_at):
        json_ext = group.json_ext or {}
        json_ext.update(
            build_validation_json_ext(
                uploaded_row=uploaded_row,
                project=project,
                upload_date=upload_date,
                uploaded_at=uploaded_at,
                user_id=getattr(self.user, "id", None),
            )
        )
        group.json_ext = json_ext
        group.save(user=self.user)

    def _apply_primary_worker(self, group_individual, primary_worker):
        json_ext = dict(group_individual.json_ext or {})
        json_ext["primary_worker"] = primary_worker
        GroupIndividualService(self.user).update(
            {
                "id": group_individual.id,
                "group_id": group_individual.group_id,
                "json_ext": json_ext,
            }
        )

    def _save_batch_row(
        self,
        batch,
        uploaded_row,
        group=None,
        group_individual=None,
        project=None,
        status=HouseholdValidationBatchRow.Status.PENDING,
        error_message=None,
    ):
        batch_row = HouseholdValidationBatchRow(
            batch=batch,
            group=group,
            group_individual=group_individual,
            individual=getattr(group_individual, "individual", None),
            project=project,
            row_number=uploaded_row.row_number,
            verified=uploaded_row.verified,
            validation_date=uploaded_row.validation_date,
            status=status,
            error_message=error_message,
            raw_row=_json_safe(uploaded_row.values),
            json_ext={
                "primary_worker": uploaded_row.primary_worker,
                "project_name": uploaded_row.project_name,
                "validation_notes": uploaded_row.notes,
            },
        )
        batch_row.save(user=self.user)

    def _project(self, project_id):
        if not project_id:
            return None
        try:
            return Project.objects.filter(id=project_id).first()
        except (ValueError, ValidationError):
            return None

    def _group(self, group_id):
        cache_key = str(group_id)
        if cache_key in self._group_cache:
            return self._group_cache[cache_key]
        try:
            group = (
                Group.objects.filter(id=group_id)
                .select_related("location")
                .prefetch_related("groupindividuals__individual")
                .first()
            )
        except (ValueError, ValidationError):
            group = None
        self._group_cache[cache_key] = group
        return group

    def _group_individual(self, individual_id, group):
        prefetched_members = getattr(
            group,
            "_prefetched_objects_cache",
            {},
        ).get("groupindividuals")
        if prefetched_members is not None:
            for group_individual in prefetched_members:
                if (
                    str(group_individual.individual_id) == str(individual_id)
                    and not group_individual.is_deleted
                ):
                    return group_individual
            return None
        try:
            return (
                GroupIndividual.objects.filter(
                    individual_id=individual_id,
                    group=group,
                    is_deleted=False,
                )
                .select_related("individual")
                .first()
            )
        except (ValueError, ValidationError):
            return None

    def _structural_errors(self, uploaded_row, group, group_individual=None):
        errors = []
        row_number = uploaded_row.row_number

        if uploaded_row.values.get("row_type") not in ("MAIN", "RESERVE"):
            errors.append(f"Row {row_number}: row_type is invalid")
        location = getattr(group, "location", None)
        for column, location_type in LOCATION_COLUMN_TYPES.items():
            uploaded_value = self._normalize(uploaded_row.values.get(column))
            database_value = self._normalize(self._location_name(location, location_type))
            if uploaded_value and database_value and uploaded_value != database_value:
                errors.append(f"Row {row_number}: {column} does not match the household")
        if group_individual is not None:
            errors.extend(
                member_structural_errors(
                    uploaded_row,
                    group_individual=group_individual,
                    group=group,
                )
            )
        return errors

    def _location_name(self, location, location_type):
        current = location
        while current is not None:
            if getattr(current, "type", None) == location_type:
                return getattr(current, "name", None) or getattr(current, "code", None)
            current = getattr(current, "parent", None)
        return None

    def _normalize(self, value):
        if value is None:
            return None
        if isinstance(value, date):
            value = value.isoformat()
        value = str(value).strip().casefold()
        return value or None

    def _batch_status(self, totals):
        if totals["errors"] and totals["errors"] >= totals["rows_read"]:
            return HouseholdValidationBatch.Status.FAILED
        if totals["errors"]:
            return HouseholdValidationBatch.Status.PARTIAL_SUCCESS
        return HouseholdValidationBatch.Status.PROCESSED


def build_validation_error_report(batch):
    rows = batch.rows.filter(
        status=HouseholdValidationBatchRow.Status.ERROR,
        is_deleted=False,
    ).order_by("row_number")
    return build_validation_error_report_csv(batch.id, rows)


class HouseholdValidationProjectLookupService:
    def list_projects(
        self,
        location_id=None,
        location_code=None,
        hotspot_id=None,
        hotspot_code=None,
        catchment_id=None,
    ):
        queryset = Project.objects.select_related("location").filter(
            status__in=ACTIVE_PROJECT_STATUSES,
        )
        queryset = self._apply_location_filter(
            queryset,
            location_id=location_id,
            location_code=location_code,
        )
        # Hotspot and public works catchment models do not exist yet. Keep the
        # nullable filter signature stable and add relation filters when they do.
        return [
            project_option_from_project(project)
            for project in queryset.order_by("name", "id")
        ]

    def _apply_location_filter(self, queryset, location_id=None, location_code=None):
        location_filter = Q()
        if location_id:
            location_filter |= (
                Q(location_id=location_id)
                | Q(location__parent_id=location_id)
                | Q(location__parent__parent_id=location_id)
                | Q(location__children__id=location_id)
                | Q(location__children__children__id=location_id)
            )
        if location_code:
            location_filter |= (
                Q(location__code=location_code)
                | Q(location__parent__code=location_code)
                | Q(location__parent__parent__code=location_code)
                | Q(location__children__code=location_code)
                | Q(location__children__children__code=location_code)
            )
        if not location_filter:
            return queryset
        return queryset.filter(location_filter).distinct()


@dataclass(frozen=True)
class HouseholdValidationPreviewRow:
    row_type: str
    category: str
    group_uuid: str
    group_code: str | None
    head_name: str | None
    individual_uuid: str
    individual_first_name: str | None
    individual_last_name: str | None
    individual_dob: date | None
    individual_age: int | None
    individual_gender: str | None
    fit_for_work: bool
    current_recipient_type: str | None
    region: str | None
    district: str | None
    municipality: str | None
    village: str | None
    wealth_quintile: str | int | None
    last_verified_date: date | None
    validation_status: str | None
    prospective_projects: list[str]


class EligibleHouseholdSelectionService:
    def __init__(self, user=None):
        self.user = user
        self._project_name_cache = {}

    def select(
        self,
        region_id=None,
        region_code=None,
        district_id=None,
        district_code=None,
        ta_id=None,
        ta_code=None,
        ta_codes=None,
        gvh_codes=None,
        village_id=None,
        village_code=None,
        village_codes=None,
        hotspot_id=None,
        hotspot_code=None,
        catchment_id=None,
        catchment_code=None,
        exclude_verified_after=None,
        target_count=None,
    ):
        candidates = self.candidates(
            region_id=region_id,
            region_code=region_code,
            district_id=district_id,
            district_code=district_code,
            ta_id=ta_id,
            ta_code=ta_code,
            ta_codes=ta_codes,
            gvh_codes=gvh_codes,
            village_id=village_id,
            village_code=village_code,
            village_codes=village_codes,
            hotspot_id=hotspot_id,
            hotspot_code=hotspot_code,
            catchment_id=catchment_id,
            catchment_code=catchment_code,
        )
        selection_result, _ = select_households(
            candidates,
            target_count=target_count,
            exclude_verified_after=exclude_verified_after,
        )
        return selection_result

    def candidates(
        self,
        region_id=None,
        region_code=None,
        district_id=None,
        district_code=None,
        ta_id=None,
        ta_code=None,
        ta_codes=None,
        gvh_codes=None,
        village_id=None,
        village_code=None,
        village_codes=None,
        hotspot_id=None,
        hotspot_code=None,
        catchment_id=None,
        catchment_code=None,
    ):
        queryset = self._base_queryset()
        queryset = self._apply_location_filters(
            queryset,
            region_id=region_id,
            region_code=region_code,
            district_id=district_id,
            district_code=district_code,
            ta_id=ta_id,
            ta_code=ta_code,
            ta_codes=ta_codes,
            gvh_codes=gvh_codes,
            village_id=village_id,
            village_code=village_code,
            village_codes=village_codes,
            hotspot_id=hotspot_id,
            hotspot_code=hotspot_code,
            catchment_id=catchment_id,
            catchment_code=catchment_code,
        )
        return [
            household
            for household in (self._build_household(group) for group in queryset)
            if household is not None
        ]

    def generate(self, **filters):
        """
        The ``generateHouseholdValidationList`` mutation exposes both: the
        ``SelectionResult`` to export the workbook, and the summary counts inline
        on the same response, so callers get a single request/response.
        """
        queryset = self._base_queryset()
        queryset = self._apply_location_filters(
            queryset,
            region_id=filters.get("region_id"),
            region_code=filters.get("region_code"),
            district_id=filters.get("district_id"),
            district_code=filters.get("district_code"),
            ta_id=filters.get("ta_id"),
            ta_code=filters.get("ta_code"),
            ta_codes=filters.get("ta_codes"),
            gvh_codes=filters.get("gvh_codes"),
            village_id=filters.get("village_id"),
            village_code=filters.get("village_code"),
            village_codes=filters.get("village_codes"),
            hotspot_id=filters.get("hotspot_id"),
            hotspot_code=filters.get("hotspot_code"),
            catchment_id=filters.get("catchment_id"),
            catchment_code=filters.get("catchment_code"),
        )
        groups = list(queryset)
        total_households = len(groups)
        total_individuals = 0
        eligible_individuals = 0
        candidates = []
        for group in groups:
            total_individuals += len(
                [member for member in group.groupindividuals.all() if not member.is_deleted]
            )
            household = self._build_household(group)
            if household is None:
                continue
            candidates.append(household)
            eligible_individuals += len(household.eligible_members)

        selection_result, selection_summary = select_households(
            candidates,
            target_count=filters.get("target_count"),
            exclude_verified_after=filters.get("exclude_verified_after"),
        )
        summary = {
            "total_households": total_households,
            "total_individuals": total_individuals,
            "eligible_households": len(candidates),
            "eligible_individuals": eligible_individuals,
            **selection_summary,
            "generated_at": timezone.now(),
        }
        return selection_result, summary

    def preview(self, **filters):
        selection_result = self.select(**filters)
        return [
            self._preview_row(selected_member)
            for selected_member in selection_result.member_rows
        ]

    def _base_queryset(self):
        queryset = Group.objects.select_related("location").prefetch_related(
            "groupindividuals__individual",
        )
        if self.user is not None:
            queryset = Group.get_queryset(queryset, self.user)
        return queryset

    def _apply_location_filters(
        self,
        queryset,
        region_id=None,
        region_code=None,
        district_id=None,
        district_code=None,
        ta_id=None,
        ta_code=None,
        ta_codes=None,
        gvh_codes=None,
        village_id=None,
        village_code=None,
        village_codes=None,
        hotspot_id=None,
        hotspot_code=None,
        catchment_id=None,
        catchment_code=None,
    ):
        # Accept both the legacy singular ``village_code`` and the plural
        # ``village_codes`` list; merge them into a single code set.
        codes = list(village_codes or [])
        if village_code:
            codes.append(village_code)

        if village_id or codes:
            village_filter = Q()
            if village_id:
                village_filter |= Q(location_id=village_id)
            if codes:
                village_filter |= Q(location__code__in=codes)
            return queryset.filter(village_filter)

        selected_gvh_codes = list(dict.fromkeys(gvh_codes or []))
        if selected_gvh_codes:
            return queryset.filter(
                Q(location__code__in=selected_gvh_codes)
                | Q(location__parent__code__in=selected_gvh_codes)
            )

        # A hotspot is a curated set of specific villages, finer-grained than a
        # plain TA/GVH pick, so it's checked before those but still yields to an
        # explicit village/GVH selection made on top of it in the UI.
        if hotspot_id or hotspot_code:
            hotspot = self._resolve_hotspot(hotspot_id, hotspot_code)
            if hotspot is None:
                return queryset.none()
            hotspot_village_codes = list(hotspot.villages.values_list("code", flat=True))
            if not hotspot_village_codes:
                return queryset.none()
            return queryset.filter(location__code__in=hotspot_village_codes)

        selected_ta_codes = list(dict.fromkeys(ta_codes or []))
        if ta_code:
            selected_ta_codes.append(ta_code)

        if ta_id or selected_ta_codes:
            ta_filter = Q()
            if ta_id:
                ta_filter |= (
                    Q(location_id=ta_id)
                    | Q(location__parent_id=ta_id)
                    | Q(location__parent__parent_id=ta_id)
                )
            if selected_ta_codes:
                ta_filter |= (
                    Q(location__code__in=selected_ta_codes)
                    | Q(location__parent__code__in=selected_ta_codes)
                    | Q(location__parent__parent__code__in=selected_ta_codes)
                )
            return queryset.filter(ta_filter)

        # A micro-catchment spans a set of TAs and/or GVHs within a district.
        if catchment_id or catchment_code:
            micro_catchment = self._resolve_micro_catchment(catchment_id, catchment_code)
            if micro_catchment is None:
                return queryset.none()
            catchment_ta_codes = list(
                micro_catchment.traditional_authorities.filter(
                    validity_to__isnull=True,
                ).values_list("location__code", flat=True)
            )
            catchment_gvh_codes = list(
                micro_catchment.gvhs.filter(
                    validity_to__isnull=True,
                ).values_list("location__code", flat=True)
            )
            catchment_filter = Q()
            if catchment_ta_codes:
                catchment_filter |= (
                    Q(location__code__in=catchment_ta_codes)
                    | Q(location__parent__code__in=catchment_ta_codes)
                    | Q(location__parent__parent__code__in=catchment_ta_codes)
                )
            if catchment_gvh_codes:
                catchment_filter |= (
                    Q(location__code__in=catchment_gvh_codes)
                    | Q(location__parent__code__in=catchment_gvh_codes)
                )
            if not catchment_filter:
                return queryset.none()
            return queryset.filter(catchment_filter)

        if district_id or district_code:
            district_filter = Q()
            if district_id:
                district_filter |= (
                    Q(location_id=district_id)
                    | Q(location__parent_id=district_id)
                    | Q(location__parent__parent_id=district_id)
                    | Q(location__parent__parent__parent_id=district_id)
                )
            if district_code:
                district_filter |= (
                    Q(location__code=district_code)
                    | Q(location__parent__code=district_code)
                    | Q(location__parent__parent__code=district_code)
                    | Q(location__parent__parent__parent__code=district_code)
                )
            return queryset.filter(district_filter)
        if region_id or region_code:
            region_filter = Q()
            if region_id:
                region_filter |= (
                    Q(location_id=region_id)
                    | Q(location__parent_id=region_id)
                    | Q(location__parent__parent_id=region_id)
                    | Q(location__parent__parent__parent_id=region_id)
                )
            if region_code:
                region_filter |= (
                    Q(location__code=region_code)
                    | Q(location__parent__code=region_code)
                    | Q(location__parent__parent__code=region_code)
                    | Q(location__parent__parent__parent__code=region_code)
                )
            return queryset.filter(region_filter)
        return queryset

    def _resolve_hotspot(self, hotspot_id, hotspot_code):
        identity_filter = Q()
        if hotspot_id:
            identity_filter |= Q(uuid=hotspot_id)
        if hotspot_code:
            identity_filter |= Q(code=hotspot_code)
        if not identity_filter:
            return None
        return Hotspot.objects.filter(identity_filter, validity_to__isnull=True).first()

    def _resolve_micro_catchment(self, catchment_id, catchment_code):
        identity_filter = Q()
        if catchment_id:
            identity_filter |= Q(uuid=catchment_id)
        if catchment_code:
            identity_filter |= Q(code=catchment_code)
        if not identity_filter:
            return None
        return MicroCatchment.objects.filter(identity_filter, validity_to__isnull=True).first()

    def _build_household(self, group):
        groupindividuals = list(group.groupindividuals.all())
        eligible_members = []
        for group_individual in groupindividuals:
            member = self._build_member(group_individual)
            if member and member.fit_for_work:
                eligible_members.append(member)
        if not eligible_members:
            return None

        head = self._find_head(group, groupindividuals)
        wealth_quintile = get_household_wealth_quintile(
            group,
            group_individuals=groupindividuals,
        )

        group_json_ext = group.json_ext or {}

        return EligibleHousehold(
            id=group.id,
            code=group.code,
            wealth_quintile=wealth_quintile,
            last_verified_date=self._parse_date(group_json_ext.get("last_verified_date")),
            head=head,
            eligible_members=eligible_members,
            source=group,
        )

    def _find_head(self, group, groupindividuals):
        head_id = (group.json_ext or {}).get("head_id")
        for group_individual in groupindividuals:
            if group_individual.role == GroupIndividual.Role.HEAD:
                return self._build_member(group_individual)
        if head_id:
            for group_individual in groupindividuals:
                if str(group_individual.individual_id) == str(head_id):
                    return self._build_member(group_individual)
        return None

    def _build_member(self, group_individual):
        individual = group_individual.individual
        if individual is None:
            return None
        json_ext = individual.json_ext or {}
        return EligibleMember(
            id=individual.id,
            gender=json_ext.get("gender"),
            dob=individual.dob,
            fit_for_work=self._is_fit_for_work(group_individual),
            role=group_individual.role,
            recipient_type=group_individual.recipient_type,
            source=group_individual,
        )

    def _is_fit_for_work(self, group_individual):
        individual = group_individual.individual
        if individual is None:
            return False
        return is_truthy((individual.json_ext or {}).get("fit_for_work"))

    def _parse_date(self, value):
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None

    def _preview_row(self, selected_member):
        household = selected_member.household
        member = selected_member.member
        group = household.source
        group_individual = member.source
        individual = getattr(group_individual, "individual", None)
        location = getattr(group, "location", None)
        group_json_ext = getattr(group, "json_ext", None) or {}
        return HouseholdValidationPreviewRow(
            row_type=selected_member.row_type,
            category=selected_member.category,
            group_uuid=str(household.id),
            group_code=household.code,
            head_name=self._member_name(getattr(household.head, "source", None)),
            individual_uuid=str(member.id),
            individual_first_name=getattr(individual, "first_name", None),
            individual_last_name=getattr(individual, "last_name", None),
            individual_dob=getattr(individual, "dob", None),
            individual_age=member.age,
            individual_gender=member.gender,
            fit_for_work=member.fit_for_work,
            current_recipient_type=member.recipient_type,
            region=self._location_name(location, "R"),
            district=self._location_name(location, "D"),
            municipality=self._location_name(location, "W"),
            village=self._location_name(location, "V"),
            wealth_quintile=household.wealth_quintile,
            last_verified_date=household.last_verified_date,
            validation_status=group_json_ext.get("validation_status"),
            prospective_projects=self._project_names(location),
        )

    def _member_name(self, group_individual):
        individual = getattr(group_individual, "individual", None)
        if not individual:
            return None
        first_name = getattr(individual, "first_name", "") or ""
        last_name = getattr(individual, "last_name", "") or ""
        return f"{first_name} {last_name}".strip() or None

    def _location_name(self, location, location_type):
        current = location
        while current is not None:
            if getattr(current, "type", None) == location_type:
                return getattr(current, "name", None) or getattr(current, "code", None)
            current = getattr(current, "parent", None)
        return None

    def _project_names(self, location):
        location_id = getattr(location, "id", None)
        if not location_id:
            return []
        if location_id in self._project_name_cache:
            return self._project_name_cache[location_id]
        project_names = [
            project.name
            for project in HouseholdValidationProjectLookupService().list_projects(
                location_id=location_id,
            )
            if project.name
        ]
        self._project_name_cache[location_id] = project_names
        return project_names
