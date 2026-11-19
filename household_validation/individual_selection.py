"""UMEP candidate selection; independent of household validation decisions."""
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Exists, OuterRef, Q, QuerySet

from individual.data_source import DEFAULT_DATA_SOURCE, data_source_q
from individual.models import GroupIndividual, Individual

from household_validation.geography import ValidationGeographyMixin
from household_validation.programmes import INDIVIDUAL, resolve_programme_rule
from household_validation.verification import INDIVIDUAL_DECISIONS


@dataclass(frozen=True)
class IndividualSelectionResult:
    individuals: QuerySet
    eligible_individuals: int
    selected_individuals: int


class EligibleIndividualSelectionService(ValidationGeographyMixin):
    def __init__(self, user):
        self.user = user

    def candidates(self, benefit_plan_code, exclude_validated=False, **geography):
        if not self.user or getattr(self.user, "is_anonymous", False) or not getattr(self.user, "id", None):
            raise PermissionDenied("An authenticated user is required for individual validation.")
        if type(exclude_validated) is not bool:
            raise ValidationError("exclude_validated must be a boolean.")
        rule = resolve_programme_rule(benefit_plan_code)
        if rule.get("subject") != INDIVIDUAL:
            raise ValidationError("Individual selection requires an INDIVIDUAL programme.")
        queryset = Individual.get_queryset(Individual.objects.filter(is_deleted=False), self.user)
        # Use the same source-key and legacy untagged-record semantics as enrolment.
        queryset = queryset.filter(data_source_q(rule.get("data_source", DEFAULT_DATA_SOURCE)))
        if exclude_validated:
            # JSON key exclusion alone drops missing keys through SQL NULL;
            # unassessed people must remain candidates.
            queryset = queryset.filter(
                Q(json_ext__validation_status__isnull=True)
                | ~Q(json_ext__validation_status__in=INDIVIDUAL_DECISIONS)
            )
        # Matches enrolment.membership.active_household_memberships without
        # adding an enrolment-module dependency to registry validation.
        active_membership = GroupIndividual.objects.filter(
            individual_id=OuterRef("pk"),
            is_deleted=False,
            group__is_deleted=False,
            individual__is_deleted=False,
        )
        queryset = queryset.annotate(_validation_active_household=Exists(active_membership)).filter(
            _validation_active_household=False,
        )
        return self._apply_location_filters(queryset, **geography).select_related("location").distinct().order_by("pk")

    def select(self, benefit_plan_code, target_count=None, **geography):
        if target_count is not None and (type(target_count) is not int or target_count < 0):
            raise ValidationError("target_count must be a non-negative integer.")
        queryset = self.candidates(benefit_plan_code, **geography)
        eligible_count = queryset.count()
        selected_count = eligible_count if target_count is None else min(target_count, eligible_count)
        return IndividualSelectionResult(queryset[:selected_count], eligible_count, selected_count)
