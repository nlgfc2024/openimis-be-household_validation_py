import graphene
from graphene.types.generic import GenericScalar


class HouseholdValidationProjectGQLType(graphene.ObjectType):
    id = graphene.String()
    name = graphene.String()
    status = graphene.String()
    location_id = graphene.String()


class HouseholdValidationBatchGQLType(graphene.ObjectType):
    id = graphene.UUID()
    source_file_name = graphene.String()
    status = graphene.String()
    district_id = graphene.Int()
    ta_id = graphene.Int()
    village_id = graphene.Int()
    hotspot_code = graphene.String()
    catchment_code = graphene.String()
    exclude_verified_after = graphene.Date()
    target_count = graphene.Int()
    generated_at = graphene.DateTime()
    uploaded_at = graphene.DateTime()
    error_summary = graphene.String()
    json_ext = GenericScalar()


class HouseholdValidationBatchRowGQLType(graphene.ObjectType):
    id = graphene.UUID()
    batch_id = graphene.UUID()
    group_id = graphene.UUID()
    group_individual_id = graphene.UUID()
    individual_id = graphene.UUID()
    project_id = graphene.UUID()
    row_number = graphene.Int()
    verified = graphene.Boolean()
    validation_date = graphene.Date()
    status = graphene.String()
    error_message = graphene.String()
    raw_row = GenericScalar()
    json_ext = GenericScalar()


class HouseholdValidationProjectsGQLType(graphene.ObjectType):
    projects = graphene.List(HouseholdValidationProjectGQLType)
    count = graphene.Int()


class HouseholdValidationBatchesGQLType(graphene.ObjectType):
    batches = graphene.List(HouseholdValidationBatchGQLType)
    count = graphene.Int()


class HouseholdValidationBatchRowsGQLType(graphene.ObjectType):
    rows = graphene.List(HouseholdValidationBatchRowGQLType)
    count = graphene.Int()


class HouseholdValidationErrorReportGQLType(graphene.ObjectType):
    batch_id = graphene.UUID()
    file_name = graphene.String()
    file_base64 = graphene.String()
    error_count = graphene.Int()
