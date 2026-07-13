# Customer Data Onboarding Copilot

[![CI](https://github.com/atulk1000/customer-data-onboarding-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/atulk1000/customer-data-onboarding-copilot/actions/workflows/ci.yml)

Production-grade data onboarding workflow for turning messy customer files into validated, canonical, production-ready records.

The demo uses a synthetic healthcare eligibility file because the domain is realistic and recruiter-friendly: it has ambiguous customer columns, required fields, enum normalization, row rejection, audit requirements, and publish controls. The product shape is intentionally generic for implementation, FDE, Solutions Engineering, and Data Solutions roles.

Current release: **v1.3 - Contract-Driven Onboarding and Reconciliation**. The release is implemented, locally verified, and documented in [docs/v1.3-prd.md](docs/v1.3-prd.md).

## Release Snapshot

- `32` automated tests pass; Black, Ruff, compilation, and diff checks are clean.
- The real OpenAI mapping path was validated with `gpt-5-mini` and a sanitized three-column profile.
- The 1,000-row synthetic demo produces 721 accepted rows, 279 rejected rows, five coalesced plans, and zero conflicting target duplicates.
- The demo reconciliation result is `WARNING` because its intentional reject rate exceeds the configured tolerance; publish can proceed only with reviewer signoff.
- PostgreSQL verification covers a successful publish, unchanged replay, a hard-conflict block before write, post-write reconciliation, and a corrected child run.

## What It Demonstrates

- Source file profiling with inferred types, null rates, cardinality, pattern checks, sample values, and enum hints.
- Two mapping modes: deterministic rules and real LLM-assisted mapping.
- Human-in-the-loop mapping review before validation or publish.
- Target schema contracts with data types, validation kinds, required flags, and allowed values.
- PostgreSQL-backed contract registry with JSON import/export and immutable draft/published/retired versions.
- Source-to-target type alignment checks before validation.
- Source coverage review so unused columns are not silently ignored.
- Schema-versioned mapping template save/load for repeat customer files.
- Blocking validation errors, warnings, and customer-correction exports.
- Transformation from one flat source file into canonical `members`, `plans`, and `member_coverage` outputs.
- Deterministic transformation pipelines with multi-source rules, previews, failure policies, and reviewer approval.
- Field-level lineage from original source value through every transformation step to the final target value.
- Pre-publish and post-publish reconciliation with insert/update/unchanged forecasts and transactional rollback.
- Inline and CSV rejected-row correction with immutable originals and child recovery runs.
- Reviewer signoff with comments before publishing.
- Import replay/idempotency check before rerunning the same file.
- PostgreSQL publish path with canonical tables and audit tables.
- HTML and PDF validation reports generated from the same report data model.

## Reviewer Path

For a quick review, start with:

1. [docs/architecture.md](docs/architecture.md) for the workflow and module boundaries.
2. [docs/demo_run.md](docs/demo_run.md) for local verification evidence and expected output counts.
3. [docs/mvp-prd.md](docs/mvp-prd.md) for the product decisions and v1.1/v1.2 scope.
4. [docs/v1.3-prd.md](docs/v1.3-prd.md) for the implemented contract, transformation, reconciliation, and reprocessing specification.
5. [onboarding/schema.py](onboarding/schema.py) for the canonical target schema contract.
6. [onboarding/profiler.py](onboarding/profiler.py) and [onboarding/rules_mapper.py](onboarding/rules_mapper.py) for deterministic profiling and mapping.
7. [onboarding/validation.py](onboarding/validation.py) and [onboarding/transform.py](onboarding/transform.py) for validation, rejected rows, canonical outputs, and lineage.
8. [onboarding/ai_mapper.py](onboarding/ai_mapper.py) for the guarded OpenAI mapping adapter.
9. `python -m pytest` for the fastest local verification path.

## Architecture

```mermaid
flowchart LR
    A["Published target contract"] --> C["Profile and map customer CSV"]
    B["Customer CSV"] --> C
    C --> D["Approve mapping and transformation pipeline"]
    D --> E["Validate and transform"]
    E --> F["Canonical tables and step-level lineage"]
    E --> G["Rejected-row correction queue"]
    G --> H["Corrected child run"]
    H --> E
    F --> I["Pre-publish reconciliation"]
    I --> J["Transactional PostgreSQL publish"]
    J --> K["Post-publish verification"]
    K --> L["HTML / PDF / JSON / CSV evidence"]
```

The code keeps the Streamlit UI thin. The core pipeline lives in plain Python modules so profiling, mapping, validation, transformation, reports, and database publishing can be tested independently.

## Demo Scenario

The checked-in demo file is synthetic only and contains no PHI:

- 1,000 eligibility rows.
- Member, dependent, plan, coverage, and subscriber fields in one flat file.
- Mixed date formats.
- Status aliases such as `A`, `Active`, `T`, `Termed`, and invalid values.
- Relationship aliases such as `Self`, `Spouse`, `Child`, and invalid values.
- Plan type variants such as `PPO`, `P.P.O`, `HMO`, and `HDHP`.
- Missing required fields, invalid emails/phones, future dates, coverage end dates before start dates, duplicate/conflicting member identities, and dependent subscriber issues.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
docker compose up -d
.\.venv\Scripts\python.exe scripts\generate_demo_eligibility_file.py
.\.venv\Scripts\streamlit.exe run app.py
```

Run the synthetic v1.3 database verification:

```powershell
.\.venv\Scripts\python.exe scripts\verify_v13_release.py --publish --verify-rollback --verify-correction
```

Open:

```text
http://localhost:8501
```

On Windows, you can also start the app with:

```powershell
.\run_streamlit.bat
```

Keep that terminal open while using the app.

If the Docker CLI does not recognize `docker compose` on Windows, use the standalone command:

```powershell
docker-compose up -d
```

## Configuration

Rules-based mapping works without an API key. AI-assisted mapping requires `OPENAI_API_KEY`.

```text
DATABASE_URL=postgresql+psycopg://onboarding:onboarding@localhost:55432/onboarding
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5-mini
OPENAI_REASONING_EFFORT=low
```

The default PostgreSQL URL uses host port `55432` to avoid colliding with local Postgres on `5432`.

## Workflow

1. **Target**
   - Select a published versioned contract.
   - Import/export JSON contracts and manage draft, published, and retired lifecycle states.
   - Review target tables, keys, fields, data types, validations, and reconciliation policy.

2. **Upload**
   - Upload a CSV or load the checked-in 1,000-row demo file.

3. **Profile**
   - Inspect source column names, inferred types, null rates, cardinality, value-pattern signals, enum hints, samples, and top values.

4. **Map**
   - Generate rules-based or AI-assisted mapping suggestions.
   - Review confidence, type alignment, reasons, and flags.
   - Approve mappings and review unused source columns.
   - Build and preview ordered deterministic transformation pipelines.
   - Save or load exact-contract-version mapping templates.

5. **Validate**
   - Run deterministic validation on the mapped canonical frame.
   - Review blocking errors, warnings, target fields, source columns, and affected source row numbers.
   - Correct rejected rows inline or through a correction CSV.
   - Revalidate selected rejects or acknowledge unresolved rejects without publishing them.

6. **Transform**
   - Build accepted canonical outputs.
   - Preview contract-defined canonical tables, rejected rows, correction queue, and step-level lineage.

7. **Publish**
   - Check PostgreSQL connectivity.
   - Run import replay/idempotency check.
   - Review expected inserts, updates, unchanged records, duplicates, orphans, and hard reconciliation checks.
   - Capture reviewer signoff.
   - Publish in one transaction and verify stored values before commit.

8. **Report**
   - Download HTML/PDF validation report.
   - Download canonical CSVs, contract/template JSON, reconciliation JSON, correction audit, rejects, and lineage.

## Key Outputs

Canonical outputs:

- `members.csv`
- `plans.csv`
- `member_coverage.csv`

Exception and audit outputs:

- `rejected_rows_with_original_values.csv`
- `rejected_rows_for_correction.csv`
- `correction_audit.csv`
- `field_lineage.csv`
- `reconciliation.json`
- `target_contract.json`
- `mapping_template.json`
- `validation_report.html`
- `validation_report.pdf`

PostgreSQL tables:

- Canonical: `members`, `plans`, `member_coverage`
- Governance: `schema_contracts`, `schema_contract_versions`, `mapping_template_versions`
- Audit: `import_runs`, `mapping_decisions`, `source_column_audit`, `validation_issues`, `rejected_rows`, `field_lineage`
- Reconciliation and recovery: `reconciliation_runs`, `reconciliation_table_metrics`, `reconciliation_checks`, `row_corrections`

## Rerun Behavior

Each publish creates a new `import_run` audit record. The app computes a SHA-256 fingerprint of the uploaded source dataframe and checks PostgreSQL for prior imports with the same fingerprint before publishing.

If the same file was already published, the Publish step shows a replay warning and requires reviewer acknowledgement. Canonical rows are upserted:

- `members` by `member_id`
- `plans` by `plan_id`
- `member_coverage` by deterministic `coverage_id`

The coverage ID is generated from:

```text
member_id + plan_id + coverage_start_date
```

Source-value corrections create a child run that processes only recovered rejected rows. A mapping or transformation change creates a new template version and requires a complete source rerun. Original source values remain immutable in both cases.

## Production-Style Vs. Demo-Limited

Production-style pieces:

- Versioned target contract registry with immutable published definitions.
- Controlled transformation operation catalog with no arbitrary code execution.
- Human approval gates before validation and publish.
- Rules and LLM mapping modes with deterministic fallback.
- Source coverage review for unused columns.
- Field-level lineage for explainability.
- Rejected-row export for customer correction.
- Reviewer signoff.
- Idempotency/replay check.
- Pre/post publish reconciliation and hard-failure rollback.
- Audited rejected-row correction and child recovery runs.
- PostgreSQL audit trail.
- Test coverage for core logic.

Demo-limited pieces:

- One built-in healthcare demo contract and one synthetic demo file; imported contracts use the approved type and validation vocabulary.
- Streamlit UI instead of a role-based web frontend.
- Mapping templates are versioned locally and persisted with publish audit data, but there is no organization-level sharing or permission model.
- Synchronous processing instead of background jobs.
- No authentication, tenant isolation, or permission model.
- AI privacy guardrails are not yet implemented beyond controlled prompt payloads and human review.

## Tests

Run the core test suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
```

Optional compile check:

```powershell
.\.venv\Scripts\python.exe -m compileall -q onboarding app.py scripts tests
```

Quality gates used by CI:

```powershell
.\.venv\Scripts\python.exe -m black --check .
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
```

Current result: `32 passed`. Complete local evidence is captured in [docs/demo_run.md](docs/demo_run.md).

## Project Structure

```text
app.py
docker-compose.yml
requirements.txt
onboarding/
  schema.py
  contracts.py
  profiler.py
  rules_mapper.py
  ai_mapper.py
  mapping_quality.py
  source_coverage.py
  mapping_templates.py
  idempotency.py
  validation.py
  transform.py
  transformations.py
  corrections.py
  reconciliation.py
  database.py
  reports.py
  exports.py
scripts/
  generate_demo_eligibility_file.py
  verify_v13_release.py
data/
  demo/
  mapping_templates/
docs/
  architecture.md
  demo_run.md
  mvp-prd.md
  v1.3-prd.md
tests/
```
