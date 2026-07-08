# Demo Run Evidence

This page captures local verification evidence for the Customer Data Onboarding Copilot. The goal is to show that the project was run end to end, not just described architecturally.

Run date: 2026-07-07

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

Test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result:

```text
20 passed
```

Compile check:

```powershell
.\.venv\Scripts\python.exe -m compileall app.py onboarding tests
```

Result:

```text
compileall passed
```

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

## Notes

- This is a local demo, not a production deployment benchmark.
- The project intentionally uses one canonical schema in v1 so the workflow remains focused and reviewable.
- AI-assisted mapping is optional; rules-based mapping and all validation/transform/report tests run without an API key.
