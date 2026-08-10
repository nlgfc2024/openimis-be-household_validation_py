from household_validation.selection import is_truthy


def get_household_wealth_quintile(group, group_individuals=None):
    """Resolve wealth quintile consistently for export and upload."""
    if group is None:
        return None

    group_json_ext = getattr(group, "json_ext", None) or {}
    group_value = group_json_ext.get("household_wealth_quintile")
    if group_value is not None:
        return group_value

    if group_individuals is None:
        related_manager = getattr(group, "groupindividuals", None)
        if related_manager is None:
            return None
        group_individuals = related_manager.all()
    group_individuals = list(group_individuals)

    head = _find_head(group_json_ext.get("head_id"), group_individuals)
    head_value = _individual_wealth_quintile(head)
    if head_value is not None:
        return head_value

    for group_individual in group_individuals:
        individual = getattr(group_individual, "individual", None)
        individual_json_ext = getattr(individual, "json_ext", None) or {}
        if not is_truthy(individual_json_ext.get("fit_for_work")):
            continue
        member_value = individual_json_ext.get("household_wealth_quintile")
        if member_value is not None:
            return member_value
    return None


def _find_head(head_id, group_individuals):
    for group_individual in group_individuals:
        if str(getattr(group_individual, "role", "")).upper() == "HEAD":
            return group_individual
    if head_id is not None:
        for group_individual in group_individuals:
            individual_id = getattr(group_individual, "individual_id", "")
            if str(individual_id) == str(head_id):
                return group_individual
    return None


def _individual_wealth_quintile(group_individual):
    if group_individual is None:
        return None
    individual = getattr(group_individual, "individual", None)
    individual_json_ext = getattr(individual, "json_ext", None) or {}
    return individual_json_ext.get("household_wealth_quintile")
