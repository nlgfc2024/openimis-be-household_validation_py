"""Individual workbooks use trusted batch snapshots, never live programme columns."""
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import UUID
from zipfile import BadZipFile

from django.core.exceptions import ValidationError
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, Protection
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.datavalidation import DataValidation

from household_validation.excel import ExcelValidationListExporter
from household_validation.individual_snapshots import iter_selection
from household_validation.upload import _parse_extra_columns, _to_bytes_io
from household_validation.verification import INDIVIDUAL_DECISIONS, parse_individual_decision


INDIVIDUAL_SHEET = "Individual Validation"
METADATA_SHEET = "Validation Metadata"
BASE_COLUMNS = (
    "batch_id", "individual_uuid", "first_name", "last_name", "dob", "decision", "validation_notes",
)
LEGACY_BASE_COLUMNS = (*BASE_COLUMNS[:5], "national_id", "gender", "District", "TA", "GVH", "Village",
                       *BASE_COLUMNS[5:])
EDITABLE_COLUMNS = {"decision", "validation_notes"}


def normalise_header(value):
    return " ".join(str(value).replace("_", " ").split()).casefold()


def workbook_base_columns(batch):
    fields = batch.json_ext.get("display_fields")
    if fields is None:
        return list(LEGACY_BASE_COLUMNS)
    return [*BASE_COLUMNS[:5], *(col["column_name"] for col in fields), *BASE_COLUMNS[5:]]


def validate_display_fields(fields):
    if not isinstance(fields, list):
        raise ValidationError("Individual workbook display fields must be a list.")
    headers = {normalise_header(name) for name in BASE_COLUMNS}
    for col in fields:
        if not isinstance(col, dict) or not isinstance(col.get("column_name"), str) or not col["column_name"].strip():
            raise ValidationError("Individual display fields require a non-empty column_name.")
        header = normalise_header(col["column_name"])
        if header in headers:
            raise ValidationError("Individual display fields repeat or overwrite a reserved field.")
        headers.add(header)
        sources = set(col) & {"json_ext_key", "location_type"}
        if len(sources) != 1 or set(col) - {"column_name", "json_ext_key", "location_type"}:
            raise ValidationError("Individual display fields require exactly one json_ext_key or location_type.")
        source = sources.pop()
        if not isinstance(col[source], str) or not col[source].strip():
            raise ValidationError("Individual display field sources must be non-empty strings.")
        if source == "location_type" and col[source] not in ("R", "D", "W", "V"):
            raise ValidationError("Individual display location_type must be R, D, W or V.")


def validate_individual_columns(columns, display_fields=None):
    """Reject ambiguous/unsupported definitions before saving an issued list."""
    if not isinstance(columns, list):
        raise ValidationError("Individual workbook columns must be a list.")
    keys, targets = set(), set()
    validate_display_fields(display_fields or [])
    headers = {normalise_header(c) for c in BASE_COLUMNS}
    headers.update(normalise_header(col["column_name"]) for col in (display_fields or []))
    for col in columns:
        if not isinstance(col, dict):
            raise ValidationError("Each individual workbook column must be an object.")
        for field_name in ("key", "column_name", "target_individual_json_ext_key"):
            if not isinstance(col.get(field_name), str) or not col[field_name].strip():
                raise ValidationError(f"Individual column {field_name} must be a non-empty string.")
        key, target = col["key"], col["target_individual_json_ext_key"]
        header = normalise_header(col["column_name"])
        if key in keys or header in headers or target in targets or target == "validation_status":
            raise ValidationError("Individual workbook columns repeat or overwrite a reserved field.")
        keys.add(key)
        targets.add(target)
        headers.add(header)
        if col.get("type") not in ("text", "select", "number"):
            raise ValidationError(f"Unsupported individual column type for {key}.")
        if col.get("type") == "select":
            options = col.get("options")
            if not isinstance(options, list) or not options or any(
                not isinstance(option, (str, int, float)) or isinstance(option, bool)
                or (isinstance(option, float) and not math.isfinite(option)) for option in options
            ):
                raise ValidationError(f"Select column {key} must have non-empty scalar options.")
        for bound in ("min", "max"):
            if bound in col and (type(col[bound]) not in (int, float) or not math.isfinite(col[bound])):
                raise ValidationError(f"Invalid numeric {bound} for {key}.")
        if col.get("min") is not None and col.get("max") is not None and col["min"] > col["max"]:
            raise ValidationError(f"Invalid numeric bounds for {key}.")
    registry = {col["key"]: col for col in columns}
    for col in columns:
        visited = {col["key"]}
        current = col
        while current.get("depends_on"):
            dependency = current["depends_on"]
            if not isinstance(dependency, dict) or "equals" not in dependency:
                raise ValidationError("Column dependency must specify key and equals.")
            key = dependency.get("key")
            if not isinstance(key, str) or key not in registry or key in visited:
                raise ValidationError("Column dependency is missing or cyclic.")
            visited.add(key)
            current = registry[key]


class IndividualValidationListExporter(ExcelValidationListExporter):
    """Reuse configurable-column formatting without household rows or decisions."""
    def __init__(self, batch, rows=None, availability=None):
        self.batch = batch
        self.rows = rows
        self.availability = availability or {}
        self.additional_columns = batch.json_ext["columns"]
        self.columns = workbook_base_columns(batch) + [col["column_name"] for col in self.additional_columns]
        self._column_by_key = {col["key"]: col for col in self.additional_columns}
        self._select_ranges = {}
        self._extra_unavailable_range = None

    def export_workbook(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = INDIVIDUAL_SHEET
        sheet.append(self.columns)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        editable = EDITABLE_COLUMNS | {c["column_name"] for c in self.additional_columns}
        for row in iter_selection(self.batch) if self.rows is None else self.rows:
            sheet.append([str(self.batch.pk) if header == "batch_id" else row.get(header) for header in self.columns])
            for cell, header in zip(sheet[sheet.max_row], self.columns):
                # Values originating in registry data must remain literal text.
                if isinstance(cell.value, str):
                    cell.data_type = "s"
                cell.protection = Protection(locked=header not in editable)
                if isinstance(cell.value, str):
                    cell.number_format = "@"
        metadata = workbook.create_sheet(METADATA_SHEET)
        for key, value in (("batch_id", str(self.batch.pk)), ("subject", "INDIVIDUAL"),
                           ("benefit_plan_code", self.batch.json_ext["benefit_plan_code"])):
            metadata.append([key, value])
            metadata.cell(metadata.max_row, 2).data_type = "s"
        for key, value in self.availability.items():
            metadata.append([key, value])
        metadata.sheet_state = "hidden"
        self._write_extra_column_options(workbook)
        maximum = max(sheet.max_row, 2)
        decision = DataValidation(type="list", formula1='"' + ",".join(INDIVIDUAL_DECISIONS) + '"',
                                  allow_blank=True, showErrorMessage=True, errorStyle="stop")
        sheet.add_data_validation(decision)
        letter = self._column_letter("decision")
        decision.add(f"{letter}2:{letter}{maximum}")
        for col in self.additional_columns:
            self._apply_extra_column_validation(sheet, col, maximum)
        self._apply_protection(sheet)
        self._autosize_columns(sheet)
        for header in ("batch_id", "individual_uuid"):
            sheet.column_dimensions[self._column_letter(header)].hidden = True
        sheet.freeze_panes = "C2"
        sheet.auto_filter.ref = sheet.dimensions
        return workbook


@dataclass
class ParsedIndividualValidationRow:
    row_number: int
    individual_id: str | None
    values: dict
    decision: str | None = None
    updates: dict = field(default_factory=dict)
    notes: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass
class IndividualWorkbookParseResult:
    batch_id: str | None = None
    rows: list[ParsedIndividualValidationRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def valid_rows(self):
        return [] if self.errors else [row for row in self.rows if not row.errors]

    @property
    def rows_read(self):
        return len(self.rows)


def _structural_value(value):
    if isinstance(value, datetime):
        value = value.date()
    return value.isoformat() if isinstance(value, date) else (str(value) if value is not None else "")


def load_individual_workbook(file_or_bytes):
    try:
        return load_workbook(_to_bytes_io(file_or_bytes), data_only=False)
    except (BadZipFile, OSError, ValueError, KeyError, TypeError, InvalidFileException) as exc:
        raise ValidationError("Invalid individual validation workbook.") from exc


def workbook_batch_id(workbook):
    if METADATA_SHEET not in workbook.sheetnames:
        raise ValidationError(f"Missing worksheet: {METADATA_SHEET}")
    values = {}
    for key, value in workbook[METADATA_SHEET].iter_rows(min_col=1, max_col=2, values_only=True):
        if key in values:
            raise ValidationError("Duplicate workbook metadata.")
        values[key] = value
    try:
        batch_id = str(UUID(str(values.get("batch_id"))))
    except ValueError as exc:
        raise ValidationError("Missing or invalid workbook batch ID.") from exc
    if values.get("subject") != "INDIVIDUAL":
        raise ValidationError("Workbook subject must be INDIVIDUAL.")
    return batch_id, values.get("benefit_plan_code")


def parse_individual_validation_workbook(workbook, batch):
    """Pure parsing against a trusted, authorized saved batch; applies no writes."""
    result = IndividualWorkbookParseResult(batch_id=str(batch.pk))
    try:
        batch_id, programme = workbook_batch_id(workbook)
        if batch_id != str(batch.pk) or programme != batch.json_ext["benefit_plan_code"]:
            raise ValidationError("Workbook metadata does not match the saved batch.")
        if INDIVIDUAL_SHEET not in workbook.sheetnames:
            raise ValidationError(f"Missing worksheet: {INDIVIDUAL_SHEET}")
        columns = batch.json_ext["columns"]
        base_columns = workbook_base_columns(batch)
        expected = base_columns + [col["column_name"] for col in columns]
        canonical = {normalise_header(name): name for name in expected}
        sheet = workbook[INDIVIDUAL_SHEET]
        headers = {}
        for index, cell in enumerate(sheet[1], start=1):
            if cell.value is None:
                continue
            name = canonical.get(normalise_header(cell.value))
            if name is None or name in headers:
                raise ValidationError("Unknown or duplicate individual workbook column.")
            headers[name] = index
        if set(headers) != set(expected):
            raise ValidationError("Missing required individual workbook columns.")
    except ValidationError as exc:
        result.errors.extend(exc.messages)
        return result
    registry = {col["key"]: col for col in columns}
    selected = {row["individual_uuid"]: row for row in iter_selection(batch)}
    identities = {}
    for number in range(2, sheet.max_row + 1):
        cells = {name: sheet.cell(number, index) for name, index in headers.items()}
        values = {name: cell.value for name, cell in cells.items()}
        if all(value in (None, "") for value in values.values()):
            continue
        row = ParsedIndividualValidationRow(number, None, values)
        result.rows.append(row)
        try:
            row.individual_id = str(UUID(str(values["individual_uuid"])))
        except ValueError:
            row.errors.append(f"Row {number}: invalid individual UUID")
        if _structural_value(values["batch_id"]) != str(batch.pk):
            result.errors.append("Workbook contains mixed or mismatched batch IDs.")
            row.errors.append(f"Row {number}: batch ID does not match")
        original = selected.get(row.individual_id)
        if original is None:
            row.errors.append(f"Row {number}: individual is not in the saved selection")
        else:
            for name in base_columns:
                if name not in EDITABLE_COLUMNS | {"batch_id"} and (
                    _structural_value(values[name]) != _structural_value(original[name])
                ):
                    row.errors.append(f"Row {number}: protected field {name} was changed")
        if any(cell.data_type == "f" for cell in cells.values()):
            row.errors.append(f"Row {number}: formulas are not accepted")
        if row.individual_id in identities:
            previous = identities[row.individual_id]
            previous.errors.append(f"Row {previous.row_number}: duplicate individual")
            previous.updates = {}
            row.errors.append(f"Row {number}: duplicate individual")
        elif row.individual_id:
            identities[row.individual_id] = row
        try:
            row.decision = parse_individual_decision(values["decision"])
        except ValidationError as exc:
            row.errors.extend(f"Row {number}: {message}" for message in exc.messages)
        if row.decision is not None and not row.errors:
            row.updates, errors = _parse_extra_columns(number, values, registry)
            row.errors.extend(errors)
            if any(isinstance(value, float) and not math.isfinite(value) for value in row.updates.values()):
                row.errors.append(f"Row {number}: numeric value is out of range")
            row.notes = str(values["validation_notes"]) if values["validation_notes"] is not None else None
        if row.errors:
            row.updates = {}
    return result
