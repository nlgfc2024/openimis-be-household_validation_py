from datetime import date

from django.db.models import Q

from individual.models import Group, GroupIndividual

from household_validation.selection import (
    EligibleHousehold,
    EligibleMember,
    is_truthy,
    select_households,
)


class EligibleHouseholdSelectionService:
    def __init__(self, user=None):
        self.user = user

    def select(
        self,
        district_id=None,
        ta_id=None,
        village_id=None,
        exclude_verified_after=None,
        target_count=None,
        reserve_percentage=10,
    ):
        queryset = self._base_queryset()
        queryset = self._apply_location_filters(
            queryset,
            district_id=district_id,
            ta_id=ta_id,
            village_id=village_id,
        )
        candidates = [
            household
            for household in (self._build_household(group) for group in queryset)
            if household is not None
        ]
        return select_households(
            candidates,
            target_count=target_count,
            reserve_percentage=reserve_percentage,
            exclude_verified_after=exclude_verified_after,
        )

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
        district_id=None,
        ta_id=None,
        village_id=None,
    ):
        if village_id:
            return queryset.filter(location_id=village_id)
        if ta_id:
            return queryset.filter(
                Q(location_id=ta_id) | Q(location__parent_id=ta_id)
            )
        if district_id:
            return queryset.filter(
                Q(location_id=district_id)
                | Q(location__parent_id=district_id)
                | Q(location__parent__parent_id=district_id)
            )
        return queryset

    def _build_household(self, group):
        groupindividuals = list(group.groupindividuals.all())
        eligible_members = [
            self._build_member(group_individual)
            for group_individual in groupindividuals
            if self._is_fit_for_work(group_individual)
        ]
        eligible_members = [member for member in eligible_members if member is not None]
        if not eligible_members:
            return None

        head = self._find_head(group, groupindividuals)
        wealth_quintile = self._get_wealth_quintile(group, head, eligible_members)

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

    def _get_wealth_quintile(self, group, head, eligible_members):
        group_json_ext = group.json_ext or {}
        if group_json_ext.get("household_wealth_quintile") is not None:
            return group_json_ext.get("household_wealth_quintile")
        if head and head.source:
            head_json_ext = head.source.individual.json_ext or {}
            if head_json_ext.get("household_wealth_quintile") is not None:
                return head_json_ext.get("household_wealth_quintile")
        for member in eligible_members:
            if member.source:
                member_json_ext = member.source.individual.json_ext or {}
                if member_json_ext.get("household_wealth_quintile") is not None:
                    return member_json_ext.get("household_wealth_quintile")
        return None

    def _parse_date(self, value):
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
