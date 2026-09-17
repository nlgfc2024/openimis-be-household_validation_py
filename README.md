# openIMIS Backend household_validation Reference Module

`openimis-be-household_validation` is an openIMIS backend module scaffold for household validation features.

## Installation

For local development, place this repository next to `openimis-be_py`, then register it in `openimis-be_py/openimis.json`:

```json
{
  "name": "household_validation",
  "pip": "-e /home/yutaka/MSR_2026/June_2026/coremisalpha/openimis-be-household_validation_py"
}
```

Install or refresh backend module requirements from `openimis-be_py` as usual.

## Module Contents

The module provides the backend workflow for household validation:

- Django app package: `household_validation`
- Required openIMIS URL configuration: `household_validation/urls.py`
- Python package metadata: `setup.py`
- License and manifest files
- Module configuration and permission constants in `household_validation/apps.py`
- Batch tracking models in `household_validation/models.py`
- Admin registration for validation batches and batch rows in `household_validation/admin.py`
- Migrations for household validation rights, batch tracking tables, and district validation role-right assignment
- Eligible household selection and quota logic in `household_validation/selection.py`
- Project lookup support in `household_validation/project_lookup.py`
- Excel validation list export in `household_validation/excel.py`
- Excel upload parsing and validation helpers in `household_validation/upload.py`
- Service layer for selection, summary, preview, project lookup, upload/apply, primary-worker update, and error report generation in `household_validation/services.py`
- GraphQL query and mutation surface in `household_validation/schema.py`, `household_validation/gql_queries.py`, and `household_validation/gql_mutations.py`
- GraphQL permission helper in `household_validation/gql_permissions.py`
- Focused backend tests in `household_validation/tests.py`

The implemented MVP generates Excel validation workbooks, parses uploaded validation workbooks, stores household validation metadata on `Group.Json_ext`, stores primary-worker flags on `GroupIndividual.Json_ext`, tracks batch/row outcomes, exposes batch history and error reports through GraphQL, and assigns the required household validation rights to configured administrator and district roles.

The integration extension also implements the backend surface required by the validation-list frontend:

- Summary statistics for the validation cards.
- Preview rows for the selected household/member list.
- Shared selection behavior for preview and Excel export (`generateHouseholdValidationList`'s response embeds the same summary counts, so no separate summary query is needed).
- Region, district, TA/municipality, GVH, village, hotspot, and micro-catchment filter support.
- Quota-based main-list selection plus a reserve/waiting list — see "Selection Algorithm" below.

Enrollment remains a reference workflow only. This module does not call enrollment mutations and does not create `GroupBeneficiaryProjectEnrollment` records.

## Selection Algorithm

`household_validation/selection.py::select_households` is the single implementation behind `generateHouseholdValidationList`, `householdValidationPreview`, and the Excel export, so all three always describe the same selection. It runs in this order:

1. **Sort.** Eligible households (at least one fit-for-work member; not excluded by `excludeVerifiedAfter`) are sorted by `household_wealth_quintile` ascending — `Poorest` first, `Richest` last. This quintile is the available proxy for PMT score (there is no separate numeric PMT field on the household); households within the same quintile are ordered by code/id.
2. **Categorize.** Each household is tagged with exactly one category: `FEMALE_HEADED` (head is female), `YOUTH` (no female head, but at least one eligible member aged 18-35), or `OTHER` (neither).
3. **Allocate the target between villages.** For micro-catchment and hotspot requests with an explicit `targetCount`, eligible households are grouped by village and the target is distributed proportionally using the largest-remainder method. When the target is at least the number of villages containing eligible households, every such village receives at least one place. Rounding always preserves the exact overall target. Requests outside micro-catchment/hotspot selection retain the original catchment-wide pool behavior.
4. **Split each village allocation into category quotas.** Each village's requested main-list size is split into three quotas by percentage: **40% female-headed, 40% youth-headed, 20% other**, by default. Only the female-headed and youth-headed percentages are configured values (see below) — there is no separate "other" percentage setting anywhere. The "other" quota is *always computed live* as `100% - femaleHeadedPercentage - youthPercentage`. If the two configured percentages together exceed 100%, they're scaled down proportionally so their sum is exactly 100% and "other" is 0%.
5. **Fill each quota from its village's PMT-sorted pools**, taking the female-headed pool first, then youth, then other. Because the three pools are disjoint and each quota is filled independently, this order only affects row order in the exported list, not which households are ultimately selected.
6. **Backfill any shortfall.** If a category's pool cannot fill its quota, the gap is filled from the remaining eligible households in the same village, still in PMT order. Proportional allocation is capacity-aware, so the combined main list reaches `targetCount` whenever the selected area contains enough eligible households.
7. **Build the reserve/waiting list.** Reserve places default to **20%** of the main-list target and are distributed proportionally across villages from households not selected for the main list. Main and reserve households cannot overlap.

None of the three percentages are GraphQL arguments on `generateHouseholdValidationList` — they're read from `ModuleConfiguration` for the `household_validation` module (see the "Permissions" section below), so they can be retuned without a code deploy.

## Permissions

The module defines these household validation rights:

- `958001`: query/export validation lists
- `958002`: upload/apply validation lists
- `958003`: query validation upload history
- `958004`: download validation upload error reports

The initial rights migration assigns these rights to the IMIS Administrator system role (`is_system = 64`) when that role exists. The district validation role migration creates or reuses the active `District Administrator`, `District Program Manager`, and `District User` roles as non-system deployment roles and assigns them household validation rights plus group search/update rights.

The module configuration exposes these GraphQL permission keys:

- `gql_query_household_validation_rule_perms`
- `gql_mutation_generate_household_validation_list_perms`
- `gql_mutation_upload_household_validation_list_perms`
- `gql_query_household_validation_history_perms`
- `gql_query_household_validation_error_report_perms`

It also exposes the selection quota percentages used by the algorithm described in "Selection Algorithm" above (these are no longer accepted as GraphQL arguments; update `ModuleConfiguration` for the `household_validation` module to change them):

- `female_headed_percentage` (default `40`)
- `youth_percentage` (default `40`)
- `reserve_percentage` (default `20`, applied to the main-list size to size the reserve/waiting list)

There is intentionally no `other_percentage` key — the "other" quota is always derived as `100% - femaleHeadedPercentage - youthPercentage`, so it stays correct however the two configured values are changed.

## GraphQL Backend Testing

Test the backend through the GraphQL fields exposed in `household_validation/schema.py`. The frontend should use these same operations.

Available GraphQL fields:

- `householdValidationProjects`
- `householdValidationPreview`
- `householdValidationBatches`
- `householdValidationBatchRows`
- `householdValidationBatchErrorReport`
- `generateHouseholdValidationList`
- `uploadHouseholdValidationList`

Required rights:

- `958001`: project lookup, preview, and validation list generation
- `958002`: validation list upload/apply
- `958003`: batch history and batch row queries
- `958004`: validation upload error report download

Preview and export should receive the same filter payload so they describe the same selected households:

- `regionId` or `regionCode`
- `districtId` or `districtCode`
- `taId` or `taCode` or `taCodes`
- `gvhCodes`
- `villageId` or `villageCode` or `villageCodes`
- `hotspotId` or `hotspotCode`
- `catchmentId` or `catchmentCode` (micro-catchment)
- `excludeVerifiedAfter`
- `targetCount`

`ta` maps to the municipality/TA level in the location hierarchy. `hotspotId`/`hotspotCode` resolve to a `location.Hotspot` and scope selection to its linked villages; `catchmentId`/`catchmentCode` resolve to a `location.MicroCatchment` and scope selection to its linked TAs and GVHs. Only one location filter tier applies per request — the most specific one supplied wins, in this order: village > GVH > hotspot > TA > micro-catchment > district > region. Selection quota percentages (female-headed, youth, reserve) are no longer request arguments — they come from `ModuleConfiguration` (see above).

Project dropdown query:

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

Generate a validation workbook. The response embeds the same summary statistics that used to require a separate `householdValidationSummary` query, computed from the same selection run as the exported workbook:

```graphql
mutation {
  generateHouseholdValidationList(
    districtCode: "DISTRICT_CODE"
    excludeVerifiedAfter: "2026-07-01"
    targetCount: 100
  ) {
    batchId
    fileName
    fileBase64
    totalHouseholds
    totalIndividuals
    eligibleHouseholds
    eligibleIndividuals
    selectedHouseholds
    selectedIndividuals
    selectedFemaleHeadedHouseholds
    selectedYouthHouseholds
    selectedOtherHouseholds
    reserveHouseholds
    generatedAt
  }
}
```

The response `fileBase64` is the Excel workbook content. The frontend should decode it for download.

These fields map to the validation-list summary cards:

- `totalHouseholds`: Total Households In System
- `totalIndividuals`: Total Individuals In System
- `selectedHouseholds`: Selected Households
- `selectedIndividuals`: Selected Individuals
- `selectedFemaleHeadedHouseholds`: Female-Headed Households Selected
- `selectedYouthHouseholds`: Youth-Headed Households Selected
- `reserveHouseholds`: Reserve Households Selected

Query preview rows for the preview dialog:

```graphql
query {
  householdValidationPreview(
    first: 20
    offset: 0
    regionCode: "REGION_CODE"
    districtCode: "DISTRICT_CODE"
    taCode: "TA_CODE"
    villageCode: "VILLAGE_CODE"
    excludeVerifiedAfter: "2026-07-01"
    targetCount: 100
  ) {
    totalCount
    pageInfo {
      hasNextPage
      hasPreviousPage
      startCursor
      endCursor
    }
    edges {
      node {
        rowType
        category
        groupUuid
        groupCode
        headName
        individualUuid
        individualFirstName
        individualLastName
        individualDob
        individualAge
        individualGender
        fitForWork
        currentRecipientType
        region
        district
        municipality
        village
        wealthQuintile
        lastVerifiedDate
        validationStatus
        prospectiveProjects
      }
    }
  }
}
```

The preview is a selected household/member preview for the frontend modal. Excel export remains the authoritative field-officer workbook. If the frontend must show every workbook column exactly before download, extend the preview row type with the remaining workbook-edit columns.

Query generated/uploaded batches:

```graphql
query {
  householdValidationBatches {
    count
    batches {
      id
      sourceFileName
      status
      districtId
      taId
      villageId
      targetCount
      generatedAt
      uploadedAt
      errorSummary
      jsonExt
    }
  }
}
```

Upload an edited workbook. Use `dryRun: true` first, then run again with `dryRun: false` if there are no blocking errors:

```graphql
mutation UploadValidationList($fileBase64: String!) {
  uploadHouseholdValidationList(
    fileBase64: $fileBase64
    dryRun: true
    sourceFileName: "validation_list.xlsx"
  ) {
    batchId
    uploadAttemptId
    rowsRead
    householdsVerified
    participantsVerified
    participantsNotVerified
    participantsRejected
    householdsNotVerified
    participantUpdates
    errors
    errorMessages
  }
}
```

Query batch row results:

```graphql
query {
  householdValidationBatchRows(batchId: "BATCH_UUID_HERE") {
    count
    rows {
      id
      batchId
      uploadAttemptId
      groupId
      groupIndividualId
      individualId
      projectId
      rowNumber
      verified
      validationDate
      status
      errorMessage
      rawRow
      jsonExt
    }
  }
}
```

Each non-dry-run upload receives a unique `uploadAttemptId`. Use it with the
workbook `batchId` to download rejected households from that upload only:

```graphql
query {
  householdValidationRejectedBatchRows(
    batchId: "BATCH_UUID_HERE"
    uploadAttemptId: "UPLOAD_ATTEMPT_UUID_HERE"
  ) {
    fileName
    fileBase64
  }
}
```

Download upload errors as a base64 CSV:

```graphql
query {
  householdValidationBatchErrorReport(batchId: "BATCH_UUID_HERE") {
    batchId
    fileName
    fileBase64
    errorCount
  }
}
```

Expected upload behavior:

- Generated workbooks hide internal `batch_id`, `group_uuid`, `member_uuid`, `row_type`, and `project_id` columns. Their locked values remain in the file for upload matching and household formulas. Form Number and National ID remain visible. Do not delete the hidden columns.

- `rowsRead` counts every non-empty participant row encountered in the workbook, including rows that later fail validation.
- Generated workbooks contain protected `participant_status` and `household_status` formulas that recalculate as officers fill the sheet. Upload recomputes these statuses from inputs, ignoring uploaded status values. For each member: Primary Worker `Yes` with Business `Yes` or `No` is `VERIFIED`; Primary Worker `Yes` with Business blank is `NOT_VERIFIED`; Primary Worker `No`/blank with Business `Yes` is `REJECTED`. Other combinations are `NOT_VERIFIED`.
- A household is `REJECTED` if any member is rejected or more than one primary worker is selected. Otherwise it is `VERIFIED` if it has a verified primary worker, and `NOT_VERIFIED` otherwise. A member's own status remains independent of other members' results. Missing business columns in older files count as blank for verification, while preserving stored business data.
- Primary Worker dropdowns allow at most one `YES` per household, matched by hidden `group_uuid` across the whole sheet. When another member is `YES`, only `NO` is available and a Stop error blocks typing a second `YES`. Clear the current selection or change it to `NO` before selecting a different worker. Conflicting `YES` cells are highlighted red if pasted data bypasses validation; upload retains its existing household rejection check.
- Generated workbooks always leave `primary_worker`, Does member has a business, Type of Business, Business Period, and `validation_notes` blank for fresh field collection, even when saved answers exist. Export does not change those saved answers or suggest a Primary Worker from `recipient_type`.
- The Business cell displays input guidance to select Primary Worker first. Its Yes/No dropdown becomes available only after Primary Worker is answered `YES` or `NO` on that row. A Stop error rejects manually entered business answers while Primary Worker is blank. The guidance appears whenever the Business cell is selected; the blocking error appears on invalid entry. Generate a new workbook to receive these validation rules.
- Participant rows are grouped visually by alternating green and light-green fills; every row belonging to the same household uses the same fill.
- `participantsVerified`, `participantsNotVerified`, and `participantsRejected` count unique members by their calculated status. Existing multiple-primary-worker rejections count the conflicting primary workers as rejected and block changes for that household. Invalid households are excluded from verification counts.
- `householdsRejected` counts both business-rule and multiple-primary-worker rejections. `householdsWithMultiplePrimaryWorkers` retains its narrower meaning. Clients must request `householdsRejected` to display the full rejected-household count.
- For business-rule rejections, upload saves the collected member fields and member/household `validation_status = REJECTED` as applicable, and records rejected household rows in the audit and downloadable rejection report. These decisions do not count as upload errors. Dry runs report the same decisions without saving.
- Rejected rows use a dedicated `REJECTED` audit status and are excluded from system error reports.
- For a verified household, upload atomically clears every existing Primary Worker assignment before assigning the selected participant. This includes active household members not present in the workbook and does not change `recipient_type`.
- Not-verified and rejected households preserve existing Primary Worker assignments. With more than one `YES`, the household is rejected and no member updates are made.
- Primary Worker changes are not included in `participantUpdates`, which counts National ID changes only.
- Upload saves the business fields to each individual's `json_ext`: `business_experience` (`Yes`/`No`), `type_of_business`, and `business_period` (numeric years, 0–100). Types must match the configured business options. Business Period is mandatory when Business is Yes (including when the period column is absent). Missing periods are highlighted in the generated workbook, have input guidance and a Stop validation error, and prevent household updates on upload. Other blank business cells and business columns absent from older exports preserve stored values; selecting `No` clears the stored type and period. Business details require `Yes` in the same row.
- Each row's `validation_notes` is saved on its individual, while the selected Primary Worker's notes (or the first row when none is selected) remain the household's validation notes. Blank notes clear the previous notes.
- Header matching ignores case, extra whitespace, line breaks, and underscores. Legacy business headers `business_experience`, `has_business`, `type_of_business`, and `business_period` are accepted. Duplicate headers are rejected.
- Before applying any rows, upload derives verification from the Primary Worker and business answers in valid workbook rows. Stored flags do not turn blank cells into implicit selections.
- A parser or structural error on any identifiable household row prevents updates for the entire household.
- An unchanged re-upload preserves the existing `last_verified_date`; the date advances on the first validation or when status, Primary Worker, National ID, business information, project, or validation notes change.
- Project selection is stored as validation intent/prospect metadata only.
- Upload does not create `GroupBeneficiaryProjectEnrollment` records.
- Protected workbook fields such as household/member identifiers, location labels, member details, fit-for-work, and head are checked for tampering.

## Verified Extended Requirements

Implemented and verified in the integration extension:

- `generateHouseholdValidationList` returns card statistics for the validation-list UI inline on the same response as the exported workbook, so no separate summary query is needed.
- `totalHouseholds` and `totalIndividuals` cover the complete selected micro-catchment using its most specific active mapping (hotspot villages, then GVHs, then TAs); optional location filters narrow selection only and do not change these headline totals.
- `householdValidationPreview` returns paged preview rows for selected household/member rows.
- `generateHouseholdValidationList` and `householdValidationPreview` accept the same region/location filters.
- Generation and preview share the same eligible-household selection service.
- Region filtering is supported in addition to district/TA/village filtering.
- Female-headed, youth, and reserve quota percentages are configured via `ModuleConfiguration` (default 40/40/20 main quotas, 20% reserve) rather than passed as request arguments.
- Percentage over-allocation is normalized so selection cannot exceed the requested target.
- Households are sorted poorest-first by wealth quintile (PMT proxy) before quotas are applied, and the reserve/waiting list continues in that same order past the main list rather than being re-sorted.
- Hotspot and micro-catchment filters (`hotspotId`/`hotspotCode`, `catchmentId`/`catchmentCode`) scope selection to a `location.Hotspot`'s villages or a `location.MicroCatchment`'s TAs/GVHs.
- Upload and export behavior still do not create enrollment records.

Local verification commands:

```bash
python3 -m compileall -q openimis-be-household_validation_py/household_validation
```

```bash
cd openimis-be_py/openIMIS
../.venv/bin/python manage.py test household_validation
```

Latest local result:

```text
Found 78 test(s).
Ran 78 tests.
OK
```

The local openIMIS test runner logs database/configuration warnings while module configuration falls back to defaults, but the household validation test suite passes.
