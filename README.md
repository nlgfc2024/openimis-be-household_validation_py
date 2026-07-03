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

The module now provides the backend workflow for household validation:

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
- Service layer for selection, project lookup, upload/apply, participant update, and error report generation in `household_validation/services.py`
- GraphQL query and mutation surface in `household_validation/schema.py`, `household_validation/gql_queries.py`, and `household_validation/gql_mutations.py`
- GraphQL permission helper in `household_validation/gql_permissions.py`
- Focused backend tests in `household_validation/tests.py`

The implemented MVP generates Excel validation workbooks, parses uploaded validation workbooks, stores household validation metadata on `Group.Json_ext`, updates selected participants through `GroupIndividualService`, tracks batch/row outcomes, exposes batch history and error reports through GraphQL, and assigns the required household validation rights to configured administrator and district roles.

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

## GraphQL Backend Testing

Test the backend through the GraphQL fields exposed in `household_validation/schema.py`. The frontend should use these same operations.

Available GraphQL fields:

- `householdValidationProjects`
- `householdValidationBatches`
- `householdValidationBatchRows`
- `householdValidationBatchErrorReport`
- `generateHouseholdValidationList`
- `uploadHouseholdValidationList`

Required rights:

- `958001`: project lookup and validation list generation
- `958002`: validation list upload/apply
- `958003`: batch history and batch row queries
- `958004`: validation upload error report download

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

Generate a validation workbook:

```graphql
mutation {
  generateHouseholdValidationList(
    districtCode: "DISTRICT_CODE"
    excludeVerifiedAfter: "2026-07-01"
    targetCount: 100
    reservePercentage: 10
  ) {
    batchId
    fileName
    fileBase64
    householdsSelected
    reserveHouseholds
    memberRows
  }
}
```

The response `fileBase64` is the Excel workbook content. The frontend should decode it for download.

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
- `participant = YES` updates the selected `GroupIndividual.recipient_type` to `PRIMARY`.
- Project selection is stored as validation intent/prospect metadata only.
- Upload does not create `GroupBeneficiaryProjectEnrollment` records.
- Protected workbook fields such as household/member identifiers, location labels, member details, fit-for-work, head, and current recipient values are checked for tampering.
