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
- Config-driven, backend-only eligibility rules and a simpler target-count selection for program-based deployments (e.g. Jobs Now) via `benefitPlanCode` — see "Program-Based Selection" below.
- Config-driven, per-program Excel export/upload columns (`program_specific_export_columns`) — see "Program-specific export columns" below.

Enrollment remains a reference workflow only. This module does not call enrollment mutations and does not create `GroupBeneficiaryProjectEnrollment` records.

## Selection Algorithm

`household_validation/selection.py::select_households` is the single implementation behind `generateHouseholdValidationList`, `householdValidationPreview`, and the Excel export, so all three always describe the same selection. It takes a `rule`, resolved from the `program_eligibility_rules` module configuration (see "Permissions" below) for the selected Program — falling back to that config's `"PWP"` entry only when `benefitPlanCode` isn't sent at all. A `benefitPlanCode` that doesn't match any configured entry raises a `ValidationError` rather than silently falling back to PWP, since running PWP's wealth-ranked quota targeting for an unrecognized Program would be a worse failure mode than an explicit error. Whether the rule has a `selection_strategy` key picks the algorithm: present (a dict of quota settings, PWP's) or absent (program-based, e.g. Jobs Now — see "Program-Based Selection" below).

**With `selection_strategy` present**, the algorithm runs in this order:

1. **Sort.** Eligible households (at least one member matching the rule; not excluded by `excludeVerifiedAfter`) are sorted by `household_wealth_quintile` ascending — `Poorest` first, `Richest` last. This quintile is the available proxy for PMT score (there is no separate numeric PMT field on the household); households within the same quintile are ordered by code/id.
2. **Categorize.** Each household is tagged with exactly one category: `FEMALE_HEADED` (head is female), `YOUTH` (no female head, but at least one eligible member aged 18-35), or `OTHER` (neither).
3. **Allocate the target between villages.** For micro-catchment and hotspot requests with an explicit `targetCount`, eligible households are grouped by village and the target is distributed proportionally using the largest-remainder method. When the target is at least the number of villages containing eligible households, every such village receives at least one place. Rounding always preserves the exact overall target. Requests outside micro-catchment/hotspot selection retain the original catchment-wide pool behavior.
4. **Split each village allocation into category quotas.** Each village's requested main-list size is split into three quotas by percentage: **40% female-headed, 40% youth-headed, 20% other**, by default. Only the female-headed and youth-headed percentages are configured values (see below) — there is no separate "other" percentage setting anywhere. The "other" quota is *always computed live* as `100% - femaleHeadedPercentage - youthPercentage`. If the two configured percentages together exceed 100%, they're scaled down proportionally so their sum is exactly 100% and "other" is 0%.
5. **Fill each quota from its village's PMT-sorted pools**, taking the female-headed pool first, then youth, then other. Because the three pools are disjoint and each quota is filled independently, this order only affects row order in the exported list, not which households are ultimately selected.
6. **Backfill any shortfall.** If a category's pool cannot fill its quota, the gap is filled from the remaining eligible households in the same village, still in PMT order. Proportional allocation is capacity-aware, so the combined main list reaches `targetCount` whenever the selected area contains enough eligible households.
7. **Build the reserve/waiting list.** Reserve places default to **20%** of the main-list target and are distributed proportionally across villages from households not selected for the main list. Main and reserve households cannot overlap.

None of the three percentages are GraphQL arguments on `generateHouseholdValidationList` — they're read from the rule (see "Permissions" below), so they can be retuned without a code deploy.

### Program-Based Selection (e.g. Jobs Now)

`generateHouseholdValidationList`/`householdValidationPreview` accept an optional `benefitPlanCode` argument (the Program's benefit plan code, e.g. `RMEP` or `UPG`). When it matches an entry in the `program_eligibility_rules` module configuration that has **no `selection_strategy` key** (see "Permissions" below), `select_households` runs a deliberately simpler path instead of the quota algorithm above:

- **Eligibility is program-specific and stricter.** In addition to the location filters, a household needs at least one member matching the resolved rule — see the `program_eligibility_rules` keys documented under "Permissions". This check lives on `EligibleMember.is_eligible(rule)` (`household_validation/selection.py`), evaluated against fields captured once per member (`data_source`, `recipient_type`, `json_ext`, `dob`/`age`) when the member is built.
- **No wealth ranking, no PMT proxy, no demographic quotas.** `wealth_quintile`/`pmt_score` are not even computed when `selection_strategy` is absent (`EligibleHouseholdSelectionService._build_household` skips those lookups), and there's no `FEMALE_HEADED`/`YOUTH`/`OTHER` categorization or village-proportional allocation.
- **Ordering** is by the rule's `priority_flag`, if any: households with a qualifying member carrying that `json_ext` flag sort first (e.g. RMEP's `business_experience`); everything else follows in a stable, deterministic order (currently by household code/id — a placeholder until a real business-relevance score is defined).
- **The main list is simply the first `targetCount` households** from that ordering — **there is no reserve/waiting list** in this mode. Households beyond `targetCount` are not selected at all (unlike the quota algorithm's 20%-of-target reserve pool).

Deployments that never send `benefitPlanCode` always resolve to the `"PWP"` rule, which has `selection_strategy` set, so they always run the quota algorithm described above. Sending a `benefitPlanCode` that matches no configured rule is an error (see below), not a fallback to PWP.

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

It also exposes `program_eligibility_rules` and `program_specific_export_columns`, which drive selection and Excel export/upload respectively (see "Selection Algorithm" and "Program-specific export columns" above — none of these values are GraphQL arguments on `generateHouseholdValidationList`; update `ModuleConfiguration` for the `household_validation` module to change them):

- `program_eligibility_rules`: a dict keyed by benefit plan code (matched case-insensitively against the `benefitPlanCode` GraphQL argument), including a `"PWP"` entry used whenever `benefitPlanCode` is absent. A non-empty `benefitPlanCode` that matches no key here raises a `ValidationError` instead of falling back to `"PWP"`. Each value is a rule with these optional keys:
  - `selection_strategy`: **presence, not its value, is what matters.** Given as a dict, PWP's wealth-ranked, demographic-quota algorithm runs, configured by that dict's own keys (below). Omitted entirely, the simple algorithm runs instead — see "Program-Based Selection" above.
    - `female_headed_percentage` (default `40`) / `youth_headed_percentage` (default `40`) / `reserve_percentage` (default `20`): the demographic quota split and reserve-list size, applied to the main-list target. There is intentionally no `other_percentage` key — the "other" quota is always derived as `100% - female_headed_percentage - youth_headed_percentage`, so it stays correct however the two configured values are changed.
  - `requires_data_source`: a member's `Individual.json_ext["data_source"]` must equal this value (case-insensitive) — a hard requirement.
  - `requires_recipient_type`: a member's `GroupIndividual.recipient_type` (`PRIMARY`/`SECONDARY`) must equal this value — a hard requirement.
  - `member_flag`: an `Individual.json_ext` boolean key a member must also have — a hard requirement.
  - `member_min_age` / `member_max_age`: inclusive age bounds (derived from `Individual.dob`) a member must also fall within — a hard requirement.
  - `priority_flag`: (meaningful only when `selection_strategy` is absent) an `Individual.json_ext` boolean key that does **not** gate eligibility — it only ranks otherwise-eligible households ahead of the rest of the pool once the selection is capped at `targetCount`.

  Default configuration (`household_validation/apps.py::DEFAULT_CONFIG`) ships **only `"PWP"`** —
  RMEP/UPG (or any other program) are deployment-specific, not built-in defaults, and are added by
  overriding this key wholesale in that deployment's `ModuleConfiguration`. See "Program-based
  (Jobs Now) example" below for a worked RMEP/UPG example to copy from:

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

  In words: **PWP** requires a fit-for-work member, selected via the 40/40/20 wealth-ranked quota
  algorithm — this is the one entry the fallback logic actually depends on being present (see
  below), and the one most deployments run largely unmodified. The "Program-based (Jobs Now)
  example" below's **RMEP** requires a PWP-sourced member — households with such a member qualify
  whether or not that member also has business experience, but business-experienced households are
  selected first (up to `targetCount`) before any other PWP-sourced household fills the remaining
  slots. Its **UPG** requires an SCTP-sourced member who is fit for work and aged 18-60.

  This key is intentionally **not exposed to the frontend**: this module's `ModuleConfiguration` row keeps the default `is_exposed = False`, and `resolve_module_configurations` (`openimis-be-core_py/core/schema.py`) only ever returns rows with `is_exposed = True` to GraphQL callers. A deployment overrides it by updating (or inserting) the `household_validation` module's `ModuleConfiguration` row directly (e.g. via Django admin), leaving `is_exposed` unchecked. A deployment override **replaces the whole `program_eligibility_rules` dict** (it isn't deep-merged with the default), so an override that only adds, say, RMEP-specific tweaks and forgets `"PWP"` doesn't break PWP-style generation on that deployment: `EligibleHouseholdSelectionService._resolve_eligibility_rule` falls back to the built-in default `"PWP"` rule (`household_validation/apps.py::DEFAULT_CONFIG`) whenever the resolved config has no `"PWP"` entry of its own, rather than an empty `{}` — an empty rule would silently make `select_households` run the "simple" algorithm instead of PWP's wealth-ranked quota one for every request that omits `benefitPlanCode`.

  **Migrating from the old flat quota keys:** before `program_eligibility_rules` existed, PWP's quota split was configured via flat top-level `ModuleConfiguration` keys — `female_headed_percentage`, `youth_percentage` (now `youth_headed_percentage`), `reserve_percentage`. Those keys are no longer read at all; the equivalent values now live at `program_eligibility_rules["PWP"]["selection_strategy"]`. A deployment whose saved `ModuleConfiguration` still sets any of the old keys won't get an error — `HouseholdValidationConfig._load_config` (`household_validation/apps.py`) logs a `logger.warning(...)` on startup naming exactly which stale keys it found, since silently ignoring a configured override is worse than a loud one-time log line. There's no automatic migration; update the deployment's `ModuleConfiguration` row to move the values into `selection_strategy` to silence the warning.

## Program-specific export columns

VERIFIED status depends **only** on Primary Worker. At the **participant** level, a
member is `VERIFIED` once the Primary Worker cell is answered at all — `YES` or `NO`
— meaning they were checked on the ground, whether or not they were chosen as the
primary worker; only a blank cell is `NOT_VERIFIED`. At the **household** level, an
actual primary worker still has to be designated: exactly one `YES` makes the
household `VERIFIED` (shown on all its rows); no `YES` (even if some members
answered `NO`) means `NOT_VERIFIED`; more than one `YES` means `REJECTED` and blocks
household updates. No other column — business or otherwise — affects either status;
they're all optional, informational fields
(`household_validation/verification.py::resolve_participant_status`/
`resolve_household_status`).

Beyond the base schema (`household_validation/excel.py::EXCEL_COLUMNS`), each Program
can define its own extra Excel columns via the `program_specific_export_columns`
`ModuleConfiguration` key (backend-only, `is_exposed = False`, same as
`program_eligibility_rules` above). Same "PWP-only ships by default" rule as
`program_eligibility_rules`: only `"PWP"`'s 3 business columns are in
`DEFAULT_CONFIG` — RMEP/UPG's columns are in "Program-based (Jobs Now) example" below for a
deployment to copy in. Each entry is a list of column definitions:

```python
"program_specific_export_columns": {
    "PWP": [
        {
            "key": "has_business",
            "column_name": "Does member has a business",
            "target_individual_json_ext_key": "business_experience",
            "type": "select",
            "options": ["Yes", "No"],
            "required": False,
        },
        {
            "key": "business_type",
            "column_name": "Type of Business",
            "target_individual_json_ext_key": "type_of_business",
            "type": "select",
            "options": ["Crop farming", "Livestock farming", "..."],
            "required": True,
            "depends_on": {"key": "has_business", "equals": "Yes"},
        },
        {
            "key": "business_duration",
            "column_name": "Business Period (in years)",
            "target_individual_json_ext_key": "business_period",
            "type": "number",
            "min": 0, "max": 100,
            "required": True,
            "depends_on": {"key": "has_business", "equals": "Yes"},
        },
    ],
}
```

Field meanings:

- `key`: stable id, referenced by `depends_on` and used as the upload-side parsing
  registry's lookup key. Not shown to the field officer.
- `column_name`: the Excel header text.
- `target_individual_json_ext_key`: where the value is stored/read on
  `Individual.json_ext`.
- `type`: `"select"` (dropdown from `options`), `"number"` (optional `min`/`max`
  bounds), or `"text"` (freeform, no validation).
- `required`: when `depends_on` is absent or satisfied for a row, a blank value is an
  upload row-error — but this **never** affects VERIFIED status.
- `depends_on` (optional): `{key, equals}` — the cell is only editable (export) and only
  checked/required (upload) when the referenced column's value on that row equals
  `equals`. A dependency left unanswered leaves the column's stored value untouched; an
  explicit non-matching answer (e.g. `has_business = No`) clears it.

`generateHouseholdValidationList`/`householdValidationPreview`'s `benefitPlanCode`
resolves which program's columns are used
(`EligibleHouseholdSelectionService._resolve_export_columns`), falling back to `"PWP"`'s
columns only when `benefitPlanCode` is absent — the same fallback pattern as
`program_eligibility_rules`, but resolved independently: a deployment can configure
eligibility without export columns, or vice versa.

Upload never requires these columns as headers — a workbook missing one just gets
`None` for that field on every row, and `parse_validation_workbook` doesn't know (or
need to know) which program a given workbook was exported for: it validates whatever
configured columns it finds, across every program's definitions merged together
(`upload.py::_column_registry`/`_parse_extra_columns`). Since two programs can share a
column `key` (PWP and RMEP both have `has_business`) with different `options`/bounds,
the merge is permissive rather than picking one arbitrarily: `upload.py::_merge_columns`
unions `options`, widens numeric `min`/`max`, and only keeps `required` when every
definition that shares the key agrees — so a value valid under *either* program's rule
is accepted, rather than whichever program happened to merge last silently winning.

### Program-based (Jobs Now) example

A worked example of a Jobs Now (RMEP/UPG) `ModuleConfiguration` override. Not loaded
automatically — paste the `program_eligibility_rules`/`program_specific_export_columns`
entries you need into the `household_validation` module's `ModuleConfiguration.config`
(layer `be`) via Django admin, and update the `"RMEP"`/`"UPG"` keys first to match this
deployment's actual `BenefitPlan.code` values (see the note on `program_eligibility_rules`'
top-level keys above).

```json
{
  "program_eligibility_rules": {
    "RMEP": {
      "requires_data_source": "PWP",
      "priority_flag": "business_experience"
    },
    "UPG": {
      "requires_data_source": "SCTP",
      "member_flag": "fit_for_work",
      "member_min_age": 18,
      "member_max_age": 60
    }
  },
  "program_specific_export_columns": {
    "RMEP": [
      {
        "key": "has_business",
        "column_name": "Does member has a business",
        "target_individual_json_ext_key": "business_experience",
        "type": "select",
        "options": ["Yes", "No"],
        "required": false
      },
      {
        "key": "business_type",
        "column_name": "Type of Business",
        "target_individual_json_ext_key": "type_of_business",
        "type": "select",
        "required": true,
        "depends_on": {"key": "has_business", "equals": "Yes"},
        "options": [
          "Rice production", "rice aggregation/trading", "rice milling", "rice packaging/marketing", "input supply",
          "Cattle production/fattening", "cattle aggregation/trading", "meat retailing",
          "livestock feed/input supply", "livestock services",
          "Beekeeping", "honey harvesting", "honey processing/packaging", "honey aggregation/marketing",
          "beekeeping equipment",
          "Soybean production", "aggregation/trading", "soybean processing", "livestock/feed production",
          "Groundnut production", "peanut butter processing", "sorting/packaging",
          "Fruit production/aggregation", "fruit processing/drying", "packaging/marketing",
          "macadamia production/aggregation", "processing",
          "Sunflower production", "oil pressing/processing", "sunflower cake/feed production",
          "Goat production", "goat aggregation/trading", "livestock feed", "animal-health services",
          "Broiler production", "layer/egg production", "poultry feed", "chick/input supply", "poultry trading",
          "Vegetable production", "aggregation", "vegetable trading", "processing/drying",
          "Dairy production", "milk collection/bulking", "milk processing", "yoghurt and other dairy products",
          "milk retailing",
          "fresh produce trading", "processing/packaging", "egg production",
          "Tea production/smallholder supply", "tea nurseries", "green-leaf aggregation/transport",
          "tea-related services",
          "Pineapple and banana production", "fruit aggregation", "fruit trading",
          "rice aggregation", "packaging", "rice trading",
          "Fish production", "fish trading", "fish processing/drying", "fish aggregation", "fish feed/input supply",
          "Cassava production", "cassava flour processing", "cassava snacks/chips", "cassava trading"
        ]
      },
      {
        "key": "business_duration",
        "column_name": "Business Period (in years)",
        "target_individual_json_ext_key": "business_period",
        "type": "number",
        "min": 0,
        "max": 100,
        "required": true,
        "depends_on": {"key": "has_business", "equals": "Yes"}
      },
      {
        "key": "business_capital",
        "column_name": "Business Capital",
        "target_individual_json_ext_key": "business_capital",
        "type": "number",
        "min": 0,
        "required": false,
        "depends_on": {"key": "has_business", "equals": "Yes"}
      },
      {
        "key": "business_skills",
        "column_name": "Business Skills",
        "target_individual_json_ext_key": "business_skills",
        "type": "select",
        "required": false,
        "depends_on": {"key": "has_business", "equals": "Yes"},
        "options": [
          "Formal training",
          "Apprenticeship",
          "Learned from family/community",
          "Self-taught",
          "Previous work/business experience"
        ]
      },
      {
        "key": "prior_knowledge",
        "column_name": "Prior Knowledge",
        "target_individual_json_ext_key": "prior_knowledge",
        "type": "select",
        "required": false,
        "depends_on": {"key": "has_business", "equals": "Yes"},
        "options": [
          "Technical/Production",
          "Business management",
          "Financial literacy",
          "Marketing/sales"
        ]
      }
    ],
    "UPG": [
      {
        "key": "savings_experience",
        "column_name": "Savings Experience",
        "target_individual_json_ext_key": "savings_experience",
        "type": "select",
        "options": ["Yes", "No"],
        "required": false
      },
      {
        "key": "willingness_to_participate",
        "column_name": "Willingness to Participate",
        "target_individual_json_ext_key": "willingness_to_participate",
        "type": "select",
        "options": ["Yes", "No"],
        "required": false
      },
      {
        "key": "sanitary_facilities",
        "column_name": "Sanitary Facilities",
        "target_individual_json_ext_key": "sanitary_facilities",
        "type": "select",
        "required": false,
        "options": ["Toilet", "Mponda gear", "Kitchen", "Rubbish pit", "Bathroom"]
      }
    ]
  }
}
```

### `export_column_options_overrides`

For the common case — a deployment that's otherwise happy with the built-in PWP
defaults but wants different `business_type` choices — redeclaring the whole
`program_specific_export_columns["PWP"]` list is more than necessary. The
`export_column_options_overrides` `ModuleConfiguration` key swaps just a column's `options`,
keyed by the column's `key` (not by program):

```json
{
  "export_column_options_overrides": {
    "business_type": ["Rice farming", "Retail shop", "Transport services"]
  }
}
```

is a complete, valid override on its own — no need to touch `program_eligibility_rules`
or `program_specific_export_columns` at all. Applied by
`apps.py::apply_column_option_overrides`, called from both the export path
(`_resolve_export_columns`) and the upload path (`_column_registry`) so the two stay
consistent. It applies wherever the column `key` appears across *every* configured
program — if a deployment later runs PWP and RMEP with genuinely different
`business_type` options again, use each program's own `program_specific_export_columns`
entry instead of this key for that column.

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
- `benefitPlanCode` (the Program's benefit plan code, e.g. `RMEP`/`UPG` — see "Program-Based Selection" above; omit it for the default PWP algorithm)

`ta` maps to the municipality/TA level in the location hierarchy. `hotspotId`/`hotspotCode` resolve to a `location.Hotspot` and scope selection to its linked villages; `catchmentId`/`catchmentCode` resolve to a `location.MicroCatchment` and scope selection to its linked TAs and GVHs. Only one location filter tier applies per request — the most specific one supplied wins, in this order: village > GVH > hotspot > TA > micro-catchment > district > region. Selection quota percentages (female-headed, youth, reserve) are no longer request arguments — they come from `ModuleConfiguration` (see above), as is `benefitPlanCode`'s eligibility rule (`program_eligibility_rules`).

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

Generate for a program-based (e.g. Jobs Now) deployment instead by adding `benefitPlanCode`; the response shape is identical, but `selectedFemaleHeadedHouseholds`/`selectedYouthHouseholds` are always `0` and `reserveHouseholds` is always `0` for these requests (see "Program-Based Selection" above):

```graphql
mutation {
  generateHouseholdValidationList(
    districtCode: "DISTRICT_CODE"
    benefitPlanCode: "RMEP"
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
- Generated workbooks contain protected `participant_status` and `household_status` formulas that recalculate as officers fill the sheet. Upload recomputes these statuses from inputs, ignoring uploaded status values. A participant is `VERIFIED` once Primary Worker is answered at all — `Yes` or `No` — meaning they were checked on the ground whether or not chosen as the primary worker; only a blank Primary Worker cell is `NOT_VERIFIED`. Business (or any other extra column) never affects this status.
- A household is `REJECTED` if any member is rejected or more than one primary worker is selected. Otherwise it is `VERIFIED` only once a primary worker is actually designated (`Yes`), and `NOT_VERIFIED` otherwise — even if every member answered `No`. A member's own participant status remains independent of other members' results and of the household's status.
- Primary Worker dropdowns allow at most one `YES` per household, matched by hidden `group_uuid` across the whole sheet. When another member is `YES`, only `NO` is available and a Stop error blocks typing a second `YES`. Clear the current selection or change it to `NO` before selecting a different worker. Conflicting `YES` cells are highlighted red if pasted data bypasses validation; upload retains its existing household rejection check.
- Generated workbooks always leave `primary_worker`, `validation_notes`, and any enabled business columns blank for fresh field collection, even when saved answers exist. Export does not change those saved answers or suggest a Primary Worker from `recipient_type`.
- Business fields require Primary Worker `YES`. Business Type and Period also require Business `Yes`; Period requires a selected type. Unavailable cells retain the household row colours, have no applicable dropdown choices and reject invalid typed entries. Existing values become red when their prerequisites are removed; clear them before uploading. This is validation, not dynamic cell protection or automatic clearing. Upload enforces the worker prerequisite even when copy/paste bypasses workbook rules. Generate a new workbook to receive these rules.
- Participant rows are grouped visually by alternating green and light-green fills; every row belonging to the same household uses the same fill.
- `participantsVerified`, `participantsNotVerified`, and `participantsRejected` count unique members by their calculated status. Existing multiple-primary-worker rejections count the conflicting primary workers as rejected and block changes for that household. Invalid households are excluded from verification counts.
- Multiple-primary-worker conflicts remain household rejections. Business information on a non-primary worker is a correctable upload error, excluded from verification counts until corrected.
- Historical business-rule rejection records remain available in audit and rejection downloads. New invalid business entries do not save participant changes for the affected household.
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
- Female-headed, youth, and reserve quota percentages are configured via `ModuleConfiguration`'s `program_eligibility_rules["PWP"]` (default 40/40/20 main quotas, 20% reserve) rather than passed as request arguments.
- Percentage over-allocation is normalized so selection cannot exceed the requested target.
- Households are sorted poorest-first by wealth quintile (PMT proxy) before quotas are applied, and the reserve/waiting list continues in that same order past the main list rather than being re-sorted.
- Hotspot and micro-catchment filters (`hotspotId`/`hotspotCode`, `catchmentId`/`catchmentCode`) scope selection to a `location.Hotspot`'s villages or a `location.MicroCatchment`'s TAs/GVHs.
- Upload and export behavior still do not create enrollment records.

Implemented and verified against the real test suite:

- `benefitPlanCode` on `generateHouseholdValidationList`/`householdValidationPreview` selects a `program_eligibility_rules` rule (`ModuleConfiguration`, not exposed to the frontend) instead of the `"PWP"` one.
- `select_households` is a single function driven by whether the resolved rule's `selection_strategy` key is present: a dict (PWP's wealth/demographic-quota allocation, percentages read from that dict) or absent (program-based — no wealth/PMT computation, ordered by `priority_flag`, capped at `targetCount` with no reserve list).
- PWP's female-headed/youth/reserve percentages moved from flat `female_headed_percentage`/`youth_headed_percentage`/`reserve_percentage` `ModuleConfiguration` keys into `program_eligibility_rules["PWP"]["selection_strategy"]`; `household_validation/tests.py` was updated to pass a `rule={"selection_strategy": {...}}` argument to `select_households` in place of the old `patch.object(HouseholdValidationConfig, "...")` calls.
- Dedicated coverage for the pieces a config/algorithm refactor like this can silently break: `EligibleMember.is_eligible` (`EligibleMemberIsEligibleTest`), `EligibleHouseholdSelectionService._resolve_eligibility_rule` including its case-insensitive matching and PWP fallback (`ResolveEligibilityRuleTest`), and the full `generate()` path end-to-end for no-`benefitPlanCode`/`RMEP`/`UPG` (`ProgramBasedGenerationIntegrationTest`). That last class is what actually exercises "an existing PWP deployment that never sends `benefitPlanCode` keeps running the same quota/reserve algorithm as before" — the isolated `select_households` unit tests above all pass an explicit `rule=`, so none of them alone proved that end-to-end default path still works.
- A deployment whose saved `ModuleConfiguration` still sets a retired key (the flat `female_headed_percentage`/`youth_percentage`/`reserve_percentage` quota keys, or `business_columns_enabled`/`business_type_options`) gets a startup `logger.warning` naming exactly which stale keys it found, rather than the override silently doing nothing (`HouseholdValidationConfig._load_config`, `RETIRED_CONFIG_KEYS`).
- PWP's business columns (`has_business`/`business_type`/`business_duration`), and RMEP/UPG's own extra columns, moved from the `business_columns_enabled` boolean into the fully config-driven `program_specific_export_columns` — see "Program-specific export columns" above. VERIFIED status was also fixed to depend only on Primary Worker; it previously required a valid business answer too when `business_columns_enabled` was on.
- Only `"PWP"` ships in `DEFAULT_CONFIG` for both `program_eligibility_rules` and `program_specific_export_columns` — RMEP/UPG moved out to the "Program-based (Jobs Now) example" above since every real deployment replaces that content with its own program codes/columns anyway. An `export_column_options_overrides` `ModuleConfiguration` key lets a deployment swap just a column's `options` (e.g. `business_type`) without redeclaring either config — see "`export_column_options_overrides`" above.
- The accepted Primary Worker spellings (`YES`/`Y`/`TRUE`/`1`) are defined once, in `verification.py::YES_VALUES`, and imported by both `upload.py`'s parser and `excel.py`'s live in-sheet `VERIFIED`/`NOT_VERIFIED` formula — previously the same 4 values were hardcoded independently in both places, with no test catching drift if only one copy was ever updated. The household-status formula's `COUNTIFS(...,"REJECTED")` clause (checking whether any row's `participant_status` was itself `"REJECTED"`) was also removed: `participant_status` can only ever be `VERIFIED`/`NOT_VERIFIED`, so that clause was always false.
- `resolve_participant_status` was corrected: a member is `VERIFIED` once Primary Worker is answered at all (`Yes` *or* `No`, i.e. `NO_VALUES` now also counts), not only on `Yes` — a `No` answer still means the member was checked on the ground, just not chosen as the primary worker. `resolve_household_status`'s own `VERIFIED`/`REJECTED` semantics are unchanged (still keyed on an actual `Yes`). Since `household_status`'s live Excel formula could no longer infer "a primary worker was designated" from the `participant_status` column once that column also turns `VERIFIED` on `No`, it was rewritten to read the `primary_worker` answers directly instead of `COUNTIFS(...,"VERIFIED")` against `participant_status`.

Local verification commands (this module's package resolved from a checkout via `PYTHONPATH`, since the project venv otherwise has `household_validation` installed as a separate site-packages copy):

```bash
python3 -m compileall -q openimis-be-household_validation_py/household_validation
flake8 --max-line-length=120 openimis-be-household_validation_py/household_validation
```

```bash
cd openimis-be_py/openIMIS
PYTHONPATH="<path-to-this-checkout>:$PYTHONPATH" ../.venv/bin/python manage.py test household_validation --keepdb
```

Latest local result:

```text
Found 161 test(s).
Ran 161 tests in 0.7s
OK
```

All 161 tests pass.

