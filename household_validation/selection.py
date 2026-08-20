import math
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from typing import Any


WEALTH_RANK = {
    "poorest": 1,
    "poorer": 2,
    "middle": 3,
    "richer": 4,
    "richest": 5,
}

CATEGORY_FEMALE_HEADED = "FEMALE_HEADED"
CATEGORY_YOUTH = "YOUTH"
CATEGORY_OTHER = "OTHER"

ROW_TYPE_MAIN = "MAIN"
ROW_TYPE_RESERVE = "RESERVE"


@dataclass(frozen=True)
class EligibleMember:
    id: Any
    gender: str | None = None
    dob: date | None = None
    fit_for_work: bool = False
    role: str | None = None
    recipient_type: str | None = None
    source: Any = None

    @property
    def age(self) -> int | None:
        if not self.dob:
            return None
        today = date.today()
        return today.year - self.dob.year - (
            (today.month, today.day) < (self.dob.month, self.dob.day)
        )


@dataclass
class EligibleHousehold:
    id: Any
    code: str | None = None
    wealth_quintile: str | int | None = None
    pmt_score: str | float | None = None
    last_verified_date: date | None = None
    head: EligibleMember | None = None
    eligible_members: list[EligibleMember] = field(default_factory=list)
    source: Any = None


@dataclass(frozen=True)
class SelectedHousehold:
    household: EligibleHousehold
    category: str
    row_type: str


@dataclass(frozen=True)
class SelectedMember:
    household: EligibleHousehold
    member: EligibleMember
    category: str
    row_type: str


@dataclass(frozen=True)
class SelectionResult:
    main: list[SelectedHousehold]
    reserve: list[SelectedHousehold]

    @property
    def selected(self) -> list[SelectedHousehold]:
        return [*self.main, *self.reserve]

    @cached_property
    def member_rows(self) -> list[SelectedMember]:
        return [
            SelectedMember(
                household=selected.household,
                member=member,
                category=selected.category,
                row_type=selected.row_type,
            )
            for selected in self.selected
            for member in selected.household.eligible_members
        ]


def is_truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def normalize_gender(value):
    if value is None:
        return None
    gender = str(value).strip().lower()
    if gender in {"f", "female", "woman"}:
        return "female"
    if gender in {"m", "male", "man"}:
        return "male"
    return gender


def normalize_wealth_rank(value):
    if value is None:
        return 999
    if isinstance(value, int):
        return value if value > 0 else 999
    if isinstance(value, float):
        return int(value) if value > 0 else 999
    text = str(value).strip().lower()
    if text.isdigit():
        number = int(text)
        return number if number > 0 else 999
    return WEALTH_RANK.get(text, 999)


def categorize_household(household: EligibleHousehold):
    if household.head and normalize_gender(household.head.gender) == "female":
        return CATEGORY_FEMALE_HEADED

    for member in household.eligible_members:
        if member.age is not None and 18 <= member.age <= 35:
            return CATEGORY_YOUTH

    return CATEGORY_OTHER


def household_sort_key(household: EligibleHousehold):
    return (
        normalize_wealth_rank(household.wealth_quintile),
        str(household.code or ""),
        str(household.id),
    )


def exclude_recently_verified(households, exclude_verified_after=None):
    if exclude_verified_after is None:
        return list(households)
    return [
        household
        for household in households
        if not household.last_verified_date
        or household.last_verified_date <= exclude_verified_after
    ]


def _configured_percentage(attr_name, default):
    from household_validation.apps import HouseholdValidationConfig

    value = getattr(HouseholdValidationConfig, attr_name, None)
    if value is None:
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(value, 100))


def female_headed_percentage():
    return _configured_percentage("female_headed_percentage", 40)


def youth_percentage():
    return _configured_percentage("youth_percentage", 40)


def reserve_percentage():
    return _configured_percentage("reserve_percentage", 20)


def _allocate_quotas(target_count):
    female_weight = female_headed_percentage() / 100
    youth_weight = youth_percentage() / 100
    if female_weight + youth_weight > 1:
        total = female_weight + youth_weight
        female_weight = female_weight / total
        youth_weight = youth_weight / total
    other_weight = max(0, 1 - female_weight - youth_weight)
    weights = [
        (CATEGORY_FEMALE_HEADED, female_weight),
        (CATEGORY_YOUTH, youth_weight),
        (CATEGORY_OTHER, other_weight),
    ]
    exact = [(category, target_count * weight) for category, weight in weights]
    quotas = {category: math.floor(value) for category, value in exact}
    remaining = target_count - sum(quotas.values())
    remainders = sorted(
        exact,
        key=lambda item: (item[1] - math.floor(item[1])),
        reverse=True,
    )
    for category, _value in remainders[:remaining]:
        quotas[category] += 1
    return quotas


def select_households(
    households,
    target_count=None,
    exclude_verified_after=None,
):
    eligible = [
        household
        for household in exclude_recently_verified(households, exclude_verified_after)
        if household.eligible_members
    ]
    eligible = sorted(eligible, key=household_sort_key)

    main_target = len(eligible) if target_count is None else target_count
    main_target = max(0, min(main_target, len(eligible)))
    quotas = _allocate_quotas(main_target)

    by_category = {
        CATEGORY_FEMALE_HEADED: [],
        CATEGORY_YOUTH: [],
        CATEGORY_OTHER: [],
    }
    household_categories = {}
    for household in eligible:
        category = categorize_household(household)
        household_categories[household.id] = category
        by_category[category].append(household)

    selected_ids = set()
    main = []
    category_counts = {
        CATEGORY_FEMALE_HEADED: 0,
        CATEGORY_YOUTH: 0,
        CATEGORY_OTHER: 0,
    }
    selected_individuals = 0
    for category in (CATEGORY_FEMALE_HEADED, CATEGORY_YOUTH, CATEGORY_OTHER):
        for household in by_category[category]:
            if len(main) >= main_target:
                break
            if category_counts[category] >= quotas[category]:
                break
            selected_ids.add(household.id)
            category_counts[category] += 1
            selected_individuals += len(household.eligible_members)
            main.append(SelectedHousehold(household, category, ROW_TYPE_MAIN))

    if len(main) < main_target:
        for household in eligible:
            if household.id in selected_ids:
                continue
            selected_ids.add(household.id)
            category = household_categories[household.id]
            category_counts[category] += 1
            selected_individuals += len(household.eligible_members)
            main.append(SelectedHousehold(household, category, ROW_TYPE_MAIN))
            if len(main) >= main_target:
                break

    reserve_target = math.ceil(main_target * reserve_percentage() / 100)
    reserve_target = max(0, min(reserve_target, len(eligible) - len(selected_ids)))

    reserve = []
    for household in eligible:
        if household.id in selected_ids:
            continue
        selected_ids.add(household.id)
        reserve.append(
            SelectedHousehold(
                household,
                household_categories[household.id],
                ROW_TYPE_RESERVE,
            )
        )
        if len(reserve) >= reserve_target:
            break

    selection_result = SelectionResult(main=main, reserve=reserve)
    summary = {
        "selected_households": len(main),
        "selected_individuals": selected_individuals,
        "selected_female_headed_households": category_counts[CATEGORY_FEMALE_HEADED],
        "selected_youth_households": category_counts[CATEGORY_YOUTH],
        "selected_other_households": category_counts[CATEGORY_OTHER],
        "reserve_households": len(reserve),
    }
    return selection_result, summary
