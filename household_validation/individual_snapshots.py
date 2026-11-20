"""Issued selection rows, kept separate from later upload attempts."""


SELECTION_STORAGE = "BATCH_ROWS"
SELECTION_ROW_KIND = "INDIVIDUAL_SELECTION"


def is_individual_list(batch):
    data = batch.json_ext or {}
    return data.get("subject") == "INDIVIDUAL" and data.get("selection_storage") == SELECTION_STORAGE


def selection_rows(batch):
    return batch.rows.filter(
        is_deleted=False, upload_attempt_id__isnull=True, json_ext__kind=SELECTION_ROW_KIND,
    ).order_by("row_number", "pk")


def selected_individual_ids(batch):
    return selection_rows(batch).values_list("individual_id", flat=True)


def iter_selection(batch, individual_ids=None):
    rows = selection_rows(batch)
    if individual_ids is not None:
        rows = rows.filter(individual_id__in=individual_ids)
    yield from rows.values_list("raw_row", flat=True).iterator(chunk_size=500)
