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


class HouseholdValidationSummaryGQLType(graphene.ObjectType):
    total_households = graphene.Int()
    total_individuals = graphene.Int()
    eligible_households = graphene.Int()
    eligible_individuals = graphene.Int()
    selected_households = graphene.Int()
    selected_individuals = graphene.Int()
    selected_female_headed_households = graphene.Int()
    selected_youth_households = graphene.Int()
    selected_other_households = graphene.Int()
    reserve_households = graphene.Int()
    main_households = graphene.Int()
    generated_at = graphene.DateTime()


class HouseholdValidationPreviewRowGQLType(graphene.ObjectType):
    row_type = graphene.String()
    category = graphene.String()
    group_uuid = graphene.String()
    group_code = graphene.String()
    head_name = graphene.String()
    individual_uuid = graphene.String()
    individual_first_name = graphene.String()
    individual_last_name = graphene.String()
    individual_dob = graphene.Date()
    individual_age = graphene.Int()
    individual_gender = graphene.String()
    fit_for_work = graphene.Boolean()
    current_recipient_type = graphene.String()
    region = graphene.String()
    district = graphene.String()
    municipality = graphene.String()
    village = graphene.String()
    wealth_quintile = graphene.String()
    last_verified_date = graphene.Date()
    validation_status = graphene.String()
    prospective_projects = graphene.List(graphene.String)


class HouseholdValidationPreviewEdgeGQLType(graphene.ObjectType):
    node = graphene.Field(HouseholdValidationPreviewRowGQLType)


class HouseholdValidationPreviewPageInfoGQLType(graphene.ObjectType):
    has_next_page = graphene.Boolean()
    has_previous_page = graphene.Boolean()
    start_cursor = graphene.String()
    end_cursor = graphene.String()


class HouseholdValidationPreviewGQLType(graphene.ObjectType):
    edges = graphene.List(HouseholdValidationPreviewEdgeGQLType)
    total_count = graphene.Int()
    page_info = graphene.Field(HouseholdValidationPreviewPageInfoGQLType)


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
