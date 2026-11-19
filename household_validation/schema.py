import base64

import graphene
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q

from household_validation.apps import HouseholdValidationConfig
from household_validation.gql_permissions import require_permissions
from household_validation.excel import (
    build_rejected_households_workbook_bytes,
    is_primary_worker_rejection,
)
from household_validation.gql_mutations import (
    UploadIndividualValidationListMutation,
    GenerateIndividualValidationListMutation,
    IndividualValidationGenerateResultGQLType,
    individual_workbook_result,
    GenerateHouseholdValidationListMutation,
    UploadHouseholdValidationListMutation,
)
from household_validation.gql_queries import (
    IndividualValidationAttemptsGQLType,
    IndividualValidationAttemptRowsGQLType,
    IndividualValidationErrorReportGQLType,
    IndividualValidationSavedListGQLType,
    ValidationProgrammeGQLType,
    IndividualValidationCandidatesGQLType,
    HouseholdValidationBatchRowsGQLType,
    HouseholdValidationBatchesGQLType,
    HouseholdValidationErrorReportGQLType,
    HouseholdValidationPreviewEdgeGQLType,
    HouseholdValidationPreviewGQLType,
    HouseholdValidationPreviewPageInfoGQLType,
    HouseholdValidationProjectsGQLType,
)
from household_validation.individual_selection import EligibleIndividualSelectionService
from household_validation.individual_lists import IndividualValidationListService
from household_validation.individual_upload import IndividualValidationUploadService
from household_validation.programmes import configured_programmes
from household_validation.models import (
    HouseholdValidationBatch,
    HouseholdValidationBatchRow,
)
from household_validation.services import (
    EligibleHouseholdSelectionService,
    HouseholdValidationProjectLookupService,
    build_validation_error_report,
)
from household_validation.verification import NOT_VERIFIED

VALIDATION_FILTER_ARG_NAMES = {
    "region_id",
    "region_code",
    "district_id",
    "district_code",
    "ta_id",
    "ta_code",
    "ta_codes",
    "gvh_codes",
    "village_id",
    "village_code",
    "village_codes",
    "hotspot_id",
    "hotspot_code",
    "catchment_id",
    "catchment_code",
    "exclude_verified_after",
    "target_count",
    "benefit_plan_code",
}


def household_batch_scope(prefix=""):
    """Legacy history must not expose individual snapshots without their access checks."""
    field = f"{prefix}json_ext__subject"
    return Q(**{f"{field}__isnull": True}) | ~Q(**{field: "INDIVIDUAL"})


def validation_filter_args():
    return {
        "region_id": graphene.Argument(graphene.Int, required=False),
        "region_code": graphene.Argument(graphene.String, required=False),
        "district_id": graphene.Argument(graphene.Int, required=False),
        "district_code": graphene.Argument(graphene.String, required=False),
        "ta_id": graphene.Argument(graphene.Int, required=False),
        "ta_code": graphene.Argument(graphene.String, required=False),
        "ta_codes": graphene.Argument(graphene.List(graphene.String), required=False),
        "gvh_codes": graphene.Argument(graphene.List(graphene.String), required=False),
        "village_id": graphene.Argument(graphene.Int, required=False),
        "village_code": graphene.Argument(graphene.String, required=False),
        "village_codes": graphene.Argument(graphene.List(graphene.String), required=False),
        "hotspot_id": graphene.Argument(graphene.String, required=False),
        "hotspot_code": graphene.Argument(graphene.String, required=False),
        "catchment_id": graphene.Argument(graphene.String, required=False),
        "catchment_code": graphene.Argument(graphene.String, required=False),
        "exclude_verified_after": graphene.Argument(graphene.Date, required=False),
        "target_count": graphene.Argument(graphene.Int, required=False),
        "benefit_plan_code": graphene.Argument(graphene.String, required=False),
    }


class Query(graphene.ObjectType):
    individual_validation_attempts = graphene.Field(
        IndividualValidationAttemptsGQLType, batch_id=graphene.UUID(required=True),
    )
    individual_validation_attempt_rows = graphene.Field(
        IndividualValidationAttemptRowsGQLType, batch_id=graphene.UUID(required=True),
        upload_attempt_id=graphene.UUID(required=True),
    )
    individual_validation_error_report = graphene.Field(
        IndividualValidationErrorReportGQLType, batch_id=graphene.UUID(required=True),
        upload_attempt_id=graphene.UUID(required=True),
    )
    individual_validation_list = graphene.Field(
        IndividualValidationSavedListGQLType, batch_id=graphene.UUID(required=True),
        first=graphene.Int(default_value=20), offset=graphene.Int(default_value=0),
    )
    individual_validation_list_export = graphene.Field(
        IndividualValidationGenerateResultGQLType, batch_id=graphene.UUID(required=True),
    )
    validation_programmes = graphene.List(ValidationProgrammeGQLType)
    individual_validation_candidates = graphene.Field(
        IndividualValidationCandidatesGQLType,
        exclude_validated=graphene.Argument(graphene.Boolean, default_value=False),
        first=graphene.Argument(graphene.Int, default_value=20),
        offset=graphene.Argument(graphene.Int, default_value=0),
        **{
            key: (graphene.Argument(graphene.String, required=True) if key == "benefit_plan_code" else value)
            for key, value in validation_filter_args().items()
            if key != "exclude_verified_after"
        },
    )
    household_validation_preview = graphene.Field(
        HouseholdValidationPreviewGQLType,
        first=graphene.Argument(graphene.Int, required=False),
        offset=graphene.Argument(graphene.Int, required=False),
        order_by=graphene.Argument(graphene.String, required=False),
        **validation_filter_args(),
    )
    household_validation_projects = graphene.Field(
        HouseholdValidationProjectsGQLType,
        location_id=graphene.Argument(graphene.Int, required=False),
        location_code=graphene.Argument(graphene.String, required=False),
        hotspot_id=graphene.Argument(graphene.String, required=False),
        hotspot_code=graphene.Argument(graphene.String, required=False),
        catchment_id=graphene.Argument(graphene.String, required=False),
    )
    household_validation_batches = graphene.Field(
        HouseholdValidationBatchesGQLType,
        status=graphene.Argument(graphene.String, required=False),
    )
    household_validation_batch_rows = graphene.Field(
        HouseholdValidationBatchRowsGQLType,
        batch_id=graphene.Argument(graphene.UUID, required=True),
        status=graphene.Argument(graphene.String, required=False),
    )
    household_validation_rejected_batch_rows = graphene.Field(
        HouseholdValidationBatchRowsGQLType,
        batch_id=graphene.Argument(graphene.UUID, required=True),
        upload_attempt_id=graphene.Argument(graphene.UUID, required=True),
    )
    household_validation_batch_error_report = graphene.Field(
        HouseholdValidationErrorReportGQLType,
        batch_id=graphene.Argument(graphene.UUID, required=True),
    )

    def resolve_individual_validation_attempts(parent, info, batch_id):
        attempts = IndividualValidationUploadService(info.context.user).history(batch_id)
        return IndividualValidationAttemptsGQLType(attempts=attempts, count=len(attempts))

    def resolve_individual_validation_attempt_rows(parent, info, batch_id, upload_attempt_id):
        rows = IndividualValidationUploadService(info.context.user).attempt_rows(batch_id, upload_attempt_id)
        return IndividualValidationAttemptRowsGQLType(items=rows, count=len(rows))

    def resolve_individual_validation_error_report(parent, info, batch_id, upload_attempt_id):
        content, count = IndividualValidationUploadService(info.context.user).error_report(batch_id, upload_attempt_id)
        return IndividualValidationErrorReportGQLType(
            batch_id=batch_id, upload_attempt_id=upload_attempt_id, error_count=count,
            file_name=f"individual_validation_errors_{batch_id}_{upload_attempt_id}.csv",
            file_base64=base64.b64encode(content.encode("utf-8")).decode("ascii"),
        )

    def resolve_individual_validation_list(parent, info, batch_id, first=20, offset=0):
        return IndividualValidationListService(info.context.user).preview(batch_id, first, offset)

    def resolve_individual_validation_list_export(parent, info, batch_id):
        batch, content = IndividualValidationListService(info.context.user).export(batch_id)
        return individual_workbook_result(batch, content)

    def resolve_validation_programmes(parent, info):
        Query._check_permissions(
            info.context.user, HouseholdValidationConfig.gql_query_household_validation_rule_perms,
        )
        return configured_programmes()

    def resolve_individual_validation_candidates(parent, info, first=20, offset=0, **kwargs):
        Query._check_permissions(
            info.context.user, HouseholdValidationConfig.gql_query_household_validation_rule_perms,
        )
        if first is None or first < 0 or first > 100 or offset is None or offset < 0:
            raise ValidationError("first must be between 0 and 100 and offset must be non-negative.")
        result = EligibleIndividualSelectionService(info.context.user).select(**kwargs)
        return IndividualValidationCandidatesGQLType(
            eligible_individuals=result.eligible_individuals,
            selected_individuals=result.selected_individuals,
            items=list(result.individuals[offset:offset + first]),
            has_next_page=offset + first < result.selected_individuals,
        )

    def resolve_household_validation_preview(parent, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            HouseholdValidationConfig.gql_query_household_validation_rule_perms,
        )
        rows = EligibleHouseholdSelectionService(info.context.user).preview(
            **Query._selection_filters(kwargs)
        )
        total_count = len(rows)
        offset = max(0, kwargs.get("offset") or 0)
        first = kwargs.get("first")
        if first is None:
            first = total_count
        first = max(0, first)
        page_rows = rows[offset:offset + first]
        return HouseholdValidationPreviewGQLType(
            edges=[
                HouseholdValidationPreviewEdgeGQLType(node=row)
                for row in page_rows
            ],
            total_count=total_count,
            page_info=HouseholdValidationPreviewPageInfoGQLType(
                has_next_page=offset + first < total_count,
                has_previous_page=offset > 0,
                start_cursor=str(offset) if page_rows else None,
                end_cursor=str(offset + len(page_rows) - 1) if page_rows else None,
            ),
        )

    def resolve_household_validation_projects(parent, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            HouseholdValidationConfig.gql_query_household_validation_rule_perms,
        )
        projects = HouseholdValidationProjectLookupService().list_projects(
            location_id=kwargs.get("location_id"),
            location_code=kwargs.get("location_code"),
            hotspot_id=kwargs.get("hotspot_id"),
            hotspot_code=kwargs.get("hotspot_code"),
            catchment_id=kwargs.get("catchment_id"),
        )
        return HouseholdValidationProjectsGQLType(
            projects=projects,
            count=len(projects),
        )

    def resolve_household_validation_batches(parent, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            HouseholdValidationConfig.gql_query_household_validation_history_perms,
        )
        queryset = HouseholdValidationBatch.objects.filter(household_batch_scope(), is_deleted=False)
        if kwargs.get("status"):
            queryset = queryset.filter(status=kwargs["status"])
        batches = list(queryset.order_by("-date_created"))
        return HouseholdValidationBatchesGQLType(
            batches=batches,
            count=len(batches),
        )

    def resolve_household_validation_batch_rows(parent, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            HouseholdValidationConfig.gql_query_household_validation_history_perms,
        )
        queryset = HouseholdValidationBatchRow.objects.filter(
            batch_id=kwargs["batch_id"],
            is_deleted=False,
        ).filter(household_batch_scope("batch__"))
        if kwargs.get("status"):
            queryset = queryset.filter(status=kwargs["status"])
        rows = list(queryset.order_by("row_number"))
        return HouseholdValidationBatchRowsGQLType(
            rows=rows,
            count=len(rows),
        )

    def resolve_household_validation_rejected_batch_rows(parent, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            HouseholdValidationConfig.gql_mutation_upload_household_validation_list_perms,
        )
        rejected_scope = (
            Q(status__in=(HouseholdValidationBatchRow.Status.REJECTED, HouseholdValidationBatchRow.Status.ERROR))
            | Q(json_ext__household_status=NOT_VERIFIED)
            | Q(json_ext__participant_status=NOT_VERIFIED)
        )
        rows = list(
            HouseholdValidationBatchRow.objects.filter(
                batch_id=kwargs["batch_id"],
                upload_attempt_id=kwargs["upload_attempt_id"],
                is_deleted=False,
            ).filter(
                rejected_scope & household_batch_scope("batch__"),
            ).order_by("row_number")
        )
        report, _ = build_rejected_households_workbook_bytes(rows)
        return HouseholdValidationBatchRowsGQLType(
            rows=rows,
            count=len(rows),
            file_name=(
                f"rejected_households_{kwargs['batch_id']}_"
                f"{kwargs['upload_attempt_id']}.xlsx"
            ),
            file_base64=base64.b64encode(report).decode("ascii"),
        )

    def resolve_household_validation_batch_error_report(parent, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            HouseholdValidationConfig.gql_query_household_validation_error_report_perms,
        )
        batch = HouseholdValidationBatch.objects.get(
            household_batch_scope(),
            id=kwargs["batch_id"],
            is_deleted=False,
        )
        error_rows = [
            row
            for row in batch.rows.filter(
                status=HouseholdValidationBatchRow.Status.ERROR,
                is_deleted=False,
            )
            if not is_primary_worker_rejection(row)
        ]
        report = build_validation_error_report(batch)
        return HouseholdValidationErrorReportGQLType(
            batch_id=batch.id,
            file_name=f"household_validation_errors_{batch.id}.csv",
            file_base64=base64.b64encode(report.encode("utf-8")).decode("ascii"),
            error_count=len(error_rows),
        )

    @staticmethod
    def _check_permissions(user, perms):
        require_permissions(user, perms, error_class=PermissionDenied)

    @staticmethod
    def _selection_filters(kwargs):
        return {
            key: kwargs.get(key)
            for key in VALIDATION_FILTER_ARG_NAMES
        }


class Mutation(graphene.ObjectType):
    upload_individual_validation_list = UploadIndividualValidationListMutation.Field()
    generate_individual_validation_list = GenerateIndividualValidationListMutation.Field()
    generate_household_validation_list = GenerateHouseholdValidationListMutation.Field()
    upload_household_validation_list = UploadHouseholdValidationListMutation.Field()
