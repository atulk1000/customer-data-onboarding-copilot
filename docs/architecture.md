# Architecture

## Goal

Build a portfolio-quality customer data onboarding workflow that turns a messy customer file into canonical records with validation, lineage, rejected-row handling, reviewer signoff, and publish auditability.

The app is intentionally framed as an implementation control plane rather than a generic CSV mapper.

## Core Flow

1. Target schema
   - select a published contract from the PostgreSQL registry
   - import/export JSON contracts with tables, keys, relationships, types, validations, and reconciliation policy
   - keep published versions immutable and retain draft/published/retired lifecycle evidence

2. Source profiling
   - normalize source column names
   - infer source types from names and values
   - compute null rates, uniqueness, value-pattern rates, date bounds, top values, and enum hints

3. Mapping
   - generate deterministic rules-based suggestions
   - optionally call an LLM for AI-assisted suggestions
   - compare source profile evidence against target data type contracts
   - configure ordered deterministic transformation pipelines from an approved operation catalog
   - preview before/after values and require human approval before validation

4. Coverage review
   - show every source column
   - mark approved mapped, suggested only, or unused
   - require reviewer acceptance before ignoring unused columns

5. Validation
   - validate the canonical flat frame after mapping approval
   - create blocking errors for rows that cannot publish
   - create warnings for quality issues that do not block publish
   - attach target field and mapped source column to each issue
   - expose inline correction, correction CSV upload, and acknowledged-reject controls

6. Transformation
   - build contract-defined output tables, including `members`, `plans`, and `member_coverage` for the demo
   - generate deterministic `coverage_id` values
   - produce rejected rows with original values
   - produce field-level lineage with the ordered transformation trace for every source row and target field

7. Publish
   - run PostgreSQL connectivity and replay checks
   - forecast inserts, updates, unchanged rows, duplicates, missing outputs, and foreign-key orphans
   - capture reviewer signoff
   - publish in one transaction and verify stored values before commit
   - roll back hard reconciliation failures and retain a failed import-run audit record
   - persist contract/template versions, import data, lineage, reconciliation, corrections, and signoff

8. Report
   - generate HTML and PDF reports from the same report data model
   - export canonical data, correction work queue, correction audit, lineage, contract, template, and reconciliation evidence

## Module Boundaries

| Module | Responsibility |
| --- | --- |
| `onboarding/schema.py` | Canonical schema, aliases, data type contracts, enum normalizers |
| `onboarding/contracts.py` | Contract validation, checksums, lifecycle, JSON import/export, PostgreSQL registry |
| `onboarding/profiler.py` | Source column profiling and type inference |
| `onboarding/rules_mapper.py` | Deterministic mapping suggestions and scoring |
| `onboarding/ai_mapper.py` | OpenAI-assisted mapping with structured output validation |
| `onboarding/mapping_quality.py` | Source/target type alignment and blocking mismatch checks |
| `onboarding/source_coverage.py` | Unused source column audit and recommendations |
| `onboarding/mapping_templates.py` | Exact-contract-version mapping and transformation template save/load |
| `onboarding/validation.py` | Canonical validation errors and warnings |
| `onboarding/transformations.py` | Controlled operation catalog, pipeline execution, preview, failure policies |
| `onboarding/transform.py` | Dynamic canonical outputs, deduplication metrics, rejected rows, field lineage |
| `onboarding/corrections.py` | Stable source identity, correction CSV validation, immutable overlays, audit rows |
| `onboarding/reconciliation.py` | Transform/pre/post reconciliation, database forecast, stored-value verification |
| `onboarding/idempotency.py` | Source file fingerprint and import replay check |
| `onboarding/database.py` | PostgreSQL schema initialization and publish path |
| `onboarding/reports.py` | HTML/PDF report data and rendering |
| `app.py` | Streamlit workflow orchestration |

## Mapping Confidence Model

The rules mapper uses the same scoring framework across fields:

```text
Name match score:      0-70
Value/profile score:   0-30
Ambiguity penalty:     0 to -20
Conflict penalty:      0 to -10
Final confidence:      0-100
```

Header names are useful but not enough. A mapping should become high confidence only when the source name and value profile agree with the target field contract.

## Type Treatment

Target field data types are declared before mapping:

- `identifier`
- `text`
- `date`
- `enum`
- `email`
- `phone`
- `numeric`
- `boolean`

The profiler infers source types, but the target field type decides final handling:

- text fields are trimmed and preserved
- enum fields are normalized to allowed vocabularies
- dates are parsed to canonical dates
- phones are normalized to `###-###-####`
- invalid enum/date/phone/email values become validation issues

## Rejected Rows And Lineage

Rejected rows are designed as a customer-correction work queue.

They include:

- source row number
- blocking error metadata
- warning metadata
- mapped source columns
- original source values prefixed with `original__`
- editable values prefixed with `corrected__`
- stable source record ID and immutable original-row fingerprint

Field lineage is designed as an explainability artifact.

It answers:

```text
For this target field, which source column and source value were used?
What normalized value did the app produce?
Which ordered transformation steps were applied?
Was a corrected overlay used?
Was the field accepted, warning-only, or erroring?
```

Source-value corrections revalidate only the selected reject set and publish recovered rows through a child run. Mapping or transformation changes create a new template version and force a full source rerun so one file is never processed under mixed logic.

## Production Upgrade Path

- Add authentication, roles, and organization-level separation.
- Add organization-level contract/template sharing, promotion, and permissions.
- Add AI privacy controls such as masking, sample minimization, and prompt preview.
- Add import history UI and run comparison.
- Support multi-file onboarding packages and non-CSV source formats.
- Move retained source snapshots to encrypted object storage for asynchronous reruns.
- Add recurring feed schema and value-distribution drift monitoring.
