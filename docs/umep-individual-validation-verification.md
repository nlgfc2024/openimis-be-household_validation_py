# UMEP individual validation: integrated verification record

Verified on 2026-11-20 for issue #39. This records automated assembly evidence and
the outstanding release gates; it does not assert a deployed pilot sign-off.
Follow the [configuration and rollout guide](umep-individual-validation-rollout.md)
when enabling an instance.

## Revisions exercised

| Component | Tested revision and status |
| --- | --- |
| Validation backend | This PR, based on `68b094e19a79156a8ef263d0fbce0cf7c88487a8`; implementation PRs #40, #41 and #42 are merged into `mw/develop`. |
| Enrolment | `2f40f95bfabb062a6445687646c0c3907c0980e0` on `mw/develop`, including configured City derivation and programme-profile ranking. |
| Social protection | Base `1273344fd31ede298caeaa8359e83e245f1de516`, including merged preservation PR #23; test-fixture correction `566b4a06e3364561e57ebeb3f4871f187abfcf4a` in [companion PR #26](https://github.com/nlgfc2024/openimis-be-social_protection_py/pull/26). |
| Validation frontend | `235982d720537471a26698ec3db0afa776d50b1d` from [PR #20](https://github.com/nlgfc2024/openimis-fe-household_validation_js/pull/20); still awaiting merge at verification time. |
| Deployment configuration | `935c353` on `coremis-configs/main`, plus documentation-only `02624aaf7e716b8cc75479301fbce973079c6c3b` in [companion PR #8](https://github.com/nlgfc2024/coremis-configs/pull/8). The 31 business definitions and ranking values are unchanged. |

The local assembly used Python 3.12 and isolated PostgreSQL. Dependency worktrees
were selected through `PYTHONPATH` so tests exercised the merged enrolment and
social-protection revisions, rather than older editable checkouts. Configuration
files were read as JSON inside test fixtures; no deployed configuration was applied.

## Automated evidence

| Check | Result and scope |
| --- | --- |
| Full assembled backend regression run | **329/329 passed**, with no skips and the default PostgreSQL planner: 314 validation tests, 10 beneficiary-update regressions and five enrolment regressions. Acceptance cases used the canonical UMEP configuration files. |
| Canonical UMEP configuration acceptance | **6/6 passed** within the full run, against all 31 editable fields and the matching enrolment profile, including the below-threshold VERIFIED case. |
| Self-contained acceptance configuration | **6/6 passed** using the small synthetic configuration exercised by the default module tests. |
| Frontend module | 28/28 tests, lint and build passed at `235982d`. Tests use real Redux and mocked host components/transport. |
| Static checks | Changed backend Python files pass flake8 at 120 characters, compilation and diff checks. Companion configuration parses as JSON. |

Reproduction commands are in the rollout guide. The configuration-backed
run executed the full assembly suite with `UMEP_VALIDATION_CONFIG` and
`UMEP_ENROLMENT_CONFIG` pointing to the instance JSON files. Check the PR's CI and
test evidence against its final head before release.

### Core acceptance scenario

`test_umep_acceptance.UmepValidationAcceptanceTests` runs through the following
with synthetic records and real database-backed application code:

1. Configure a UMEP individual Phase and a full R/D/W/V hierarchy. Generate a list
   of two people outside active households through GraphQL.
2. Compare the issued identities, preview/counts and re-exported workbook. Supply
   fresh, valid configured answers and VERIFIED for one person; leave the other
   person's decision blank.
3. Dry-run the workbook and verify no individual history, attempts or decisions
   are written. Commit it and verify one APPLIED row, one SKIPPED row, correct
   attempt/report data and an idempotent retry.
4. Call the real individual enrolment selection service. It recognizes the
   VERIFIED person and excludes the unvalidated person. Household statuses,
   membership JSON, roles, recipient types and versions remain unchanged.
5. Execute the real full and accepted-row partial beneficiary-update procedures,
   including reporting synchronization. Ordinary business corrections survive;
   incoming attempts to replace VERIFIED or introduce VERIFIED on the skipped
   person do not. Validation notes, source, configured answers and unrelated
   nested JSON survive. Enrolment still recognizes the original VERIFIED result.
6. Verify active-household exclusion agrees between validation and enrolment,
   including duplicate active memberships, deleted memberships and deleted
   households. Verify NOT_VERIFIED and invalid answers do not admit candidates.
7. Verify a valid VERIFIED row with low-scoring answers remains outside the ranked
   selection when the configured profile threshold is not met, even though it
   remains in the unranked eligibility pool. Validation is not automatic enrolment
   or a waiver of ranking rules.

### Regression coverage map

| Requirement | Executed coverage |
| --- | --- |
| PWP/UPG/RMEP coexistence | `test_household_programme_coexistence`: real generation/upload/dry-run, correct programme headers and fields, primary-worker updates, audit rows, household history isolation and rejection of stale rights. Household configurations here are synthetic fixtures. |
| Source, individual geography, access and person counts | `test_individual_selection`, `test_individual_workbook`, `test_individual_upload`, and cross-module acceptance. Access tests exercise supplied access querysets; live district-role mappings remain a pilot check. |
| Issued identities/columns/rules, drift, omission counts | `test_individual_workbook` and `test_individual_review_integrity`; preview/export retain saved values and current-access filtering. |
| Unknown/mixed batches, foreign/duplicate identities, formulas and row errors | Existing workbook/upload suites; file errors block every change and row errors support partial success. |
| Dry-run isolation, repeated/corrected attempts, history and reports | `test_individual_upload` and `test_individual_upload_review`, including redaction, pagination, complete reports and retained batch status after failed retries. |
| Concurrent updates | PostgreSQL transaction tests for same-batch history, overlapping batches and waiting for a registry writer before merging/rechecking source. |
| Large saved lists and joined access | Existing 2,004-row SQL-paging regression plus a duplicate-producing access-join test for preview/export. |
| Beneficiary preservation | Real full/partial SQL procedures in acceptance; companion module tests also cover omitted/null/blank/conflicting decisions, unvalidated people, legacy null JSON and reporting. |
| Frontend rights and contributions | Frontend 28-test suite covers 958xxx gating, editable/read-only contexts, unchanged Upload references, individual contribution, hierarchy labels, fallback/retry, stale responses, dry run/commit and history/report paging. |

## Findings addressed during verification

- Household upload originally flattened all programmes' columns into one
  registry. Required UMEP answers blocked household workbooks, and individual
  definitions could loosen a shared select key. Individual definitions are now
  excluded. Issued household batches use their programme's current columns;
  legacy files retain the household-only union behavior. Removing a required
  programme header still produces an error.
- Four existing quota tests changed the worker's age while their helper kept the
  household head aged 50. The fixtures now set head age explicitly, and a new
  regression distinguishes a young worker from a young head. Production quota
  and age rules are unchanged.
- A reused test database exposed a slow nested-selection query while paging
  saved snapshots. Preview/export now use a correlated authorized-person EXISTS
  query, preserving SQL pagination, deduplication and omission counts. The full
  default-planner run passed after this change; no planner override is needed.
- The merged beneficiary workflow now calls the trusted private reporting
  method. Its test helper still mocked the public method, accidentally running
  reporting during isolated SQL assertions. Companion PR #26 corrects the mock;
  the explicit real-reporting test remains unmocked.
- The canonical configuration comment incorrectly said D/TA had no UMEP
  equivalent. Companion PR #8 identifies Constituency and the workbook-only
  omission without changing any configuration values.

Reference-fixture setup mattered locally: prior TransactionTestCase flushes had
removed the standard English language row and bootstrap auth group from the
reused isolated database. They were restored before the assembled run. An earlier
combined attempt reported eight resulting language-FK errors and the stale mock
failure; those were resolved before the passing run above. A slow-query diagnostic
run was interrupted before the query simplification. Those attempts are not
counted as passing evidence.

## Release gates still requiring evidence

- Merge/deploy the frontend implementation and matching UMEP language pack. A
  passing mocked frontend suite is not a live browser/session test.
- Review/merge the two companion follow-ups and repeat the documented checks on
  the exact release assembly and instance configuration.
- Obtain programme-owner acceptance of the deployment's complete column
  definitions, source/schema/profile, geography and ranking suitability. The
  settled decision/blank/notes contract is documented in the rollout guide.
- Complete its live pilot table, including operator roles, real picker contents,
  workbook usability, two-person acceptance, ordinary full/partial updates,
  history/report accuracy, unchanged household flows and deployment sign-off.

Do not mark the operational rollout complete until those checks have evidence.
