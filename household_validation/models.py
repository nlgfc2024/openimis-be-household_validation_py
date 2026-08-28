from django.db import models
from django.utils.translation import gettext_lazy as _

from core.models import HistoryModel
from individual.models import Group, GroupIndividual, Individual
from location.models import Location
from project_social_protection.models import Project


class HouseholdValidationBatch(HistoryModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        PROCESSED = "PROCESSED", _("Processed")
        PARTIAL_SUCCESS = "PARTIAL_SUCCESS", _("Partial success")
        FAILED = "FAILED", _("Failed")

    source_file_name = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PENDING,
    )
    district = models.ForeignKey(
        Location,
        models.DO_NOTHING,
        null=True,
        blank=True,
        related_name="household_validation_district_batches",
    )
    ta = models.ForeignKey(
        Location,
        models.DO_NOTHING,
        null=True,
        blank=True,
        related_name="household_validation_ta_batches",
    )
    village = models.ForeignKey(
        Location,
        models.DO_NOTHING,
        null=True,
        blank=True,
        related_name="household_validation_village_batches",
    )
    hotspot_code = models.CharField(max_length=64, null=True, blank=True)
    catchment_code = models.CharField(max_length=64, null=True, blank=True)
    exclude_verified_after = models.DateField(null=True, blank=True)
    target_count = models.PositiveIntegerField(null=True, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    error_summary = models.TextField(null=True, blank=True)
    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    def __str__(self):
        return f"{self.source_file_name or 'Household validation batch'} ({self.status})"

    class Meta:
        verbose_name = _("Household Validation Batch")
        verbose_name_plural = _("Household Validation Batches")


class HouseholdValidationBatchRow(HistoryModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        APPLIED = "APPLIED", _("Applied")
        SKIPPED = "SKIPPED", _("Skipped")
        ERROR = "ERROR", _("Error")

    batch = models.ForeignKey(
        HouseholdValidationBatch,
        models.CASCADE,
        related_name="rows",
    )
    upload_attempt_id = models.UUIDField(null=True, blank=True, db_index=True)
    group = models.ForeignKey(
        Group,
        models.DO_NOTHING,
        null=True,
        blank=True,
        related_name="household_validation_rows",
    )
    group_individual = models.ForeignKey(
        GroupIndividual,
        models.DO_NOTHING,
        null=True,
        blank=True,
        related_name="household_validation_rows",
    )
    individual = models.ForeignKey(
        Individual,
        models.DO_NOTHING,
        null=True,
        blank=True,
        related_name="household_validation_rows",
    )
    project = models.ForeignKey(
        Project,
        models.DO_NOTHING,
        null=True,
        blank=True,
        related_name="household_validation_rows",
    )
    row_number = models.PositiveIntegerField()
    verified = models.BooleanField(null=True, blank=True)
    validation_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PENDING,
    )
    error_message = models.TextField(null=True, blank=True)
    raw_row = models.JSONField(blank=True, default=dict)
    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    def __str__(self):
        return f"{self.batch_id} row {self.row_number} ({self.status})"

    class Meta:
        verbose_name = _("Household Validation Batch Row")
        verbose_name_plural = _("Household Validation Batch Rows")
        ordering = ("batch", "row_number")
