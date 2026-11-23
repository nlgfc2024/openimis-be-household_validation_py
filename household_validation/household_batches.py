"""Shared batch identity and subject checks for household workbook uploads."""
from uuid import UUID

from django.core.exceptions import ValidationError

from household_validation.models import HouseholdValidationBatch
from household_validation.programmes import INDIVIDUAL


def resolve_household_batch(batch_ids):
    """Return the single file batch ID and existing household batch, if any.

    Unissued legacy files can contain non-UUID identifiers. Keep their fallback
    behavior, but never allow an existing individual batch on the household path.
    """
    ids = {str(value).strip() for value in batch_ids if value is not None and str(value).strip()}
    if len(ids) != 1:
        return None, None
    try:
        batch_id = UUID(ids.pop())
    except ValueError:
        return None, None
    batch = HouseholdValidationBatch.objects.filter(pk=batch_id).first()
    if batch and (batch.json_ext or {}).get("subject") == INDIVIDUAL:
        raise ValidationError("Household uploads cannot use an individual validation batch.")
    return batch_id, batch
