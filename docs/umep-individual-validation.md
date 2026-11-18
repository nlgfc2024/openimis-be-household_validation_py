# UMEP programme configuration and candidate selection

This implements work package 1 (#36) on `mw/develop`. It supplies programme
resolution, an individual candidate service and read-only GraphQL queries.
Persisting the generated list and exporting/parsing individual workbooks belongs
to #37; applying uploads and preserving status during beneficiary updates belongs
to #38. The candidate query is not a persisted validation list.

## Enabling an individual programme

The existing `household_validation` backend `ModuleConfiguration` stores
`program_eligibility_rules`, `program_specific_export_columns` and
`export_column_options_overrides`. Add this rule alongside existing programmes:

```json
"UMEP": {"subject": "INDIVIDUAL"}
```

Programme codes are matched case-insensitively. Missing `subject` means
`HOUSEHOLD`; `HOUSEHOLD` and `INDIVIDUAL` are the only supported subject values.
PWP, UPG and RMEP retain their existing household rules. No programme profiles
or separate instance capability flag are introduced. The default configuration
still contains only PWP: individual validation is opt-in through deployment config.

The individual v1 rule supports only `subject`: household member, quota and
priority settings are rejected for individual programmes rather than silently
ignored. Its eligibility rules are the user scope, geography and active-household
exclusion below. Business ranking remains the responsibility of enrolment.

Saved **top-level keys replace their entire default dictionaries**, with no deep
merge. When editing `program_eligibility_rules` or
`program_specific_export_columns`, retain the existing entries required by your
deployment. Explicit unknown programme codes fail. Omitting a programme continues
the legacy default-PWP behavior, including its existing built-in fallback.

See [examples/umep.config.json](../examples/umep.config.json) for an opt-in
configuration containing unchanged PWP defaults and a UMEP entry. It is not loaded
automatically. Add your current RMEP/UPG entries before using it in a deployment
that already configures those programmes; the example does not replace their rules.

## Configurable verified fields

Keep UMEP columns under `program_specific_export_columns["UMEP"]`. Reuse the
existing `key`, `column_name`, `target_individual_json_ext_key`, `type`, `options`,
`required`, `depends_on`, `min` and `max` properties and the existing
`export_column_options_overrides` mechanism.

The example uses the enrolment ranking field representations:

| Field | Representation | Example validation |
| --- | --- | --- |
| `monthly_turnover` | Numeric MWK amount | Non-negative number, required |
| `youth_owned_managed` | `Yes` / `No` string | Required dropdown |
| `inclusion_contribution` | `Yes` / `No` string | Required dropdown |

Required flags in the example are deployment choices, not new eligibility or
ranking thresholds. The numeric representation and exact `Yes` strings match
the UMEP ranking example; keep definitions consistent with your deployed ranking.
These keys are not hardcoded in the selection service: supported columns can be
added through configuration. No age, turnover threshold, city allocation or score
cutoff is imposed by registry validation selection.

`resolve_programme_columns()` returns a detached copy with option overrides
applied, ready for #37 to save alongside the resolved rule in `batch.json_ext`.
Export, individual parsing and error reports must use that same resolved UMEP
snapshot, not the household parser's union of all programme columns.

## Individual decision contract

The accepted values, confirmed for #36, are **VERIFIED** and **NOT_VERIFIED**.
The shared `parse_individual_decision()` helper trims surrounding whitespace and
normalises case; it does not apply any database changes.

| Input | Contract for individual workbook processing |
| --- | --- |
| `VERIFIED` | Validate applicable fields, then write `Individual.json_ext.validation_status = "VERIFIED"` |
| `NOT_VERIFIED` | Validate applicable fields, then write `Individual.json_ext.validation_status = "NOT_VERIFIED"` |
| Empty / whitespace / null | Skip the whole row; no status or programme-field changes |
| `REJECTED`, Yes/No, boolean, number or other unsupported value | Row error; apply no changes from that row |

For either submitted decision, enforce applicable `required`, `depends_on`,
type, option and numeric constraints before applying that row. An unmet dependency
deactivates the dependent column according to the existing column mechanism.
Invalid fields or missing applicable required values produce a row error and no
row updates. Unchanged status may still accompany valid programme-field updates;
avoid unnecessary status writes. Blank decisions skip all row updates.

This does not change household Primary Worker decisions or household REJECTED
status. Do not derive an individual decision from a Yes/No Primary Worker answer.
The enrolment contract is the individual's actual JSON status, not a separate
assessment table. Workbook and upload enforcement of this contract follow in
#37–#38; no status mutation is supplied by #36.

## Geography, membership and ordering

1. Require an authenticated user and apply `Individual.get_queryset()` before
   filters, counts and pagination.
2. Select non-deleted individuals with no active household membership. Match
   enrolment's definition: membership, household and individual are all non-deleted.
   Deleted memberships or memberships in deleted households do not exclude a person.
   This mirrors enrolment without adding a dependency on that module.
3. Filter **the individual's own location**, using the same hierarchy and
   hotspot/catchment coverage utilities as household validation. There is no
   household-location fallback. A record with no own location cannot satisfy a
   geographic filter; no geographic filter leaves it subject to normal user scope.
4. Match the existing geography precedence: catchment coverage is applied first,
   followed by the most specific supplied village, GVH, hotspot, TA, district or
   region filter. These filters never widen the user's authorized queryset.
5. Order by the immutable individual primary key. `target_count` is a non-negative
   number of people; zero selects none and an omitted target selects all candidates.
   Count eligible individuals before the target cap. Do not require a VERIFIED
   status before selecting somebody to be verified.

There are no household caps, household quotas or household-member selection in
this path. `selection_service(programme, user)` dispatches the subject-specific
service. Existing household endpoints reject an individual rule rather than
exporting a household workbook for it while #37 is pending.

## Frontend contract

Both read queries require the configured query right, `958001` by default.
Anonymous or unauthorized calls fail; individual row access remains enforced
even when a user has the query right. Existing rights are reused, with no new codes.

```graphql
query {
  validationProgrammes {
    benefitPlanCode
    subject
    columns
    decisionValues
    blankDecisionBehavior
  }
  individualValidationCandidates(
    benefitPlanCode: "UMEP"
    districtCode: "CITY_CODE"
    targetCount: 100
    first: 20
    offset: 0
  ) {
    eligibleIndividuals
    selectedIndividuals
    hasNextPage
    items { id firstName lastName dob locationId }
  }
}
```

`benefitPlanCode` is required on the individual query. `first` defaults to 20 and
is bounded to 0–100; `offset` is non-negative. Counts describe the complete eligible
and target-capped selections, independent of the returned page. Programme discovery
lists configured entries only; household entries have no individual decision
contract. The individual query does not accept the household-only
`excludeVerifiedAfter` filter.

## Downstream implementation gate

Work packages 2–5 use this two-value decision contract and individual geography.
They must preserve existing household behavior, use the saved individual selection
and resolved definitions for issued lists, and keep the existing Individuals
Upload/Upload History actions intact. UI scaffolding can overlap after API agreement;
the complete workflow is not available until the dependent work is delivered.
