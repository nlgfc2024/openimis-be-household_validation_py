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
- [UMEP Individual Validation](#umep-individual-validation)
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

The configuration remains backend-owned. The permission-checked
`validationProgrammes` query exposes programme codes, resolved subjects,
effective export columns and the individual decision contract to the frontend.

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

## UMEP Individual Validation

Add `"UMEP": {"subject": "INDIVIDUAL", "data_source": "ubr"}` to the
existing `program_eligibility_rules`. Omitted subjects remain `HOUSEHOLD`; other
subject values are rejected. Individual rules accept `subject` and an optional
non-empty `data_source` only. Set the source to the UMEP Phase's
`json_ext.data_source`; when omitted, both validation and enrolment default to
`ubr`, including legacy missing, null and empty source tags. Surrounding whitespace
in a configured individual source is stripped when read; case is preserved and
other source keys match exactly. Candidates are accessible, non-deleted individuals
outside active households, filtered through `Individual.location` and ordered by primary key.
Targets count people.

The source-rule names deliberately retain different matching contracts:
household `requires_data_source` is an optional member eligibility requirement,
with case-insensitive matching and no implicit source default. Individual
`data_source` scopes candidates through enrolment's `data_source_q` semantics,
including the UBR default and legacy-tag handling above. Keeping both contracts
preserves existing household deployments while aligning individual validation
with the UMEP enrolment population.

Deployment configuration is maintained in
[nlgfc2024/coremis-configs](https://github.com/nlgfc2024/coremis-configs/blob/main/household_validation/umep.household_validation.config.json).
Its per-instance file contains the complete UMEP rules, programme-specific columns
and option overrides. Saved top-level blocks **replace** defaults, without deep
merge. Use the instance file as a whole; retain other programme entries explicitly
if configuring a shared deployment. The default module configuration remains
household-only. Configuration rules are validated on save through the core module
configuration registry and again at startup.

The individual decision contract is **VERIFIED / NOT_VERIFIED only**. Leading and
trailing whitespace is trimmed and case normalised. Blank/null decisions skip the
whole row without changes; unsupported values, including REJECTED and Yes/No, are
invalid. VERIFIED decisions must satisfy applicable configured required fields.
NOT_VERIFIED decisions allow blank business answers (for example, when a business
does not exist or a person cannot be reached); omitted answers preserve stored
values, subject to the configured dependency rules. Supplied answers for either
decision must satisfy dependencies, types, options and numeric bounds. Household
Primary Worker/status rules are unchanged.

The read-only queries below reuse right `958001`. They expose programme metadata
and current candidates; candidate results are not a saved workbook list.

```graphql
query {
  validationProgrammes {
    benefitPlanCode subject dataSource columns decisionValues blankDecisionBehavior
  }
  individualValidationCandidates(
    benefitPlanCode: "UMEP", districtCode: "CITY_CODE",
    excludeValidated: true, targetCount: 100, first: 20, offset: 0
  ) {
    eligibleIndividuals selectedIndividuals hasNextPage
    items { id firstName lastName dob locationId }
  }
}
```

`benefitPlanCode` is required for individual candidates. `first` defaults to 20
and accepts 0–100; `offset` is non-negative. Geographic filters use each person's
own location, with no household fallback. `targetCount: 0` selects none; omitting
the target selects all eligible candidates before pagination. Programme discovery
exposes configured entries only. Existing household queries and mutations retain
their household behavior.

`excludeValidated: true` excludes people whose individual `validation_status` is
`VERIFIED` or `NOT_VERIFIED`, before counting, targeting and pagination. Missing,
null, blank and unsupported statuses remain candidates. The default is `false`
so existing calls can still select people for revalidation. This is an explicit
status filter, not a date cutoff. Live candidate counts are computed per request;
the query is not a persisted list or cached snapshot.

### Saved individual lists and workbooks

`generateIndividualValidationList` uses the individual programme's selection
service and creates an existing validation batch. `batch.json_ext` stores the
programme, effective rule/source, resolved editable columns (including option
overrides), protected display-field definitions, filters and issued counts.
Ordered identities and descriptive snapshots are stored in existing
`HouseholdValidationBatchRow` records, marked `INDIVIDUAL_SELECTION` with a null
`upload_attempt_id`; later upload attempts use different rows. Generation writes
selection rows and their history in chunks of 500 within the same transaction.
Preview and re-export read the saved values; later registry or configuration
edits do not change those values. Decision cells start blank, including on
revalidation lists.

```graphql
mutation {
  generateIndividualValidationList(
    benefitPlanCode: "UMEP", districtCode: "CITY_CODE",
    excludeValidated: true, targetCount: 100
  ) { batchId fileName fileBase64 eligibleIndividuals selectedIndividuals generatedAt }
}
```

```graphql
query SavedIndividualList($id: UUID!) {
  individualValidationList(batchId: $id, first: 20, offset: 0) {
    batchId columns displayFields items eligibleIndividuals selectedIndividuals hasNextPage
    availableIndividuals deletedIndividuals missingIndividuals inaccessibleIndividuals
  }
  individualValidationListExport(batchId: $id) {
    batchId fileName fileBase64 eligibleIndividuals selectedIndividuals
    availableIndividuals deletedIndividuals missingIndividuals inaccessibleIndividuals
  }
}
```

`items` contains saved row dictionaries for individuals who are currently active
and within the caller's access. Preview uses SQL `LIMIT`/`OFFSET` over those saved
rows rather than loading every snapshot or constructing an in-memory ID list.
It does not rerun candidate selection. `eligibleIndividuals` and
`selectedIndividuals` remain the issued counts. Current availability is reported
separately: `availableIndividuals`, soft-deleted `deletedIndividuals`, physically
missing `missingIndividuals`, and active `inaccessibleIndividuals` outside the
caller's scope. `hasNextPage` and offsets refer to the available rows. Re-export
includes the same available subset in the saved order, with omission counts in
both the GraphQL response and hidden workbook metadata. A deletion or scope
change to one person does not block the remaining list.

Generation requires `958001`. Saved preview and re-export require the configured
history right (`958003`), while parsing requires the upload right (`958002`).
A reader must have at least one currently accessible selected person, or be the
batch creator or an administrator. A creator/admin with no accessible people may
read omission counts and export an empty workbook, but cannot retrieve personal
snapshots outside their current scope. Historical household batches and workbook
APIs retain their existing behaviour.

There is no confirmed production UMEP list-size estimate. The paging regression
covers 2,004 selected individuals. With no `targetCount`, generation still selects
all eligible individuals; use geographic filters or a target for operational
workbook sizes. Export and parsing necessarily process the whole workbook in
memory; this change bounds preview snapshot loading, not workbook memory use.

Protected display columns are configured per programme through
`individual_workbook_fields`. Each definition supplies `column_name` and exactly
one of `json_ext_key` or `location_type` (`R`, `D`, `W` or `V`). Labels and identity
source keys are deployment choices; the backend supplies no UMEP-specific labels.
UMEP maps `R` to City, `D` (TA) to Constituency, `W` (GVH) to Ward and `V`
to Neighbourhood. The individual validation workbook intentionally omits
Constituency: its configured geography columns are City, Ward and Neighbourhood.
This workbook subset does not remove `D` from the location hierarchy. Its
workbook display configuration is:

```json
{
  "individual_workbook_fields": {
    "UMEP": [
      {"column_name": "National ID", "json_ext_key": "national_id"},
      {"column_name": "Gender", "json_ext_key": "gender"},
      {"column_name": "City", "location_type": "R"},
      {"column_name": "Ward", "location_type": "W"},
      {"column_name": "Neighbourhood", "location_type": "V"}
    ]
  }
}
```

The source keys and location types must match that deployment's registry. An
absent programme entry means no extra protected display columns. These
read-only fields are saved with the issued list and checked during parsing;
editable business fields continue to use `program_specific_export_columns` and
`export_column_options_overrides`. Duplicate or reserved display names, invalid
sources and collisions with editable columns prevent list generation.

The individual workbook contains protected batch/individual identity and
descriptive fields, plus editable decisions, notes and configured programme
columns. It does not require a household, household head or Primary Worker.
`IndividualValidationListService(user).parse(bytes_or_file)` requires the upload
right (`958002`) and an existing accessible individual batch. It returns global
file errors and per-row errors/raw values, decisions, notes and parsed JSON-field
updates; it does **not** save upload attempts or apply updates. `valid_rows` is
empty on global file errors and otherwise excludes erroneous rows; a valid row
with `decision is None` must be skipped without applying any fields.

Parsing uses only the batch's saved columns and identity set, checks protected
values, rejects unknown/mixed batches, foreign/duplicate IDs, unexpected/missing
headers and formulas, and enforces applicable types, options, numeric bounds,
dependencies and required fields for VERIFIED decisions. NOT_VERIFIED decisions
may omit business answers. Excel permits blank business cells; parsing enforces
the decision-specific requiredness. A blank decision skips programme-field
validation and updates; a submitted invalid row has no parsed updates to apply.

Current deletion and access checks are per submitted row: a deleted, missing or
inaccessible individual receives a row error with no parsed updates, while other
valid rows remain usable. A removed individual's saved protected fields are
never returned through preview or re-export. Parsing itself creates no upload
attempts or individual updates.

### Applying individual decisions

`uploadIndividualValidationList` requires `958002` and a known generated individual
batch. It uses the saved rule, columns and selected identities, then rechecks each
person's current access, deletion state, source, active-household membership and
saved geography. It does not reapply `excludeValidated` during upload, so retries
and corrections remain possible after an accepted decision.

```graphql
mutation ValidateIndividuals($file: String!) {
  uploadIndividualValidationList(fileBase64: $file, dryRun: true, sourceFileName: "checked.xlsx") {
    batchId dryRun status rowsRead rowsApplied rowsSkipped errors errorMessages
    wouldUpdate individualsUpdated uploadAttemptId
  }
}
```

Dry run performs parsing and current eligibility checks without changing an
individual, batch, row or history record. `wouldUpdate` predicts changed individuals;
`individualsUpdated` is zero in a dry run and counts actual writes after commit.
Submit again with `dryRun: false` to commit; current state is checked again.

Each committed upload uses one transaction and a new `uploadAttemptId`. These
commits lock their batch row before reading its attempt summaries and current
individual state, so simultaneous uploads for the same batch serialize rather
than overwrite each other's attempt metadata. Dry runs do not acquire this
write lock or persist changes; committing still rechecks current state.

Valid submitted decisions are recorded as APPLIED even if the individual already has
the same values; blank decisions are SKIPPED. Expected row errors are recorded
as ERROR while other valid rows apply, producing PARTIAL_SUCCESS. All-error files
are FAILED; a file with no errors is PROCESSED. Unknown batches are rejected and
never created by upload. Unexpected write failures roll back the entire attempt.

Decisions update `Individual.json_ext.validation_status` directly, together with
valid configured fields and supplied notes. Unrelated JSON, household status,
membership and primary workers are preserved. An identical retry creates an audit
attempt and result rows without an unnecessary individual save/history entry.
Corrections create new attempts without replacing earlier decisions. Existing
Individual history and validation batch rows supply the audit trail.

```graphql
query IndividualValidationHistory($batch: UUID!, $attempt: UUID!) {
  individualValidationAttempts(batchId: $batch) { count attempts }
  individualValidationAttemptRows(batchId: $batch, uploadAttemptId: $attempt, first: 20, offset: 0) {
    count items hasNextPage
  }
  individualValidationErrorReport(batchId: $batch, uploadAttemptId: $attempt) {
    fileName fileBase64 errorCount
  }
}
```

Attempt history/rows require `958003`; the attempt-specific CSV error report requires
`958004`. No new rights are introduced. Batch creators, administrators or users
with current access to selected individuals may access attempt metadata, subject
to those rights. Personal row data is returned only for individuals currently
visible to the reader; other rows have `redacted: true`, no individual ID and empty
raw/JSON values. CSV reports use the saved programme columns and the same redaction.
Deleted or inaccessible people produce individual row errors rather than blocking
processing of the rest of an authorized upload.

Attempt summaries retain counters, outcome, actor, filename and timing metadata;
new summaries do not duplicate the full `error_messages` list. The immediate
upload/dry-run response still includes those messages. Detailed committed errors
remain on their attempt rows and in the CSV report, with a compact current-attempt
error summary on the batch.

Attempt rows use SQL pagination: `first` defaults to 20 and accepts integers from
0 to 100; `offset` defaults to 0 and must be a non-negative integer. Invalid
ranges are rejected. `count` is the total number of rows in that
attempt, including redacted rows; `items` is the requested page and `hasNextPage`
indicates whether more rows remain. Clients that previously assumed all rows were
returned must paginate. The attempt-specific CSV always includes every error row,
independent of the result page size, and applies the same current-access redaction.

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
