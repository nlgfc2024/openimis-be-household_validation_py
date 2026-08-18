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
3. **Split `targetCount` into quotas.** The requested main-list size (`targetCount`, or every eligible household if omitted) is split into three quotas by percentage: **40% female-headed, 40% youth-headed, 20% other**, by default. Only the female-headed and youth-headed percentages are configured values (see below) — there is no separate "other" percentage setting anywhere. The "other" quota is *always computed live* as `100% - femaleHeadedPercentage - youthPercentage`, so it stays correct for any configured pair, not just the 40/40 default: e.g. 30/30 leaves 40% for other, 45/45 leaves 10%. If the two configured percentages together exceed 100%, they're scaled down proportionally so their sum is exactly 100% and "other" is 0% — the three quotas can never sum to anything but the full `targetCount`.
4. **Fill each quota from its own PMT-sorted pool**, taking the female-headed pool first, then youth, then other. Because the three pools are disjoint and each quota is filled independently, this order only affects row order in the exported list, not which households are ultimately selected.
5. **Backfill any shortfall.** If a category's pool can't fill its own quota (e.g. too few youth-headed households in the filtered area), the gap is backfilled from whatever eligible households remain, still walked in PMT order — so the main list reaches `targetCount` as long as enough eligible households exist in total, even if the 40/40/20 split isn't hit exactly for that run.
6. **Build the reserve/waiting list.** Once the main list is filled, the reserve list is drawn from the households still left over, continuing in the *same* PMT-sorted order (not a fresh sort) — so the waiting list is a direct continuation of the main list's ranking. Its size defaults to **20%** of the main list size, capped by whatever eligible households remain.

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

- `gql_mutation_female_headed_percentage` (default `40`)
- `gql_mutation_youth_percentage` (default `40`)
- `gql_mutation_reserve_percentage` (default `20`, applied to the main-list size to size the reserve/waiting list)

There is intentionally no `gql_mutation_other_percentage` key — the "other" quota is always derived as `100% - femaleHeadedPercentage - youthPercentage`, so it stays correct however the two configured values are changed.

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
    rowsRead
    householdsVerified
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

- `verified = YES` stores `validation_status = VERIFIED` on `Group.Json_ext`.
- `verified = NO` stores `validation_status = NOT_VERIFIED` on `Group.Json_ext`.
- `primary_worker = YES/NO` stores the worker flag on `GroupIndividual.Json_ext` without changing `recipient_type`.
- Project selection is stored as validation intent/prospect metadata only.
- Upload does not create `GroupBeneficiaryProjectEnrollment` records.
- Protected workbook fields such as household/member identifiers, location labels, member details, fit-for-work, head, and current recipient values are checked for tampering.

## Verified Extended Requirements

Implemented and verified in the integration extension:

- `generateHouseholdValidationList` returns card statistics for the validation-list UI inline on the same response as the exported workbook, so no separate summary query is needed.
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
Found 69 test(s).
Ran 69 tests.
OK
```

The local openIMIS test runner logs database/configuration warnings while module configuration falls back to defaults, but the household validation test suite passes.
