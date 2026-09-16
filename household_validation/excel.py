from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from household_validation.identity import get_household_form_number


PRIMARY_WORKER_FORMULA = '"YES,NO"'
HAS_BUSINESS_FORMULA = '"Yes,No"'
HOUSEHOLD_ROW_COLORS = ("FFA9D18E", "FFE2F0D9")

LOCATION_COLUMN_TYPES = {
    "District": "R",
    "TA": "D",
    "GVH": "W",
    "Village": "V",
}

MICRO_CATCHMENT_COLUMN = "Micro-Catchment"
HOTSPOT_COLUMN = "Hotspot"

HAS_BUSINESS_COLUMN = "Does member has a business"
BUSINESS_TYPE_COLUMN = "Type of Business"
BUSINESS_DURATION_COLUMN = "Business Period (in years)"

BUSINESS_TYPE_OPTIONS_SHEET = "Business Type Options"
BUSINESS_TYPE_OPTIONS = [
    "Crop farming",
    "Livestock farming",
    "Poultry farming",
    "Fish farming",
    "Produce buying and selling",
    "Agricultural input sales",
    "Grain milling",
    "Food processing",
    "Grocery shop",
    "General merchandise shop",
    "Market vending",
    "Hardware and building materials sales",
    "Fuel and energy products sales",
    "Mobile money services",
    "Phone and ICT services",
    "Transport services",
    "Motorcycle taxi (Kabaza) services",
    "Restaurants and food outlets",
    "Bakery and confectionery",
    "Butchery",
    "Lodging and guest house services",
    "Tailoring",
    "Carpentry",
    "Welding and metal fabrication",
    "Brick making",
    "Construction services",
    "Plumbing services",
    "Electrical services",
    "Barber shop",
    "Salon",
    "Photography",
    "Printing services",
    "Equipment hire",
    "Education and training services",
    "Healthcare and pharmacy services",
    "Water supply services",
    "Solar and renewable energy services",
    "Waste collection and recycling",
    "Tourism and recreation services",
    "Financial and cooperative services",
    "Machinery hire",
    "Handicrafts and artisan products",
    "Forestry products (firewood, charcoal, timber)",
    "Other businesses",
]


EXCEL_COLUMNS = [
    "batch_id",
    "group_uuid",
    "member_uuid",
    "row_type",
    "District",
    MICRO_CATCHMENT_COLUMN,
    "TA",
    "GVH",
    HOTSPOT_COLUMN,
    "Village",
    "form_number",
    "member_name",
    "relationship",
    "member_dob",
    "national_id",
    "primary_worker",
    "member_gender",
    "member_age",
    "marital_status",
    "disability",
    "fit_for_work",
    "pmt_score",
    "household_wealth_quintile",
    "project",
    "project_id",
    HAS_BUSINESS_COLUMN,
    BUSINESS_TYPE_COLUMN,
    BUSINESS_DURATION_COLUMN,
    "validation_notes",
]

PROJECT_OPTIONS_SHEET = "Project Options"
PROJECT_OPTIONS_HEADERS = ["project_id", "project", "project_label"]

EDITABLE_COLUMNS = {
    "national_id",
    "primary_worker",
    "project",
    HAS_BUSINESS_COLUMN,
    BUSINESS_TYPE_COLUMN,
    BUSINESS_DURATION_COLUMN,
    "validation_notes",
}

TEXT_COLUMNS = {
    "form_number",
    "national_id",
}

PRIMARY_WORKER_REJECTION_CODE = "MULTIPLE_PRIMARY_WORKERS"
PRIMARY_WORKER_REJECTION_MESSAGE = (
    "household has more than one primary worker"
)
LEGACY_PRIMARY_WORKER_REJECTION_MESSAGE = (
    "Rejected: household would have more than one primary worker"
)


def is_primary_worker_rejection(row):
    json_ext = row.json_ext or {}
    return (
        json_ext.get("error_code") == PRIMARY_WORKER_REJECTION_CODE
        or row.error_message
        in {
            PRIMARY_WORKER_REJECTION_MESSAGE,
            LEGACY_PRIMARY_WORKER_REJECTION_MESSAGE,
        }
    )


def build_rejected_households_workbook_bytes(rows):
    rejected_rows = [row for row in rows if is_primary_worker_rejection(row)]
    households = {}
    for row in rejected_rows:
        raw_row = row.raw_row or {}
        group_uuid = str(
            getattr(row, "group_id", None)
            or raw_row.get("group_uuid")
            or f"row-{row.row_number}"
        )
        household = households.setdefault(
            group_uuid,
            {
                "form_number": (
                    raw_row.get("form_number")
                    or raw_row.get("group_code")
                    or group_uuid
                ),
                "group_uuid": group_uuid,
                "row_numbers": set(),
                "rejection_reason": row.error_message,
            },
        )
        if row.row_number is not None:
            household["row_numbers"].add(row.row_number)

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Rejected Households"
    headers = [
        "form_number",
        "group_uuid",
        "workbook_rows",
        "rejection_reason",
    ]
    worksheet.append(headers)

    for household in households.values():
        values = [
            household["form_number"],
            household["group_uuid"],
            ", ".join(
                str(row_number)
                for row_number in sorted(household["row_numbers"])
            ),
            household["rejection_reason"],
        ]
        worksheet.append(values)
        for cell in worksheet[worksheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"

    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill(fill_type="solid", fgColor="D9EAF7")
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for column_index, column_cells in enumerate(worksheet.columns, start=1):
        max_length = max(
            len(str(cell.value)) if cell.value is not None else 0
            for cell in column_cells
        )
        worksheet.column_dimensions[get_column_letter(column_index)].width = min(
            max(max_length + 2, 12),
            50,
        )

    output = BytesIO()
    workbook.save(output)
    return output.getvalue(), len(households)


class ExcelValidationListExporter:
    def __init__(self, selection_result, batch_id, projects=None):
        self.selection_result = selection_result
        self.batch_id = batch_id
        self.projects = projects or []
        self._micro_catchment_cache = {}
        self._hotspot_cache = {}

    def export_workbook(self):
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Validation List"

        self._write_header(worksheet)
        self._write_rows(worksheet)
        project_options_worksheet = self._write_project_options(workbook)
        business_type_options_worksheet = self._write_business_type_options(workbook)
        self._apply_validation(worksheet)
        self._apply_protection(worksheet)
        self._autosize_columns(worksheet)
        self._autosize_columns(project_options_worksheet)
        self._autosize_columns(business_type_options_worksheet)

        project_id_column = EXCEL_COLUMNS.index("project_id") + 1
        worksheet.column_dimensions[worksheet.cell(1, project_id_column).column_letter].hidden = True
        return workbook

    def export_bytes(self):
        output = BytesIO()
        self.export_workbook().save(output)
        output.seek(0)
        return output.getvalue()

    def _write_header(self, worksheet):
        header_fill = PatternFill("solid", fgColor="D9EAD3")
        for column_number, title in enumerate(EXCEL_COLUMNS, start=1):
            cell = worksheet.cell(row=1, column=column_number, value=title)
            cell.font = Font(bold=True)
            cell.fill = header_fill
            cell.protection = Protection(locked=True)
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions

    def _write_rows(self, worksheet):
        household_fills = {}
        for row_number, selected_member in enumerate(self.selection_result.member_rows, start=2):
            household_key = str(selected_member.household.id)
            if household_key not in household_fills:
                color = HOUSEHOLD_ROW_COLORS[
                    len(household_fills) % len(HOUSEHOLD_ROW_COLORS)
                ]
                household_fills[household_key] = PatternFill(
                    fill_type="solid",
                    fgColor=color,
                )
            household_fill = household_fills[household_key]
            values = self._build_row(selected_member)
            for column_number, title in enumerate(EXCEL_COLUMNS, start=1):
                value = values.get(title)
                if title in TEXT_COLUMNS and value is not None:
                    value = str(value)
                cell = worksheet.cell(
                    row=row_number,
                    column=column_number,
                    value=value,
                )
                if title in TEXT_COLUMNS:
                    cell.number_format = "@"
                cell.fill = household_fill
                cell.protection = Protection(locked=title not in EDITABLE_COLUMNS)

    def _write_project_options(self, workbook):
        worksheet = workbook.create_sheet(PROJECT_OPTIONS_SHEET)
        for column_number, title in enumerate(PROJECT_OPTIONS_HEADERS, start=1):
            worksheet.cell(row=1, column=column_number, value=title)
        project_labels = self._project_labels()
        for row_number, project in enumerate(self.projects, start=2):
            worksheet.cell(row=row_number, column=1, value=self._project_id(project))
            worksheet.cell(row=row_number, column=2, value=self._project_name(project))
            worksheet.cell(row=row_number, column=3, value=project_labels[id(project)])
        worksheet.sheet_state = "hidden"
        return worksheet

    def _write_business_type_options(self, workbook):
        worksheet = workbook.create_sheet(BUSINESS_TYPE_OPTIONS_SHEET)
        worksheet.cell(row=1, column=1, value="business_type")
        for row_number, business_type in enumerate(BUSINESS_TYPE_OPTIONS, start=2):
            worksheet.cell(row=row_number, column=1, value=business_type)
        worksheet.sheet_state = "hidden"
        return worksheet

    def _build_row(self, selected_member):
        household = selected_member.household
        member = selected_member.member
        group = household.source
        group_individual = member.source
        individual = getattr(group_individual, "individual", None)
        location = getattr(group, "location", None)

        return {
            "batch_id": str(self.batch_id),
            "row_type": selected_member.row_type,
            **{
                column: self._location_name(location, location_type)
                for column, location_type in LOCATION_COLUMN_TYPES.items()
            },
            MICRO_CATCHMENT_COLUMN: self._micro_catchment_name(location),
            HOTSPOT_COLUMN: self._hotspot_name(location),
            "form_number": get_household_form_number(group, individual),
            "group_uuid": str(household.id),
            "member_uuid": str(member.id),
            "member_name": self._member_name(individual),
            "national_id": self._national_id(individual),
            "member_gender": member.gender,
            "member_dob": member.dob,
            "member_age": member.age,
            "marital_status": self._marital_status(individual),
            "disability": self._disability(individual),
            "fit_for_work": "YES" if member.fit_for_work else "NO",
            "relationship": self._relationship(member.role),
            "pmt_score": household.pmt_score,
            "household_wealth_quintile": household.wealth_quintile,
            # Primary Worker is an upload input. Do not expose or suggest the
            # currently stored assignment in a newly generated workbook.
            "primary_worker": None,
            "project": None,
            "project_id": None,
            HAS_BUSINESS_COLUMN: self._has_business(individual),
            BUSINESS_TYPE_COLUMN: self._business_type(individual),
            BUSINESS_DURATION_COLUMN: self._business_period(individual),
            "validation_notes": None,
        }

    def _apply_validation(self, worksheet):
        max_row = max(worksheet.max_row, 2)
        primary_worker_col = self._column_letter("primary_worker")
        project_col = self._column_letter("project")
        has_business_col = self._column_letter(HAS_BUSINESS_COLUMN)
        business_type_col = self._column_letter(BUSINESS_TYPE_COLUMN)
        business_duration_col = self._column_letter(BUSINESS_DURATION_COLUMN)

        primary_worker_validation = DataValidation(
            type="list",
            formula1=PRIMARY_WORKER_FORMULA,
            allow_blank=True,
        )
        worksheet.add_data_validation(primary_worker_validation)
        primary_worker_validation.add(
            f"{primary_worker_col}2:{primary_worker_col}{max_row}"
        )

        project_count = len([project for project in self.projects if self._project_name(project)])
        if project_count:
            project_formula = f"'{PROJECT_OPTIONS_SHEET}'!$C$2:$C${project_count + 1}"
            project_validation = DataValidation(
                type="list",
                formula1=project_formula,
                allow_blank=True,
            )
            worksheet.add_data_validation(project_validation)
            project_validation.add(f"{project_col}2:{project_col}{max_row}")

        has_business_validation = DataValidation(
            type="list",
            formula1=HAS_BUSINESS_FORMULA,
            allow_blank=True,
        )
        worksheet.add_data_validation(has_business_validation)
        has_business_validation.add(
            f"{has_business_col}2:{has_business_col}{max_row}"
        )

        business_type_options_range = (
            f"'{BUSINESS_TYPE_OPTIONS_SHEET}'!$A$2:$A${len(BUSINESS_TYPE_OPTIONS) + 1}"
        )
        business_type_validation = DataValidation(
            type="list",
            formula1=f'IF(${has_business_col}2="Yes",{business_type_options_range},"")',
            allow_blank=True,
        )
        business_type_validation.error = (
            'Select "Yes" for "Does member has a business" before choosing a business type.'
        )
        business_type_validation.errorTitle = "Business type not applicable"
        business_type_validation.showErrorMessage = True
        worksheet.add_data_validation(business_type_validation)
        business_type_validation.add(
            f"{business_type_col}2:{business_type_col}{max_row}"
        )

        business_duration_validation = DataValidation(
            type="custom",
            formula1=(
                f'AND(ISNUMBER(${business_duration_col}2),'
                f'${business_duration_col}2>=0,'
                f'${business_duration_col}2<=100)'
            ),
            allow_blank=True,
        )
        business_duration_validation.error = (
            'Business period must be between 0 and 100'
        )
        business_duration_validation.errorTitle = "Invalid business period"
        business_duration_validation.showErrorMessage = True
        worksheet.add_data_validation(business_duration_validation)
        business_duration_validation.add(
            f"{business_duration_col}2:{business_duration_col}{max_row}"
        )

    def _apply_protection(self, worksheet):
        worksheet.protection.sheet = True
        worksheet.protection.enable()

    def _autosize_columns(self, worksheet):
        for column_cells in worksheet.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter
            for cell in column_cells:
                value = cell.value
                if value is not None:
                    max_length = max(max_length, len(str(value)))
            worksheet.column_dimensions[column_letter].width = min(max(max_length + 2, 12), 40)

    def _column_letter(self, column_name):
        return get_column_letter(EXCEL_COLUMNS.index(column_name) + 1)

    def _location_name(self, location, location_type):
        current = location
        while current is not None:
            if getattr(current, "type", None) == location_type:
                return getattr(current, "name", None) or getattr(current, "code", None)
            current = getattr(current, "parent", None)
        return None

    def _micro_catchment_name(self, location):
        """Micro-catchment for the row's location.

        A micro-catchment isn't a level in the Region/TA/GVH/Village chain — it's
        a separate grouping of specific GVHs (and TAs) defined in ``location.MicroCatchment``.
        Prefer a GVH-level link (more specific) and fall back to the TA-level link.
        """
        gvh_location = self._location_ancestor(location, "W")
        if gvh_location is not None:
            name = self._micro_catchment_link_name(gvh_location, "micro_catchments_gvh")
            if name:
                return name

        ta_location = self._location_ancestor(location, "D")
        if ta_location is not None:
            return self._micro_catchment_link_name(ta_location, "micro_catchments_ta")

        return None

    def _location_ancestor(self, location, location_type):
        current = location
        while current is not None:
            if getattr(current, "type", None) == location_type:
                return current
            current = getattr(current, "parent", None)
        return None

    def _micro_catchment_link_name(self, location, related_name):
        related_manager = getattr(location, related_name, None)
        location_id = getattr(location, "id", None)
        if related_manager is None or location_id is None:
            return None
        cache_key = (related_name, location_id)
        if cache_key not in self._micro_catchment_cache:
            link = related_manager.filter(
                validity_to__isnull=True,
                micro_catchment__validity_to__isnull=True,
            ).select_related("micro_catchment").first()
            micro_catchment = link.micro_catchment if link else None
            name = None
            if micro_catchment is not None:
                name = micro_catchment.name or micro_catchment.code
            self._micro_catchment_cache[cache_key] = name
        return self._micro_catchment_cache[cache_key]

    def _hotspot_name(self, location):
        """Hotspot for the row's location.

        A hotspot links to specific villages (``location.HotspotVillage``), so unlike
        the micro-catchment lookup there's no ancestor tier to fall back through —
        just resolve the village-level ancestor's hotspot link, if any.
        """
        village_location = self._location_ancestor(location, "V")
        if village_location is None:
            return None

        related_manager = getattr(village_location, "hotspot_links", None)
        location_id = getattr(village_location, "id", None)
        if related_manager is None or location_id is None:
            return None
        if location_id not in self._hotspot_cache:
            link = related_manager.filter(
                validity_to__isnull=True,
                hotspot__validity_to__isnull=True,
            ).select_related("hotspot").first()
            hotspot = link.hotspot if link else None
            name = None
            if hotspot is not None:
                name = hotspot.name or hotspot.code
            self._hotspot_cache[location_id] = name
        return self._hotspot_cache[location_id]

    def _member_name(self, individual):
        if not individual:
            return None
        first_name = getattr(individual, "first_name", "") or ""
        last_name = getattr(individual, "last_name", "") or ""
        return f"{first_name} {last_name}".strip()

    def _national_id(self, individual):
        if not individual:
            return None
        return (getattr(individual, "json_ext", None) or {}).get("national_id")

    def _marital_status(self, individual):
        if not individual:
            return None
        return (getattr(individual, "json_ext", None) or {}).get("marital_status")

    def _disability(self, individual):
        if not individual:
            return None
        return (getattr(individual, "json_ext", None) or {}).get("disability")

    def _has_business(self, individual):
        if not individual:
            return None
        value = (getattr(individual, "json_ext", None) or {}).get("has_business")
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, str):
            normalized = value.strip().upper()
            if normalized == "YES":
                return "Yes"
            if normalized == "NO":
                return "No"
        return None

    def _business_type(self, individual):
        if not individual:
            return None
        return (getattr(individual, "json_ext", None) or {}).get("business_type")

    def _business_period(self, individual):
        if not individual:
            return None
        return (getattr(individual, "json_ext", None) or {}).get("business_period")

    def _relationship(self, role):
        if role is None:
            return None
        return str(role).strip() or None

    def _is_head(self, member):
        return str(member.role or "").upper() == "HEAD"

    def _project_name(self, project):
        return getattr(project, "name", None)

    def _project_id(self, project):
        return str(getattr(project, "id", "") or getattr(project, "uuid", "") or "")

    def _project_labels(self):
        name_counts = {}
        for project in self.projects:
            project_name = self._project_name(project)
            if project_name:
                name_counts[project_name] = name_counts.get(project_name, 0) + 1

        labels = {}
        for project in self.projects:
            project_name = self._project_name(project)
            project_id = self._project_id(project)
            if project_name and name_counts.get(project_name, 0) > 1 and project_id:
                labels[id(project)] = f"{project_name} ({project_id})"
            else:
                labels[id(project)] = project_name
        return labels
