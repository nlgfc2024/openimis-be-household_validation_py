class HouseholdValidationPermissionError(Exception):
    pass


def require_permissions(user, perms, error_class=HouseholdValidationPermissionError):
    if not _has_permissions(user, perms):
        raise error_class("unauthorized")


def _has_permissions(user, perms):
    if not user or getattr(user, "is_anonymous", False) or not getattr(user, "id", None):
        return False
    return user.has_perms(perms)
