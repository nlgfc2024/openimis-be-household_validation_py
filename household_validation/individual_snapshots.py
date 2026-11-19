"""Issued selection rows, kept separate from later upload attempts."""


SELECTION_STORAGE = "BATCH_ROWS"
SELECTION_ROW_KIND = "INDIVIDUAL_SELECTION"


def is_individual_list(batch):
    data = batch.json_ext or {}
    return data.get("subject") == "INDIVIDUAL" and (
        data.get("selection_storage") == SELECTION_STORAGE or isinstance(data.get("selection"), list)
    )


def selection_rows(batch):
    return batch.rows.filter(
        is_deleted=False, upload_attempt_id__isnull=True, json_ext__kind=SELECTION_ROW_KIND,
    ).order_by("row_number", "pk")


def selected_individual_ids(batch):
    if batch.json_ext.get("selection_storage") == SELECTION_STORAGE:
        return selection_rows(batch).values_list("individual_id", flat=True)
    # Compatibility for lists issued before row storage was introduced.
    return [row["individual_uuid"] for row in batch.json_ext["selection"]]


def iter_selection(batch, individual_ids=None):
    if batch.json_ext.get("selection_storage") == SELECTION_STORAGE:
        rows = selection_rows(batch)
        if individual_ids is not None:
            rows = rows.filter(individual_id__in=individual_ids)
        yield from rows.values_list("raw_row", flat=True).iterator(chunk_size=500)
    else:
        allowed = None if individual_ids is None else {str(pk) for pk in individual_ids}
        for row in batch.json_ext["selection"]:
            if allowed is None or row["individual_uuid"] in allowed:
                yield row
