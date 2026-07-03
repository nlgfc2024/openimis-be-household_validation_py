from dataclasses import dataclass, field
from datetime import date, datetime
from io import BytesIO

from openpyxl import load_workbook

from household_validation.excel import (
    EXCEL_COLUMNS,
    PROJECT_OPTIONS_SHEET,
    PROJECT_OPTIONS_HEADERS,
)


VALIDATION_LIST_SHEET = "Validation List"
PROJECT_SELECTION_TYPE_INTENT = "INTENT"

EDITABLE_UPLOAD_COLUMNS = {
    "participant",
    "verified",
    "validation_date",
    "project",
    "validation_notes",
}
STRUCTURAL_UPLOAD_COLUMNS = tuple(
    column for column in EXCEL_COLUMNS if column not in EDITABLE_UPLOAD_COLUMNS
)

YES_VALUES = {"YES", "Y", "TRUE", "1"}
NO_VALUES = {"NO", "N", "FALSE", "0"}


@dataclass(frozen=True)
class UploadedValidationRow:
    row_number: int
    values: dict
    verified: bool | None
    participant: bool
    validation_date: date | None
    project_name: str | None
    project_id: str | None
    notes: str | None


@dataclass(frozen=True)
class WorkbookParseResult:
    rows: list[UploadedValidationRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    project_options: dict = field(default_factory=dict)

    @property
    def rows_read(self):
        return len(self.rows)


def parse_validation_workbook(file_or_bytes):
    workbook = load_workbook(_to_bytes_io(file_or_bytes), data_only=True)
    errors = []
    if VALIDATION_LIST_SHEET not in workbook.sheetnames:
        return WorkbookParseResult(errors=[f"Missing worksheet: {VALIDATION_LIST_SHEET}"])

    worksheet = workbook[VALIDATION_LIST_SHEET]
    headers = _read_headers(worksheet)
    missing_columns = [column for column in EXCEL_COLUMNS if column not in headers]
    if missing_columns:
        return WorkbookParseResult(
            errors=[f"Missing required columns: {', '.join(missing_columns)}"]
        )

    project_options = _read_project_options(workbook)
    rows = []
    for row_number in range(2, worksheet.max_row + 1):
        values = {
            column: _cell_value(worksheet.cell(row=row_number, column=headers[column]))
            for column in EXCEL_COLUMNS
        }
        if _is_blank_row(values):
            continue
        row_errors = _validate_structural_values(row_number, values)
        verified = _parse_yes_no(values.get("verified"))
        participant = _parse_yes_no(values.get("participant")) is True
        validation_date = _parse_date(values.get("validation_date"))
        project_name = _clean(values.get("project"))
        project_id = _resolve_project_id(
            project_name=project_name,
            workbook_project_id=_clean(values.get("project_id")),
            project_options=project_options,
        )
        if values.get("project_id") and not project_name:
            row_errors.append(f"Row {row_number}: project_id cannot be set without project")
        if values.get("verified") not in (None, "") and verified is None:
            row_errors.append(f"Row {row_number}: verified must be YES or NO")
        if values.get("participant") not in (None, "") and _parse_yes_no(values.get("participant")) is None:
            row_errors.append(f"Row {row_number}: participant must be YES or NO")
        if values.get("validation_date") and validation_date is None:
            row_errors.append(f"Row {row_number}: validation_date is invalid")
        if project_name and not project_id:
            row_errors.append(f"Row {row_number}: project is not in the project options")
        if row_errors:
            errors.extend(row_errors)
            continue
        rows.append(
            UploadedValidationRow(
                row_number=row_number,
                values=values,
                verified=verified,
                participant=participant,
                validation_date=validation_date,
                project_name=project_name,
                project_id=project_id,
                notes=_clean(values.get("validation_notes")),
            )
        )
    return WorkbookParseResult(rows=rows, errors=errors, project_options=project_options)


def _to_bytes_io(file_or_bytes):
    if isinstance(file_or_bytes, bytes):
        return BytesIO(file_or_bytes)
    if hasattr(file_or_bytes, "read"):
        content = file_or_bytes.read()
        if hasattr(file_or_bytes, "seek"):
            file_or_bytes.seek(0)
        return BytesIO(content)
    return file_or_bytes


def _read_headers(worksheet):
    headers = {}
    for column_number in range(1, worksheet.max_column + 1):
        value = _clean(worksheet.cell(row=1, column=column_number).value)
        if value:
            headers[value] = column_number
    return headers


def _read_project_options(workbook):
    if PROJECT_OPTIONS_SHEET not in workbook.sheetnames:
        return {}
    worksheet = workbook[PROJECT_OPTIONS_SHEET]
    headers = _read_headers(worksheet)
    if not all(header in headers for header in PROJECT_OPTIONS_HEADERS):
        return {}
    options = {}
    for row_number in range(2, worksheet.max_row + 1):
        project_id = _clean(worksheet.cell(row=row_number, column=headers["project_id"]).value)
        project_name = _clean(worksheet.cell(row=row_number, column=headers["project"]).value)
        if project_id and project_name:
            options[project_name] = project_id
    return options


def _validate_structural_values(row_number, values):
    errors = []
    for column in ("batch_id", "group_uuid", "member_uuid"):
        if not _clean(values.get(column)):
            errors.append(f"Row {row_number}: {column} is required")
    return errors


def _is_blank_row(values):
    return all(value in (None, "") for value in values.values())


def _cell_value(cell):
    value = cell.value
    if isinstance(value, datetime):
        return value.date()
    return value


def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_yes_no(value):
    value = _clean(value)
    if value is None:
        return None
    normalized = value.upper()
    if normalized in YES_VALUES:
        return True
    if normalized in NO_VALUES:
        return False
    return None


def _parse_date(value):
    if isinstance(value, date):
        return value
    value = _clean(value)
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _resolve_project_id(project_name, workbook_project_id, project_options):
    if not project_name:
        return workbook_project_id
    project_id = project_options.get(project_name)
    if workbook_project_id and project_id and workbook_project_id != project_id:
        return None
    return project_id or workbook_project_id
