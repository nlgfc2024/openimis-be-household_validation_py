def get_household_form_number(group, individual=None):
    """Return the Excel household identifier without changing Group.code."""
    group_json_ext = getattr(group, "json_ext", None) or {}
    individual_json_ext = getattr(individual, "json_ext", None) or {}
    return (
        group_json_ext.get("form_number")
        or individual_json_ext.get("form_number")
    )
