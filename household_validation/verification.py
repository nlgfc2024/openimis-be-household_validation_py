VERIFIED = "VERIFIED"
NOT_VERIFIED = "NOT_VERIFIED"
REJECTED = "REJECTED"

PARTICIPANT_STATUS_COLUMN = "participant_status"
HOUSEHOLD_STATUS_COLUMN = "household_status"
BUSINESS_REJECTION_CODE = "BUSINESS_WITHOUT_PRIMARY_WORKER"

YES_VALUES = ("YES", "Y", "TRUE", "1")
NO_VALUES = ("NO", "N", "FALSE", "0")


def resolve_participant_status(primary_worker):
    """A member is VERIFIED once the Primary Worker cell is answered at all
    (Yes or No) -- that simply means the member was checked on the ground,
    whether or not they were chosen as the primary worker. NOT_VERIFIED only
    when the cell is blank (primary_worker is None).
    """
    return NOT_VERIFIED if primary_worker is None else VERIFIED


def resolve_household_status(primary_workers):
    """Reject a household with more than one Primary Worker."""
    workers = list(primary_workers)
    if sum(worker is True for worker in workers) > 1:
        return REJECTED
    return VERIFIED if any(worker is True for worker in workers) else NOT_VERIFIED
