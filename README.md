# openIMIS Backend Household Validation Reference Module

`openimis-be-household_validation` provides household validation workflows for openIMIS.

The module enables:
 
- Household selection using configurable eligibility rules
- Validation workbook generation and preview
- Excel-based field validation
- Validation workbook upload and processing
- Batch tracking and audit history
- Program-specific selection and export rules
- Primary worker assignment management
- Error reporting and upload validation
- GraphQL APIs for frontend integration

---

## Table of Contents

- [Installation](#installation)
- [Overview](#overview)
- [Architecture](#architecture)
- [Permissions](#permissions)
- [Selection Logic](#selection-logic)
- [Program-Based Selection](#program-based-selection)
- [Configuration](#configuration)
- [Program-Specific Export Columns](#program-specific-export-columns)
- [GraphQL API](#graphql-api)
- [Excel Export and Upload](#excel-export-and-upload)
- [Testing](#testing)

---

## Installation

For local development, place this repository alongside `openimis-be_py` and register it in `openimis.json`:

```json
{
  "name": "household_validation",
  "pip": "-e /path/to/openimis-be-household_validation_py"
}
```

Install or refresh backend requirements from `openimis-be_py` as usual.

---

## Overview

The household validation module enables field officers to validate households and members through an Excel-based workflow.

### Workflow

```text
Generate Validation List
          │
          ▼
Household Selection Engine
          │
          ▼
Excel Workbook Export
          │
          ▼
Field Validation
          │
          ▼
Workbook Upload
          │
          ▼
Validation Processing
          │
          ▼
Batch History & Error Reporting
```

---

## Architecture

### Core Components

| Component | Purpose |
|------------|----------|
| Selection Engine | Household eligibility and selection |
| Excel Service | Workbook generation and upload processing |
| GraphQL API | Preview, export, upload, and reporting endpoints |
| Batch Tracking | Export and upload history |
| Permission Layer | Validation-specific access control |
| Configuration Layer | Program rules and export column definitions |

### Package Structure

```text
household_validation/
├── admin.py
├── apps.py
├── excel.py
├── gql_mutations.py
├── gql_permissions.py
├── gql_queries.py
├── models.py
├── project_lookup.py
├── schema.py
├── selection.py
├── services.py
├── upload.py
└── tests.py
```

---

## Permissions

### Rights

| Right ID | Description |
|-----------|-------------|
| `958001` | Query and export validation lists |
| `958002` | Upload and apply validation lists |
| `958003` | Query validation upload history |
| `958004` | Download validation upload error reports |

### GraphQL Permission Keys

```text
gql_query_household_validation_rule_perms
gql_mutation_generate_household_validation_list_perms
gql_mutation_upload_household_validation_list_perms
gql_query_household_validation_history_perms
gql_query_household_validation_error_report_perms
```

### Default Role Assignments

The module automatically assigns household validation rights to:

- IMIS Administrator
- District Administrator
- District Program Manager
- District User

---

## Selection Logic

The module supports two selection modes:

1. Quota-Based Selection (PWP)
2. Program-Based Selection (e.g. RMEP, UPG)

The selection algorithm is implemented centrally in:

```python
household_validation.selection.select_households
```

and is used by:

- `generateHouseholdValidationList`
- `householdValidationPreview`
- Excel export generation

This guarantees that preview and export always return the same selection.

---

### Rule Resolution

Selection rules are resolved from:

```text
program_eligibility_rules
```

using the provided:

```graphql
benefitPlanCode
```

Resolution order:

1. Use the matching program configuration.
2. Use `PWP` when `benefitPlanCode` is omitted.
3. Raise a validation error if the supplied code is unknown.

---

### Quota-Based Selection (PWP)

Used when:

- `benefitPlanCode` is omitted
- `benefitPlanCode = PWP`

#### Process

1. Identify eligible households.
2. Sort households by wealth quintile (poorest first).
3. Categorize households as:
   - Female-headed
   - Youth
   - Other
4. Allocate targets proportionally across villages.
5. Apply demographic quotas.
6. Backfill quota shortfalls.
7. Build a reserve list.

#### Default Quotas

| Category | Percentage |
|-----------|-----------|
| Female-headed | 40% |
| Youth | 40% |
| Other | 20% |

#### Reserve List

Default reserve size:

```text
20% of targetCount
```

Reserve households are selected from households not included in the main list.

---

## Program-Based Selection

Program-based selection is used when the matching rule does not define a `selection_strategy`.

Examples:

```text
RMEP
UPG
```

### Characteristics

#### Program-Specific Eligibility

Eligibility is determined using configurable rules such as:

- Data source requirements
- Recipient type requirements
- Age restrictions
- Required member flags

#### No Quotas

The following are not applied:

- Wealth ranking
- PMT proxy calculations
- Female-headed quotas
- Youth quotas
- Reserve lists

#### Priority Ordering

A configured `priority_flag` can prioritize otherwise eligible households.

#### Selection Logic

The first `targetCount` eligible households are selected.

---

## Configuration

The module exposes three primary configuration sections:

```text
program_eligibility_rules
program_specific_export_columns
export_column_options_overrides
```

These values are stored in the module's `ModuleConfiguration`.

The configuration is backend-only and is not exposed through GraphQL.

---

### Program Eligibility Rules

Defines:

- Eligibility requirements
- Selection strategy
- Priority ordering

#### Default Configuration

```python
"program_eligibility_rules": {
    "PWP": {
        "member_flag": "fit_for_work",
        "selection_strategy": {
            "female_headed_percentage": 40,
            "youth_headed_percentage": 40,
            "reserve_percentage": 20,
        },
    },
}
```

#### Supported Rule Properties

```text
selection_strategy
requires_data_source
requires_recipient_type
member_flag
member_min_age
member_max_age
priority_flag
```

#### Selection Strategy

When present:

```text
Quota-based selection runs
```

When absent:

```text
Program-based selection runs
```

---

### Migrating from Legacy Configuration

The following legacy configuration keys are no longer used:

```text
female_headed_percentage
youth_percentage
reserve_percentage
```

Use:

```python
program_eligibility_rules["PWP"]["selection_strategy"]
```

instead.

Also retired, with no replacement needed (folded into `program_specific_export_columns`):

```text
business_columns_enabled
business_type_options
```

The module logs warnings when retired configuration keys are detected.

---

## Program-Specific Export Columns

Additional Excel columns can be configured per program using:

```text
program_specific_export_columns
```

Example:

```python
{
    "key": "business_type",
    "column_name": "Type of Business",
    "target_individual_json_ext_key": "type_of_business",
    "type": "select",
    "options": ["Crop farming", "Livestock farming", "Grocery shop"],
    "required": True
}
```

### Supported Types

```text
select
number
text
```

### Supported Properties

```text
key
column_name
target_individual_json_ext_key
type
options
required
depends_on
min
max
```

### Dependencies

Example:

```python
"depends_on": {
    "key": "has_business",
    "equals": "Yes"
}
```

The field becomes active only when the dependency condition is satisfied.

---

### Export Column Option Overrides

For simple dropdown customizations:

```json
{
  "export_column_options_overrides": {
    "business_type": [
      "Rice farming",
      "Retail shop",
      "Transport services"
    ]
  }
}
```

This avoids redefining the complete column configuration.

---

## GraphQL API

### Available Queries

```text
householdValidationProjects
householdValidationPreview
householdValidationBatches
householdValidationBatchRows
householdValidationBatchErrorReport
```

### Available Mutations

```text
generateHouseholdValidationList
uploadHouseholdValidationList
```

---

### Project Lookup

```graphql
query {
  householdValidationProjects(locationCode: "DISTRICT_CODE") {
    count
    projects {
      id
      name
      status
      locationId
    }
  }
}
```

---

### Generate Validation Workbook

```graphql
mutation {
  generateHouseholdValidationList(
    districtCode: "DISTRICT_CODE"
    targetCount: 100
  ) {
    batchId
    fileName
    fileBase64
    selectedHouseholds
    selectedIndividuals
    generatedAt
  }
}
```

The returned `fileBase64` contains the Excel workbook content.

---

### Preview Selection

```graphql
query {
  householdValidationPreview(
    districtCode: "DISTRICT_CODE"
    targetCount: 100
  ) {
    totalCount
    edges {
      node {
        groupCode
        headName
        village
        wealthQuintile
      }
    }
  }
}
```

---

### Batch History

```graphql
query {
  householdValidationBatches {
    count
    batches {
      id
      status
      generatedAt
      uploadedAt
    }
  }
}
```

---

### Upload Validation Workbook

```graphql
mutation UploadValidationList($fileBase64: String!) {
  uploadHouseholdValidationList(
    fileBase64: $fileBase64
    dryRun: true
    sourceFileName: "validation_list.xlsx"
  ) {
    batchId
    rowsRead
    errors
  }
}
```

Recommended workflow:

1. Upload with `dryRun: true`
2. Resolve validation errors
3. Upload again with `dryRun: false`

---

## Excel Export and Upload

### Verification Rules

#### Participant Status

| Condition | Status |
|------------|---------|
| Primary Worker answered (`YES` or `NO`) | VERIFIED |
| Primary Worker blank | NOT_VERIFIED |

#### Household Status

| Condition | Status |
|------------|---------|
| Exactly one Primary Worker | VERIFIED |
| No Primary Worker selected | NOT_VERIFIED |
| Multiple Primary Workers selected | REJECTED |

---

### Workbook Integrity Rules

- Hidden identifier columns must not be removed.
- Protected fields must not be modified.
- Duplicate headers are rejected.
- Upload recalculates verification status.
- Protected data is validated for tampering.

---

### Additional Columns Rules

- Additional columns are configurable per program.
- Upload validation enforces configured business rules.
- Invalid business data results in row-level validation errors.

---

### Upload Processing

The upload process:

1. Parses workbook contents.
2. Validates workbook structure.
3. Validates household and participant data.
4. Applies updates.
5. Records batch outcomes.
6. Generates error reports when required.

Primary Worker updates are applied atomically for verified households.

---

## Testing

### Lint

```bash
flake8 --max-line-length=120 openimis-be-household_validation_py/household_validation
```

### Compilation Check

```bash
python3 -m compileall -q openimis-be-household_validation_py/household_validation
```

### Test Suite

```bash
cd openimis-be_py/openIMIS

PYTHONPATH="<path-to-this-checkout>:$PYTHONPATH" \
../.venv/bin/python manage.py test household_validation --keepdb
```

### Latest Result

```text
Found 161 tests.
Ran 161 tests in 0.7s

OK
```

✅ All 161 tests pass.