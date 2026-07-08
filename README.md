# Customer Data Onboarding Copilot

[![CI](https://github.com/atulk1000/customer-data-onboarding-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/atulk1000/customer-data-onboarding-copilot/actions/workflows/ci.yml)

Production-grade data onboarding workflow for turning messy customer files into validated, canonical, production-ready records.

The demo uses a synthetic healthcare eligibility file because the domain is realistic and recruiter-friendly: it has ambiguous customer columns, required fields, enum normalization, row rejection, audit requirements, and publish controls. The product shape is intentionally generic for implementation, FDE, Solutions Engineering, and Data Solutions roles.

## What It Demonstrates

- Source file profiling with inferred types, null rates, cardinality, pattern checks, sample values, and enum hints.
- Two mapping modes: deterministic rules and real LLM-assisted mapping.
- Human-in-the-loop mapping review before validation or publish.
- Target schema contracts with data types, validation kinds, required flags, and allowed values.
- Source-to-target type alignment checks before validation.
- Source coverage review so unused columns are not silently ignored.
- Schema-versioned mapping template save/load for repeat customer files.
- Blocking validation errors, warnings, and customer-correction exports.
- Transformation from one flat source file into canonical `members`, `plans`, and `member_coverage` outputs.
- Field-level lineage from original source value to normalized target value.
- Reviewer signoff with comments before publishing.
- Import replay/idempotency check before rerunning the same file.
- PostgreSQL publish path with canonical tables and audit tables.
- HTML and PDF validation reports generated from the same report data model.

## Reviewer Path

For a quick review, start with:

1. [docs/architecture.md](docs/architecture.md) for the workflow and module boundaries.
2. [docs/demo_run.md](docs/demo_run.md) for local verification evidence and expected output counts.
3. [docs/mvp-prd.md](docs/mvp-prd.md) for the product decisions and v1.1/v1.2 scope.
4. [onboarding/schema.py](onboarding/schema.py) for the canonical target schema contract.
5. [onboarding/profiler.py](onboarding/profiler.py) and [onboarding/rules_mapper.py](onboarding/rules_mapper.py) for deterministic profiling and mapping.
6. [onboarding/validation.py](onboarding/validation.py) and [onboarding/transform.py](onboarding/transform.py) for validation, rejected rows, canonical outputs, and lineage.
7. [onboarding/ai_mapper.py](onboarding/ai_mapper.py) for the guarded OpenAI mapping adapter.
8. `python -m pytest` for the fastest local verification path.

## Architecture

```mermaid
flowchart LR
    A["Customer CSV"] --> B["Profile source columns"]
    B --> C["Rules or AI mapping suggestions"]
    C --> D["Human mapping approval"]
    D --> E["Source coverage review"]
    E --> F["Validate canonical flat frame"]
    F --> G["Transform accepted rows"]
    F --> H["Rejected rows with original values"]
    G --> I["members / plans / member_coverage"]
    G --> J["Field-level lineage"]
    I --> K["PostgreSQL publish"]
    H --> L["HTML / PDF / CSV exports"]
    J --> L
    K --> M["Import run audit trail"]
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

Open:

```text
http://localhost:8501
```

On Windows, you can also start the app with:

```powershell
.\run_streamlit.bat
```

Keep that terminal open while using the app.

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
   - Inspect the fixed canonical target schema before uploading data.
   - Review target tables, fields, data types, validation kinds, allowed values, and required flags.

2. **Upload**
   - Upload a CSV or load the checked-in 1,000-row demo file.

3. **Profile**
   - Inspect source column names, inferred types, null rates, cardinality, value-pattern signals, enum hints, samples, and top values.

4. **Map**
   - Generate rules-based or AI-assisted mapping suggestions.
   - Review confidence, type alignment, reasons, and flags.
   - Approve mappings and review unused source columns.
   - Save or load schema-versioned mapping templates.

5. **Validate**
   - Run deterministic validation on the mapped canonical frame.
   - Review blocking errors, warnings, target fields, source columns, and affected source row numbers.

6. **Transform**
   - Build accepted canonical outputs.
   - Preview `members`, `plans`, `member_coverage`, `rejected_rows_with_original_values`, and `field_lineage`.

7. **Publish**
   - Check PostgreSQL connectivity.
   - Run import replay/idempotency check.
   - Capture reviewer signoff.
   - Publish accepted records and audit metadata.

8. **Report**
   - Download HTML/PDF validation report.
   - Download canonical CSVs.
   - Download rejected-row and lineage exports.

## Key Outputs

Canonical outputs:

- `members.csv`
- `plans.csv`
- `member_coverage.csv`

Exception and audit outputs:

- `rejected_rows_with_original_values.csv`
- `field_lineage.csv`
- `validation_report.html`
- `validation_report.pdf`

PostgreSQL tables:

- Canonical: `members`, `plans`, `member_coverage`
- Audit: `import_runs`, `mapping_decisions`, `source_column_audit`, `validation_issues`, `rejected_rows`

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

## Production-Style Vs. Demo-Limited

Production-style pieces:

- Explicit target schema contract.
- Human approval gates before validation and publish.
- Rules and LLM mapping modes with deterministic fallback.
- Source coverage review for unused columns.
- Field-level lineage for explainability.
- Rejected-row export for customer correction.
- Reviewer signoff.
- Idempotency/replay check.
- PostgreSQL audit trail.
- Test coverage for core logic.

Demo-limited pieces:

- One fixed target schema and one synthetic healthcare demo file.
- Streamlit UI instead of a role-based web frontend.
- Local JSON mapping templates instead of shared template governance.
- Synchronous processing instead of background jobs.
- No authentication, tenant isolation, or permission model.
- AI privacy guardrails are not yet implemented beyond controlled prompt payloads and human review.

## Tests

Run the core test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Optional compile check:

```powershell
.\.venv\Scripts\python.exe -m compileall app.py onboarding tests
```

Current local verification is captured in [docs/demo_run.md](docs/demo_run.md).

## Project Structure

```text
app.py
docker-compose.yml
requirements.txt
onboarding/
  schema.py
  profiler.py
  rules_mapper.py
  ai_mapper.py
  mapping_quality.py
  source_coverage.py
  mapping_templates.py
  idempotency.py
  validation.py
  transform.py
  database.py
  reports.py
  exports.py
scripts/
  generate_demo_eligibility_file.py
data/
  demo/
  mapping_templates/
docs/
  architecture.md
  demo_run.md
  mvp-prd.md
tests/
```
