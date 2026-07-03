import base64

import graphene
from django.core.exceptions import PermissionDenied

from household_validation.apps import HouseholdValidationConfig
from household_validation.gql_permissions import require_permissions
from household_validation.gql_mutations import (
    GenerateHouseholdValidationListMutation,
    UploadHouseholdValidationListMutation,
)
from household_validation.gql_queries import (
    HouseholdValidationBatchRowsGQLType,
    HouseholdValidationBatchesGQLType,
    HouseholdValidationErrorReportGQLType,
    HouseholdValidationProjectsGQLType,
)
from household_validation.models import (
    HouseholdValidationBatch,
    HouseholdValidationBatchRow,
)
from household_validation.services import (
    HouseholdValidationProjectLookupService,
    build_validation_error_report,
)


class Query(graphene.ObjectType):
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
    household_validation_batch_error_report = graphene.Field(
        HouseholdValidationErrorReportGQLType,
        batch_id=graphene.Argument(graphene.UUID, required=True),
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
        queryset = HouseholdValidationBatch.objects.filter(is_deleted=False)
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
        )
        if kwargs.get("status"):
            queryset = queryset.filter(status=kwargs["status"])
        rows = list(queryset.order_by("row_number"))
        return HouseholdValidationBatchRowsGQLType(
            rows=rows,
            count=len(rows),
        )

    def resolve_household_validation_batch_error_report(parent, info, **kwargs):
        Query._check_permissions(
            info.context.user,
            HouseholdValidationConfig.gql_query_household_validation_error_report_perms,
        )
        batch = HouseholdValidationBatch.objects.get(
            id=kwargs["batch_id"],
            is_deleted=False,
        )
        error_rows = batch.rows.filter(
            status=HouseholdValidationBatchRow.Status.ERROR,
            is_deleted=False,
        )
        report = build_validation_error_report(batch)
        return HouseholdValidationErrorReportGQLType(
            batch_id=batch.id,
            file_name=f"household_validation_errors_{batch.id}.csv",
            file_base64=base64.b64encode(report.encode("utf-8")).decode("ascii"),
            error_count=error_rows.count(),
        )

    @staticmethod
    def _check_permissions(user, perms):
        require_permissions(user, perms, error_class=PermissionDenied)


class Mutation(graphene.ObjectType):
    generate_household_validation_list = GenerateHouseholdValidationListMutation.Field()
    upload_household_validation_list = UploadHouseholdValidationListMutation.Field()
