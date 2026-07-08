from django.contrib import admin

from household_validation.models import (
    HouseholdValidationBatch,
    HouseholdValidationBatchRow,
)


class HouseholdValidationBatchRowInline(admin.TabularInline):
    model = HouseholdValidationBatchRow
    extra = 0
    fields = (
        "row_number",
        "group",
        "individual",
        "group_individual",
        "project",
        "verified",
        "validation_date",
        "status",
        "error_message",
    )
    readonly_fields = fields
    can_delete = False


@admin.register(HouseholdValidationBatch)
class HouseholdValidationBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_file_name",
        "status",
        "district",
        "ta",
        "village",
        "target_count",
        "generated_at",
        "uploaded_at",
    )
    list_filter = ("status", "district", "ta", "village")
    search_fields = (
        "source_file_name",
        "hotspot_code",
        "catchment_code",
        "error_summary",
    )
    readonly_fields = ("id", "date_created", "date_updated")
    inlines = (HouseholdValidationBatchRowInline,)


@admin.register(HouseholdValidationBatchRow)
class HouseholdValidationBatchRowAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "batch",
        "row_number",
        "group",
        "individual",
        "project",
        "verified",
        "validation_date",
        "status",
    )
    list_filter = ("status", "verified", "validation_date")
    search_fields = (
        "batch__source_file_name",
        "group__code",
        "individual__first_name",
        "individual__last_name",
        "error_message",
    )
    readonly_fields = ("id", "date_created", "date_updated")
