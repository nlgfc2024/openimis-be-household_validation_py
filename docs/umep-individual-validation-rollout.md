# UMEP individual validation: configuration and rollout

This workflow extends the household validation module. A programme's configured
subject selects its generation path; a returned workbook's saved batch selects its
upload contract. Household programmes retain their existing workflow.

## Configuration to review before enabling a deployment

Use the maintained instance files in [coremis-configs](https://github.com/nlgfc2024/coremis-configs):

- [UMEP backend validation](https://github.com/nlgfc2024/coremis-configs/blob/main/household_validation/umep.household_validation.config.json)
- [UMEP enrolment](https://github.com/nlgfc2024/coremis-configs/blob/main/enrolment/umep.enrollment.config.json)

Keep business columns and ranking values in those files. The test suite's small
synthetic configuration demonstrates compatibility; it is not a production rule set.

| Setting | Deployment contract |
| --- | --- |
| `program_eligibility_rules["UMEP"]` | `{"subject": "INDIVIDUAL", "data_source": "ubr"}` for the current example. Match the selected Phase's `json_ext.data_source`. |
| Missing `subject` | Resolves to `HOUSEHOLD`. Keep the existing PWP, UPG and RMEP rules on shared instances. |
| Individual rule keys | `subject` and optional non-empty `data_source` only. Household criteria are not applied to individual selection. |
| `program_specific_export_columns["UMEP"]` | Editable business answers; the current instance file has 31 fields covering explicit enrolment score inputs. |
| `individual_workbook_fields["UMEP"]` | Protected descriptive fields. Each entry has `column_name` and exactly one `json_ext_key` or `location_type`. |
| `export_column_options_overrides` | Option lists keyed by the editable column's `key`, across programmes. Use distinct keys when programmes need different options. |
| Enrolment verification | `registry_individual_verification_field: "validation_status"`, `registry_verified_individual_value: "VERIFIED"`. |
| Frontend `validationListFilters` | Retain the R/D/W/V picker chain. Individual defaults are `district`, `ta`, `gvh`, `village`, `targetCount`, plus the opt-in validated-person exclusion. |

With no individual source configured, validation and enrolment both default to
`ubr`, including missing/null/empty registry source tags. Other source keys match
exactly; configured surrounding whitespace is stripped. Household
`requires_data_source` retains its existing separate matching behavior.

Saved top-level dictionaries **replace** defaults; they are not deeply merged.
The canonical UMEP file is an individual-only instance configuration. On a shared
instance, prepare complete dictionaries retaining the existing household entries
before saving. For example, this offline composition retains other programme
entries; saving the result still replaces each whole block:

```python
from copy import deepcopy

combined = deepcopy(current_config)
for key in (
    "program_eligibility_rules",
    "program_specific_export_columns",
    "individual_workbook_fields",
    "export_column_options_overrides",
):
    combined[key] = {**current_config.get(key, {}), **umep_config.get(key, {})}
```

Review the resulting JSON against the Phase and instance language pack. Save
through normal ModuleConfiguration validation and restart backend workers to load
it. Do not assume that changing a Phase's type alone enables validation.

## Workbook and decision contract

Business columns reuse `key`, `column_name`, `target_individual_json_ext_key`,
`type`, `options`, `required`, `depends_on`, and supported numeric `min`/`max`.
Types are `number`, `select` and `text`. Deployment changes to these definitions
require no workbook/parser/report code change. Verify that enrolment score field
names, numeric types and mapping strings agree with the resolved columns.

Generation saves ordered person identities, protected values, resolved columns,
rule, filters and issued counts. Preview, re-export, parser and error report use
that issued contract. Later configuration edits affect new lists. Regenerate
pre-fix workbooks that were issued with prefilled editable business answers.

| Submitted value | Effect |
| --- | --- |
| `VERIFIED` | Validate fresh applicable required answers, then update the person's status and supplied business fields. |
| `NOT_VERIFIED` | Business answers may be blank. Validate any supplied answers; omitted optional answers preserve current values. |
| Blank/null/whitespace decision | Skip the entire row, including notes and business fields. |
| `REJECTED`, Yes/No, or another unsupported decision | Row error; no changes for that row. |
| Blank/whitespace notes on a decided row | Clear an existing `validation_notes` value to JSON null. |

Decision spelling is case-insensitive and surrounding whitespace is trimmed.
Editable business cells start blank. An explicit dependency answer can clear an
inactive dependent field under its saved column rule. Protected columns must not
be edited. Invalid headers, formulas, duplicate/foreign identities and unknown or
mixed batches are rejected; file-wide errors block every update.

Dry runs write no individuals, attempts or history. Commit rechecks current state,
uses one transaction and records valid rows, skipped rows and row errors. Valid
rows may succeed alongside errors. Identical retries retain attempt evidence
without unnecessary individual writes. The batch and selected individuals are
locked for committed uploads; dry runs acquire neither write lock.

An attempt's outcome is distinct from retained batch status. Zero-applied retries
do not downgrade an existing PROCESSED/PARTIAL_SUCCESS batch. Each attempt keeps
its own outcome; `last_upload_attempt_id` and `last_upload_status` identify the
latest attempt. Batch status is not a cumulative completion count.

## Geography, membership and enrolment

UMEP uses **City R → Constituency D/TA → Ward W/GVH → Neighbourhood V**.
The configured workbook displays City, Ward and Neighbourhood; its omitted
Constituency column does not remove that level from filters or the database.
Picker wording comes from the UMEP language pack, including
`location.Location0Picker.label` through `location.Location3Picker.label`.

Selection uses `Individual.location` and normal user access, counts people, and
excludes deleted people and anyone with a non-deleted membership in a non-deleted
household. A deleted membership or deleted household releases the person. It
does not substitute household geography. `excludeValidated` is opt-in and excludes
both accepted decisions before counts/targets.

Upload intersects every saved geographic level independently. A matching
neighbourhood cannot bypass a saved City, Constituency or Ward after parent
changes. A City-only selection permits moves inside that City. Preview/re-export
omit currently unavailable people and show omission counts alongside fixed issued
counts. Upload reports unavailable people as row errors while permitting other
valid rows in an authorized batch.

Enrolment requires the individual's `validation_status` to be `VERIFIED`, the
Phase's source, no active household membership, and its own profile/Phase criteria,
ranking threshold and capacity. Validation does not enrol a person or guarantee a
place. A low-scoring VERIFIED person can remain in the unranked eligibility pool
while being excluded from the ranked selection. The current example has a
60-point threshold and 1,000 allocated places;
those settings do not limit how many people a validation list may contain.

## Rights, history and reports

| Backend right | Frontend/backend operation |
| --- | --- |
| `958001` | Programme discovery, candidates, generation and initial workbook download. |
| `958002` | Parse/upload, dry run and committed decisions. |
| `958003` | Saved preview/re-export, issued-list picker and attempt history/rows. |
| `958004` | Attempt-specific error report; independent of history permission. |

The contributed **Validate** action requires discovery/generation and upload
rights, an individual programme and an editable context. Retain ordinary
Individuals **Upload** and **Upload History**. Stale `953xxx` rights do not grant
validation access. Discovery failure must show a retryable error and preserve the
existing household generation path.

Attempt rows page with `first`/`offset`; CSV reports include all errors for the
selected attempt. Personal values are redacted when current access is lost.
Unlinked invalid/foreign identities retain correction messages while their raw
submitted values stay hidden. Household history excludes individual batches.

## Release and pilot sequence

1. Deploy the merged validation backend, enrolment support and the full/partial
   beneficiary-update preservation implementation together. Check the installed
   module revisions against the [verification record](umep-individual-validation-verification.md).
2. Merge and deploy the contributed frontend implementation and matching UMEP
   language pack before enabling operator access. Confirm the existing upload
   actions still work. Frontend branch tests are not a live browser sign-off.
3. Back up the current configuration; prepare/review the complete instance blocks,
   Phase source/schema/profile, ranking options and location hierarchy. Run the
   configuration-file acceptance command below in the test assembly.
4. Apply the reviewed configuration, restart backend workers, refresh frontend
   configuration, and grant the intended backend rights through normal roles.
5. Run the pilot checks below using the deployed UI and record evidence. Stop
   enabling additional operators if decisions, counts, permissions, geography,
   history or preservation differ from the expected result.
6. To stop new individual generation, remove the individual programme entry or
   operator access and reload configuration. Preserve issued batches and audit
   records. Existing batches retain their saved contracts; do not rewrite their
   columns or replace individual JSON to undo a decision.

From the assembled backend's `openIMIS` directory, using isolated test PostgreSQL:

```bash
python manage.py test household_validation --keepdb --noinput
UMEP_VALIDATION_CONFIG=/path/to/coremis-configs/household_validation/umep.household_validation.config.json \
UMEP_ENROLMENT_CONFIG=/path/to/coremis-configs/enrolment/umep.enrollment.config.json \
python manage.py test household_validation.test_umep_acceptance --keepdb --noinput
python manage.py test social_protection.tests.test_beneficiary_update_validation_preservation \
  social_protection.tests.test_workflows_beneficiaries_update \
  enrolment.tests.test_services enrolment.tests.test_data_source --keepdb --noinput
```

The acceptance suite uses synthetic records and real SQL update procedures. Both
optional file paths must be supplied together. All acceptance cases must execute;
a skip for missing enrolment/PostgreSQL is not rollout evidence.

### Pilot acceptance record

Automated results are in the verification record. Fill this table with deployed
instance, operator, date, role, module/config revisions and evidence references.
No live pilot sign-off is asserted by repository tests.

| Pilot check | Expected result | Evidence/sign-off |
| --- | --- | --- |
| Generate two eligible people; decide one, leave one blank | Counts/preview/export agree; only the decided person changes. | Pending live pilot |
| Enrolment preview | Only eligible VERIFIED people appear; profile threshold/capacity still apply. | Pending live pilot |
| Ordinary full and partial beneficiary updates | Decision, source and unrelated individual fields survive; unaccepted business corrections are not applied. | Pending live pilot |
| Geography and access | Full City/Constituency/Ward/Neighbourhood picker chain; inaccessible/deleted people omitted or reported correctly. | Pending live pilot |
| Errors, corrections and retries | Dry run writes nothing; partial results, attempt history and CSV match the selected attempt. | Pending live pilot |
| Roles and editable contexts | Validate respects 958xxx rights; normal Upload/Upload History remain usable. | Pending live pilot |
| PWP/UPG/RMEP smoke test | Existing generation, columns, primary workers, upload and history remain correct. | Pending live pilot |

Before enabling the pilot, the programme owner must sign off the deployment's
column wording, requiredness, options/ranges/dependencies, Phase profile and
city/ward/neighbourhood registry mappings. The two decision values and blank-row
policy above are settled; local field/ranking suitability and pilot results need
instance-specific evidence.
