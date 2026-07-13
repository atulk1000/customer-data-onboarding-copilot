# Demo Run Evidence

This page captures local verification evidence for the Customer Data Onboarding Copilot. The goal is to show that the project was run end to end, not just described architecturally.

Run date: 2026-07-12

Local stack:

- Streamlit
- pandas
- PostgreSQL in Docker
- OpenAI API path configured through environment variables
- ReportLab PDF export

## Local App

Streamlit endpoint check:

```text
http://localhost:8501 -> HTTP 200
```

The app workflow includes:

```text
Target -> Upload -> Profile -> Map -> Validate -> Transform -> Publish -> Report
```

## Demo Source File

Checked-in source:

```text
data/demo/messy_eligibility_file.csv
```

The demo generator creates 1,000 synthetic eligibility rows with intentional quality issues:

- invalid and mixed-format dates
- missing required fields
- invalid enum values
- bad emails and phone numbers
- duplicate/conflicting member identities
- invalid coverage periods
- dependent subscriber issues

The data is synthetic only and contains no PHI.

## Verification Commands

Quality gates:

```powershell
.\.venv\Scripts\python.exe -m black --check .
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m pytest
```

Result:

```text
black --check passed
ruff check passed
32 passed
```

Test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result:

```text
32 passed
```

Compile check:

```powershell
.\.venv\Scripts\python.exe -m compileall app.py onboarding tests
```

Result:

```text
compileall passed
```

## Real LLM Mapping Check

The OpenAI adapter was exercised against the configured `gpt-5-mini` model with low reasoning effort and a sanitized three-column synthetic profile. The strict response schema returned five valid source-to-target suggestions with approved-catalog transformation steps. No API key or customer-sensitive value was logged or committed.

## Rejected Rows Export Check

Export inspected:

```text
rejected_rows_with_original_values.csv
```

Observed shape:

```text
279 rows x 28 columns
```

Sanity checks:

```text
Rejected rows: 279
Unique source rows: 279
Rows with zero errors: 0
Row status: all rejected
Source row range: 2 to 1001
Original source columns included: 16
Missing error_source_columns: 0
```

Top blocking errors observed in the export:

```text
duplicate_member_id_conflicting_identity: 53
date_of_birth_missing_or_invalid: 28
coverage_status_missing_or_invalid: 27
relationship_to_subscriber_missing_or_invalid: 27
coverage_end_before_start: 27
date_of_birth_future: 27
first_name_missing_or_invalid: 27
plan_id_missing_or_invalid: 27
subscriber_id_not_found: 27
dependent_missing_subscriber_id: 16
```

The export includes blocking error metadata first, followed by warning metadata and original source values prefixed with `original__`.

## Field Lineage Check

For the 1,000-row demo and 19 target fields, the field-lineage output produces:

```text
19,000 lineage rows
```

Each lineage row includes:

- source row number
- row status
- lineage status
- target table and field
- target data type and validation kind
- mapped source column
- original value
- normalized value
- transformation applied
- issue codes and messages

V1.3 lineage also includes:

- stable source record ID
- all contributing source columns and values
- corrected values, when present
- ordered transformation trace JSON
- final target value
- exact contract and mapping-template versions

## V1.3 Database Verification

Verification command:

```powershell
.\.venv\Scripts\python.exe scripts\verify_v13_release.py --publish --verify-rollback --verify-correction
```

Observed results:

```text
Clean initial publish: pre-publish PASS, post-publish PASS, transaction committed
Exact replay: 3 members, 2 plans, and 3 coverage records classified unchanged
Reconciliation evidence per successful run: 2 stages
Field-lineage evidence for three source rows: 57 rows
Forced conflicting-key failure: blocked before write and retained as reconciliation_failed
Correction parent: 1 rejected row
Correction child: 1 recovered row, 1 persisted field correction, transaction committed
```

## V1.3 Realistic-Volume Check

Transform-only command:

```powershell
.\.venv\Scripts\python.exe scripts\verify_v13_release.py --demo-file
```

Observed local timings and results:

```text
Profile: 0.40 seconds
Mapping: 0.08 seconds
Canonical build with transformation traces: 5.11 seconds
Validation: 2.51 seconds
Transform and 19,000-row lineage: 12.15 seconds
Total core pipeline: 20.26 seconds
Accepted rows: 721
Rejected rows: 279
Reconciliation status: WARNING
Conflicting target duplicates: 0
```

The messy demo coalesces 721 repeated plan candidates into five canonical plans. A missing optional plan type does not conflict with a known value for the same plan, while two different non-null values still produce a hard integrity failure. The resulting `WARNING` is driven by the demo's intentional reject rate and requires reviewer signoff before publish. The forced rollback scenario above separately proves the hard-conflict path.

## Notes

- This is a local demo, not a production deployment benchmark.
- The project includes one built-in healthcare contract and supports imported contracts through the approved type and validation vocabulary.
- AI-assisted mapping is optional; rules-based mapping and all validation/transform/report tests run without an API key.
