# Customer Data Onboarding Copilot

## MVP PRD And Technical Spec

Status: Draft for v1 implementation  
Project name: Customer Data Onboarding Copilot  
Repository name: customer-data-onboarding-copilot  
Demo domain: Healthcare member eligibility onboarding  
Primary stack: Streamlit, pandas, PostgreSQL in Docker, OpenAI API, ReportLab  
Current implemented scope: MVP, v1.1 audit readiness, v1.2 exception explainability, and v1.3 contract-driven onboarding
Next release specification: [v1.3 Contract-Driven Onboarding And Reconciliation](v1.3-prd.md)

## 1. Product Summary

Customer Data Onboarding Copilot is a data onboarding platform for turning messy customer files into validated, canonical, production-ready records.

The MVP focuses on one realistic demo: a healthcare member eligibility CSV uploaded by a customer or implementation team. The app profiles the file, suggests source-to-target mappings using either deterministic rules or AI-assisted mapping, requires human approval, validates the data, transforms accepted rows into canonical tables, reconciles outcomes, publishes accepted records to PostgreSQL, and generates customer-facing HTML and PDF validation reports.

The product should feel like an implementation control plane, not a generic CSV utility.
For v1, the target schema is fixed by the product and should be visible in the app before the user uploads or maps a source file. The source file is customer-provided; the target is the canonical onboarding contract.

## 2. Problem

Customer implementation, FDE, Solutions Engineering, and Data Solutions teams often receive messy customer files that do not match the product's internal schema. Teams must interpret ambiguous columns, validate data quality, normalize values, reject bad records, explain issues to customers, and produce sign-off artifacts before go-live.

Common pain points:

- Customer files use inconsistent column names.
- Required fields may be missing, malformed, or ambiguous.
- Source data often mixes member, plan, and coverage information in one flat file.
- Manual mapping decisions are hard to audit.
- Validation failures need to be explainable to both internal teams and customers.
- Teams need accepted rows, rejected rows, reconciliation counts, and a sign-off report.

## 3. MVP Goals

The MVP must demonstrate an end-to-end customer data onboarding workflow:

- Upload or load a 1,000-row synthetic messy eligibility CSV.
- Profile source columns and display useful data quality signals.
- Support two mapping modes:
  - Rules-Based Mapping
  - AI-Assisted Mapping
- Require human review and approval of mapping decisions.
- Track source coverage so unused source columns are visible and explicitly reviewed.
- Capture reviewer signoff with comments before publish.
- Save and load schema-versioned mapping templates for repeat files.
- Validate mapped records with blocking errors and warnings.
- Normalize healthcare eligibility values such as status, gender, relationship, and plan type.
- Transform one flat source file into three canonical tables:
  - members
  - plans
  - member_coverage
- Produce rejected rows with row-level reasons and original source values.
- Produce field-level lineage showing source value, normalized value, and transformation applied.
- Show reconciliation metrics.
- Publish accepted canonical records and audit data to PostgreSQL.
- Generate downloadable HTML and PDF validation reports from the same report data model.
- Keep the project generic enough for healthcare and non-healthcare recruiters while using healthcare as the concrete demo domain.

## 4. Non-Goals For MVP

The MVP will not include:

- Real PHI or real customer data.
- Multi-file onboarding packages.
- Excel, JSON, or EDI 834 parsing.
- SFTP ingestion.
- Background jobs or scheduled recurring imports.
- Authentication, roles, or multi-tenant account management.
- A Next.js frontend.
- Fully configurable schemas across arbitrary industries.
- Automated publishing without human mapping approval.
- AI-generated transformations that execute directly.
- Full PDF rendering parity with HTML tables for every row in very large rejects.

## 5. Primary Users

Primary persona:

- Implementation Manager
- Solutions Engineer
- Forward Deployed Engineer
- Data Onboarding Analyst
- Data Solutions Engineer

User needs:

- Quickly understand a customer's file.
- Map ambiguous columns into a canonical model.
- See why mappings were suggested.
- Review and approve mappings before transformation.
- Validate records before publish.
- Explain rejected rows to a customer.
- Produce a sign-off artifact for implementation.

## 6. Demo Scenario

A healthcare SaaS customer sends a flat eligibility CSV exported from a benefits admin or HRIS system. The file includes employees and dependents, plan information repeated on each row, coverage dates, status values, and relationship values.

The file is synthetic but intentionally messy:

- 1,000 rows.
- Mixed date formats.
- Status aliases such as A, Active, T, Termed, Pending.
- Relationship aliases such as Self, Subscriber, Spouse, Child, Dependent.
- Plan type variants such as PPO, P.P.O, HMO, HDHP, High Deductible.
- Missing required fields.
- Bad emails and phone numbers.
- Invalid dates.
- Coverage end dates before start dates.
- Duplicate member IDs.
- Conflicting identity values.
- Dependents missing subscriber IDs.
- Unknown enum values.

## 7. MVP Workflow

The Streamlit app should use a guided step-based workflow. The navigation may look tab-like, but inactive steps must not render their page content.

1. Target
   - Show a target schema dropdown backed by the product schema registry.
   - For v1, include one selectable schema: Healthcare Eligibility Canonical v1.
   - Show an output table dropdown so users can inspect one target table at a time.
   - Show canonical target tables, fields, required status, data types, nullability, validation kinds, allowed values, and descriptions.

2. Upload
   - Upload a CSV.
   - Or load the built-in 1,000-row demo eligibility file.

3. Profile
   - Show row count, column count, inferred types, null rates, unique rates, top values, and sample rows.

4. Map
   - Select Rules-Based Mapping or AI-Assisted Mapping.
   - Generate mapping suggestions.
   - Review, edit, and approve source-to-target mappings.

5. Validate
   - Run deterministic validations.
   - Show blocking errors, warnings, issue buckets, and affected row counts.

6. Transform
   - Show output counts.
   - Use an output table dropdown to preview members, plans, member_coverage, rejected rows with original values, or field_lineage one table at a time.

7. Publish
   - Write accepted canonical records and audit data to PostgreSQL.

8. Report
   - Download canonical CSV outputs.
   - Download rejected rows with original values CSV.
   - Download field-level lineage CSV.
   - Download HTML validation report.
   - Download PDF validation report.

## 8. Canonical Schema

The canonical schema is fixed for v1 and uses clean snake_case field names.
Each target field must declare its expected data type, nullability, and validation kind before mapping starts. This target type contract drives rules-based mapping scores, AI prompt context, mapping validation, row validation, and report explanations. The target schema must also carry a stable schema name and version, such as `Healthcare Eligibility Canonical v1` and `1.0.0`, so mapping templates, reports, and import runs remain explainable after future schema changes.

### 8.1 members

| Field | Required | Type | Nullable | Validation kind | Allowed values | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| member_id | Yes | identifier | No | member_identifier |  | Stable unique identifier for the member/person. |
| first_name | Yes | text | No | person_name |  | Member first name. |
| last_name | Yes | text | No | person_name |  | Member last name. |
| date_of_birth | Yes | date | No | date_of_birth |  | Parsed and normalized to YYYY-MM-DD. |
| gender | No | enum | Yes | gender | male, female, other, unknown | Normalized enum. |
| email | No | email | Yes | email |  | Warning if invalid. |
| phone | No | phone | Yes | phone |  | Warning if invalid. |

### 8.2 plans

| Field | Required | Type | Nullable | Validation kind | Allowed values | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| plan_id | Yes | identifier | No | plan_identifier |  | Stable plan code or identifier. |
| plan_name | Yes | text | No | plan_name |  | Human-readable plan name. |
| plan_type | No | enum | Yes | plan_type | PPO, HMO, EPO, HDHP, POS | Normalized enum. |
| carrier_name | No | text | Yes | organization_name |  | Carrier or payer name. |

### 8.3 member_coverage

| Field | Required | Type | Nullable | Validation kind | Allowed values | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| coverage_id | Generated | identifier | No | generated_identifier |  | Generated by the app. |
| member_id | Yes | identifier | No | member_identifier |  | References members.member_id. |
| plan_id | Yes | identifier | No | plan_identifier |  | References plans.plan_id. |
| coverage_start_date | Yes | date | No | coverage_start_date |  | Parsed and normalized to YYYY-MM-DD. |
| coverage_end_date | No | date | Yes | coverage_end_date |  | Parsed and normalized when present. |
| coverage_status | Yes | enum | No | coverage_status | active, terminated, pending, cancelled | Normalized enum. |
| relationship_to_subscriber | Yes | enum | No | relationship_to_subscriber | self, spouse, child, dependent | Normalized enum. |
| subscriber_id | No | identifier | Yes | subscriber_identifier |  | Required when relationship is not self. |

### 8.4 Target Type Alignment

The app must compare source profile evidence against each target field's declared type before suggesting a mapping.

Alignment examples:

- date target fields require source columns with high date parse rates.
- email target fields require email-like patterns.
- phone target fields require phone-like patterns after cleanup.
- enum target fields require known value normalization or low-cardinality enum-like values.
- identifier target fields require ID-like values, uniqueness, or repeat patterns appropriate to the validation kind.
- text target fields should reject date, email, phone, or numeric-looking columns as contradictory evidence.

Mapping suggestions should include `target_data_type` and `target_validation_kind` so reviewers can see the expected target contract next to the proposed source column. The mapping review should also show the source column's inferred type, a type alignment status, and an alignment reason. Approved mappings with a hard type mismatch, such as a non-date source mapped to a date target, must block validation until the reviewer chooses a better source column.

## 9. Source Column Normalization

Source columns can contain spaces, punctuation, mixed casing, underscores, dashes, or slashes. The app should preserve original source column names for display and audit, but create a normalized comparison form for matching.

Normalization for matching:

- Lowercase.
- Trim leading and trailing whitespace.
- Replace underscores, dashes, and slashes with spaces.
- Remove punctuation.
- Collapse repeated whitespace.
- Expand common abbreviations when useful.

Examples:

| Source column | Normalized for matching |
| --- | --- |
| Member Number | member number |
| MEMBER_NO | member no |
| member-number | member number |
| Member/Number | member number |
| DOB | dob |
| Effective Date | effective date |

Canonical target fields and database columns must use snake_case.

## 10. Source Profiler

The source profiler computes column intelligence used by both mapping modes.

For each column, compute:

- column_name
- normalized_name
- inferred_type
- non_null_count
- null_rate
- unique_count
- unique_rate
- sample_values
- top_values
- date_parse_rate
- email_pattern_rate
- phone_pattern_rate
- numeric_parse_rate
- known_enum_matches
- min_date
- max_date

The profiler should limit sample values for display and AI context. The demo dataset is synthetic, so raw samples are acceptable in v1.

Example profile:

```json
{
  "column_name": "Status",
  "normalized_name": "status",
  "inferred_type": "enum",
  "null_rate": 0.02,
  "unique_count": 3,
  "unique_rate": 0.003,
  "sample_values": ["Active", "Termed", "Pending"],
  "top_values": {
    "Active": 820,
    "Termed": 140,
    "Pending": 40
  },
  "date_parse_rate": 0.0,
  "email_pattern_rate": 0.0,
  "phone_pattern_rate": 0.0,
  "known_enum_matches": {
    "coverage_status": ["active", "terminated", "pending"]
  }
}
```

## 11. Rules-Based Mapping

Rules-based mapping should be deterministic, explainable, and auditable.

### 11.1 Alias Dictionary

The alias dictionary maps canonical target fields to common source column names.

Example aliases:

```python
FIELD_ALIASES = {
    "member_id": [
        "member id", "member number", "member no", "mbr id",
        "patient id", "person id", "employee id"
    ],
    "first_name": [
        "first name", "first", "fname", "given name"
    ],
    "last_name": [
        "last name", "last", "lname", "surname", "family name"
    ],
    "date_of_birth": [
        "dob", "date of birth", "birth date", "birthdate",
        "member dob", "patient birthdate"
    ],
    "gender": [
        "gender", "sex"
    ],
    "email": [
        "email", "email address", "member email"
    ],
    "phone": [
        "phone", "phone number", "mobile", "cell"
    ],
    "plan_id": [
        "plan id", "plan code", "benefit plan", "product code"
    ],
    "plan_name": [
        "plan", "plan name", "benefit name", "product name"
    ],
    "plan_type": [
        "plan type", "product type", "network type"
    ],
    "carrier_name": [
        "carrier", "carrier name", "payer", "payer name"
    ],
    "coverage_start_date": [
        "effective date", "eff date", "start date",
        "coverage start date", "eligibility start date"
    ],
    "coverage_end_date": [
        "term date", "termination date", "end date",
        "coverage end date", "eligibility end date"
    ],
    "coverage_status": [
        "status", "coverage status", "eligibility status",
        "enrollment status"
    ],
    "relationship_to_subscriber": [
        "relationship", "relation", "relationship to subscriber",
        "dependent relationship"
    ],
    "subscriber_id": [
        "subscriber id", "subscriber number", "primary member id",
        "employee id"
    ],
}
```

### 11.2 Scoring Model

Use the same scoring framework for every target field, but field-specific value/profile heuristics.

Final confidence:

```text
Name match score:      0 to 70
Value/profile score:   0 to 30
Ambiguity penalty:     0 to -20
Conflict penalty:      0 to -10
Final confidence:      0 to 100
```

Name match score:

```text
70 = exact alias or canonical match
60 = strong fuzzy match
45 = partial token match
25 = weak token overlap
0  = no meaningful name match
```

Value/profile score:

```text
30 = strong evidence
20 = useful evidence
10 = weak evidence
0  = no evidence
-10 = contradictory evidence
```

Decision bands:

```text
85 to 100 = strong suggestion
70 to 84  = suggestion, needs review
50 to 69  = weak suggestion
<50       = unmapped
```

Even a high-confidence mapping can require review if the source column is business-ambiguous.

### 11.3 Field-Specific Value Heuristics

| Target field | Strong value/profile evidence |
| --- | --- |
| member_id | Mostly non-null, high uniqueness, ID-like values. |
| first_name | Text-like values, not date-like, not email-like, not high-cardinality IDs. |
| last_name | Text-like values, not date-like, not email-like, not high-cardinality IDs. |
| date_of_birth | High date parse rate and dates imply realistic ages from 0 to 120. |
| gender | Values normalize to male, female, other, unknown. |
| email | High email pattern rate. |
| phone | High phone pattern rate after cleanup. |
| plan_id | Code-like values, repeated across rows, low or moderate uniqueness. |
| plan_name | Repeated descriptive plan names. |
| plan_type | Values normalize to PPO, HMO, EPO, HDHP, POS, other. |
| carrier_name | Repeated organization-like text values. |
| coverage_start_date | High date parse rate and plausible eligibility start dates. |
| coverage_end_date | Date-like values, nullable allowed. |
| coverage_status | Values normalize to active, terminated, pending, cancelled. |
| relationship_to_subscriber | Values normalize to self, spouse, child, dependent. |
| subscriber_id | ID-like values, may repeat across dependent rows. |

### 11.4 Ambiguity Rules

Some source names should trigger review even when confidence is high:

| Ambiguous source term | Reason |
| --- | --- |
| ID | Could be member, subscriber, plan, employee, or internal row ID. |
| Employee ID | Could be member_id for employees or subscriber_id for dependents. |
| Subscriber ID | Could be member_id for self rows or subscriber_id for dependent rows. |
| Status | Could mean employment, eligibility, member, coverage, or plan status. |
| Date | Too generic. |
| Start Date | Could mean hire date, plan start, or coverage start. |
| End Date | Could mean employment end, plan end, or coverage end. |
| Type | Could mean plan type, member type, relationship type, or coverage type. |
| Code | Could mean plan code, group code, status code, or internal ID. |

### 11.5 Rules-Based Mapping Output

Each mapping suggestion must include:

```json
{
  "target_table": "member_coverage",
  "target_field": "coverage_status",
  "target_data_type": "enum",
  "target_validation_kind": "coverage_status",
  "source_column": "Status",
  "source_inferred_type": "enum",
  "type_alignment": "aligned",
  "type_alignment_reason": "Values normalize to known coverage_status values.",
  "confidence": 82,
  "mapping_status": "suggested",
  "needs_review": true,
  "review_flags": ["ambiguous_status"],
  "reason": "Column name matched a coverage_status alias and values match known coverage statuses.",
  "review_reason": "Status can refer to employment, member, eligibility, or coverage status."
}
```

## 12. AI-Assisted Mapping

AI-assisted mapping should use a real LLM in v1 when OPENAI_API_KEY is configured. It should use the same source profiler context as rules-based mapping plus the expected target shape and scoring guidance.

### 12.1 AI Trust Boundary

The LLM may:

- Suggest mappings.
- Explain rationale.
- Flag ambiguity.
- Recommend transformation hints.

The LLM may not:

- Approve mappings.
- Validate rows.
- Transform data.
- Publish data.
- Modify database records.

Validation, transformation, reconciliation, and publish are deterministic.

### 12.2 AI Input Payload

Send sanitized column intelligence, not the entire file.

Payload should include:

- Task description.
- Target schema.
- Required vs optional fields.
- Field descriptions.
- Expected evidence per target field.
- Allowed enum values.
- Scoring and review guidance.
- Source column profiles.
- Optional rules-based suggestions for comparison.
- Known ambiguous terms.

Example:

```json
{
  "task": "Map customer healthcare eligibility file columns into the canonical target schema.",
  "target_schema": [
    {
      "table": "members",
      "field": "member_id",
      "required": true,
      "data_type": "identifier",
      "nullable": false,
      "validation_kind": "member_identifier",
      "allowed_values": [],
      "description": "Stable unique identifier for the member/person.",
      "expected_evidence": ["high uniqueness", "ID-like values", "member id aliases"]
    }
  ],
  "scoring_guidance": {
    "name_match_score": "0-70",
    "value_profile_score": "0-30",
    "ambiguity_penalty": "0 to -20",
    "conflict_penalty": "0 to -10",
    "confidence_meaning": "0-100 likelihood that source_column maps to target_field",
    "review_rule": "Set needs_review true for ambiguous healthcare terms even when confidence is high."
  },
  "source_columns": [
    {
      "column_name": "DOB",
      "normalized_name": "dob",
      "inferred_type": "date",
      "null_rate": 0.01,
      "unique_rate": 0.94,
      "sample_values": ["1988-04-12", "1975-09-03", "2001-01-18"],
      "top_values": {},
      "date_parse_rate": 0.99,
      "email_pattern_rate": 0.0,
      "phone_pattern_rate": 0.0,
      "known_enum_matches": {}
    }
  ]
}
```

### 12.3 AI Output Contract

The LLM must return structured JSON.

Example:

```json
{
  "mappings": [
    {
      "target_table": "members",
      "target_field": "date_of_birth",
      "source_column": "DOB",
      "confidence": 97,
      "needs_review": false,
      "review_flags": [],
      "rationale": "DOB is a common abbreviation for date of birth and values are parseable dates in a realistic member age range.",
      "transformation_hint": "Parse as date and output as YYYY-MM-DD."
    }
  ],
  "unmapped_required_fields": [],
  "ambiguous_mappings": []
}
```

### 12.4 OpenAI Integration

Use the existing preferred project pattern:

- OPENAI_API_KEY from environment or .env.
- OPENAI_MODEL from environment or default model.
- OpenAI client wrapper.
- Structured Responses API output where possible.
- Clear configuration error when AI mode is selected without an API key.

If OPENAI_API_KEY is missing:

- Rules-Based Mapping remains available.
- AI-Assisted Mapping should show a clear setup message.
- Do not pretend to run mock AI.

## 13. Mapping Review UI

The mapping review screen should display one row per canonical target field:

- target_table
- target_field
- target_data_type
- target_validation_kind
- required
- suggested_source_column
- source_inferred_type
- type_alignment
- type_alignment_reason
- confidence
- mapping_mode
- needs_review
- review_flags
- reason/rationale
- editable source column dropdown
- approval checkbox

Rules:

- Required fields must be mapped before validation can proceed.
- Users can override suggestions.
- Users must approve mappings before validation.
- Approved mappings with hard target/source type mismatches must block validation.
- Unused source columns must be visible in source coverage and explicitly accepted before validation.
- Approved mappings are persisted to mapping_decisions after publish.

### 13.1 Source Coverage / Unused Columns

V1.1 must show a source coverage audit in the Map step.

For every source column, show:

- source_column
- normalized_name
- inferred_type
- null_rate
- unique_rate
- coverage_status
- mapped_targets
- approved_targets
- review_recommendation

Coverage statuses:

- approved_mapped: source column is used by at least one approved target mapping.
- suggested_mapped: source column is suggested but not approved.
- unused: source column is not used by any target mapping.

If unused columns exist, the reviewer must explicitly confirm that unused source columns were reviewed and accepted before validation can run.

### 13.2 Mapping Templates

V1.1 must support local mapping template save/load.

Template rules:

- Templates are tied to target_schema_name and target_schema_version.
- A saved template stores source columns and source-to-target mappings.
- Loading a template pre-fills mappings but does not auto-approve them.
- If a template source column is missing in the current file, that target field becomes unmapped and requires review.
- Template name should be stored with reports and import runs.

## 14. Validation

Validation runs after mappings are approved.

Validation must also require source coverage review when unused source columns exist.

### 14.1 Blocking Errors

Rows with blocking errors should go to the reject file unless corrected.

Blocking errors:

- member_id is missing.
- first_name is missing.
- last_name is missing.
- date_of_birth is missing or invalid.
- date_of_birth is in the future.
- member age is outside 0 to 120.
- plan_id is missing.
- plan_name is missing.
- coverage_start_date is missing or invalid.
- coverage_end_date is before coverage_start_date.
- coverage_status is missing or not recognized.
- relationship_to_subscriber is missing or not recognized.
- duplicate member_id with conflicting identity values.
- dependent row missing subscriber_id.
- subscriber_id references no known subscriber/member.

### 14.2 Warnings

Rows with warnings may still be accepted.

Warnings:

- email is missing.
- email format is invalid.
- phone format is invalid.
- gender is missing or unknown.
- coverage_end_date is blank for active coverage.
- coverage_status is terminated but coverage_end_date is blank.
- plan_type is missing or unknown.
- same member appears more than once with same plan and date range.
- plan_id has conflicting plan_name or plan_type across rows.

### 14.3 Normalization Rules

Normalize common values before validation.

Gender:

| Source values | Canonical value |
| --- | --- |
| M, Male | male |
| F, Female | female |
| O, Other | other |
| U, Unknown | unknown |

Coverage status:

| Source values | Canonical value |
| --- | --- |
| A, Active | active |
| T, Term, Termed, Terminated | terminated |
| P, Pending | pending |
| Cancelled, Canceled, C | cancelled |

Relationship to subscriber:

| Source values | Canonical value |
| --- | --- |
| Self, Subscriber, Employee | self |
| Spouse | spouse |
| Child | child |
| Dependent | dependent |

Plan type:

| Source values | Canonical value |
| --- | --- |
| PPO, P.P.O, PPO Plan | PPO |
| HMO, H.M.O | HMO |
| EPO, E.P.O | EPO |
| HDHP, High Deductible | HDHP |
| POS | POS |

## 15. Transformation

Transformation converts one flat accepted source file into three canonical outputs.

### 15.1 members Output

One row per unique member.

Fields:

- member_id
- first_name
- last_name
- date_of_birth
- gender
- email
- phone

Deduplication rule:

- If the same member_id appears multiple times with the same identity values, keep one member row.
- If the same member_id appears with conflicting name or date_of_birth, reject or flag conflicting source rows.

### 15.2 plans Output

One row per unique plan.

Fields:

- plan_id
- plan_name
- plan_type
- carrier_name

Deduplication rule:

- If the same plan_id appears multiple times with the same plan attributes, keep one plan row.
- If the same plan_id has conflicting plan_name or plan_type, warn or reject depending severity.

### 15.3 member_coverage Output

One row per accepted coverage relationship.

Fields:

- coverage_id
- member_id
- plan_id
- coverage_start_date
- coverage_end_date
- coverage_status
- relationship_to_subscriber
- subscriber_id

coverage_id generation:

- Use a deterministic readable ID generated from the coverage business key.
- V1.1 business key: member_id + plan_id + coverage_start_date.
- This makes reruns less sensitive to source row order and allows PostgreSQL upserts to target the same coverage record.

### 15.4 rejected_rows Output

Rejected rows should include:

- source_row_number
- row_status
- error_count
- error_codes
- error_target_fields
- error_source_columns
- errors
- warning_count
- warning_codes
- warning_target_fields
- warning_source_columns
- warnings
- original source columns, prefixed with original__

This export is the customer-correction work queue. It should preserve the original values exactly enough for an implementation team or customer to identify and correct the rejected records.

### 15.5 field_lineage Output

V1.2 must produce field-level lineage as a separate export.

One row should exist per source row and target field.

Fields:

- source_row_number
- row_status
- lineage_status
- target_table
- target_field
- target_data_type
- target_validation_kind
- source_column
- original_value
- normalized_value
- transformation_applied
- issue_codes
- issue_messages

Lineage statuses:

- accepted: field belongs to an accepted row and has no field-level issue.
- warning: field has a warning but the row may still be accepted.
- error: field has a blocking validation error.
- not_published_row_rejected: field is not itself the failing field, but the row is rejected because another field has a blocking error.

The lineage export should make it possible to answer: "Where did this target value come from, what did the app do to it, and why was it accepted or rejected?"

## 16. Reconciliation

The reconciliation view should show:

- source rows
- accepted rows
- rejected rows
- warning count
- members created
- plans created
- coverage records created
- blocking errors by type
- warnings by type

The key invariant:

```text
source rows = accepted rows + rejected rows
```

## 17. PostgreSQL Persistence

PostgreSQL should run via Docker Compose.

### 17.1 Canonical Tables

- members
- plans
- member_coverage

### 17.2 Audit Tables

- import_runs
- mapping_decisions
- source_column_audit
- validation_issues
- rejected_rows

### 17.3 import_runs

Fields:

- id
- file_name
- mapping_mode
- started_at
- completed_at
- source_file_hash
- is_replay
- previous_import_run_id
- replay_acknowledged
- source_row_count
- accepted_row_count
- rejected_row_count
- warning_count
- target_schema_name
- target_schema_version
- mapping_template_name
- source_coverage_reviewed
- signoff_reviewer_name
- signoff_reviewer_role
- signoff_decision
- signoff_comment
- signoff_at
- status

### 17.4 mapping_decisions

Fields:

- id
- import_run_id
- target_table
- target_field
- target_data_type
- target_validation_kind
- source_inferred_type
- type_alignment
- type_alignment_reason
- source_column
- confidence
- mapping_mode
- approved
- needs_review
- reason

### 17.5 source_column_audit

Fields:

- id
- import_run_id
- source_column
- normalized_name
- inferred_type
- null_rate
- unique_rate
- coverage_status
- mapped_targets
- approved_targets
- review_recommendation

### 17.6 validation_issues

Fields:

- id
- import_run_id
- source_row_number
- severity
- issue_code
- issue_message
- target_field
- source_column

### 17.7 rejected_rows

Fields:

- id
- import_run_id
- source_row_number
- raw_payload_json
- error_summary

### 17.8 Publish Behavior

For v1:

- Create one import_run per publish.
- Compute and persist a source_file_hash for the uploaded source dataframe.
- Persist approved mapping decisions.
- Persist source column coverage audit.
- Persist reviewer signoff metadata.
- Persist target schema name, target schema version, and mapping template name.
- Persist validation issues and rejected rows.
- Upsert canonical records by natural keys:
  - members.member_id
  - plans.plan_id
  - member_coverage.coverage_id
- Keep import_run_id available for audit linkage where useful.

### 17.9 Import Replay / Idempotency Check

V1.1 must run an import replay check before publish when PostgreSQL is connected.

Rerun behavior:

- Every publish creates a new import_run audit record, even if the source file was already published.
- The app computes a SHA-256 source_file_hash from the uploaded dataframe.
- Before publish, the app checks import_runs for the same source_file_hash.
- If a prior import exists, the Publish step must show the previous import_run_id and require reviewer acknowledgement before allowing publish.
- members are upserted by member_id.
- plans are upserted by plan_id.
- member_coverage is upserted by deterministic coverage_id generated from member_id + plan_id + coverage_start_date.
- Mapping decisions, source coverage audit, validation issues, rejected rows, and reviewer signoff are still recorded for the new import_run.

V1.1 does not yet compute full inserted/updated/unchanged row deltas. That is a future enhancement.

## 18. Reports

The report is the customer-facing sign-off artifact.

V1 must generate:

- Downloadable HTML report.
- Downloadable PDF report.

Both reports must use the same report data model.

### 18.1 Report Data Model

The report data model should include:

- import_summary
- mapping_summary
- source_coverage_summary
- source_coverage_detail
- validation_summary
- reconciliation_summary
- reviewer_signoff
- rejected_rows_preview
- signoff_status

### 18.2 HTML Report

HTML report should include:

1. Import Summary
2. Mapping Summary
3. Source Coverage
4. Validation Results
5. Reconciliation
6. Reviewer Signoff
7. Rejected Rows Summary
8. Sign-Off Status

HTML can show fuller tables.

### 18.3 PDF Report

PDF report should include the same sections and same content categories as HTML.

For v1:

- Use ReportLab.
- Keep the layout clean and summary-focused.
- Include first 25 rejected rows.
- Full rejected rows remain available as CSV.
- Wide tables should use selected summary columns and wrapped cell text instead of truncating values.

### 18.4 Sign-Off Status

Suggested statuses:

- Ready for publish: no blocking errors.
- Needs customer correction: rejected rows exist.
- Published to PostgreSQL: publish completed successfully.

## 19. Demo Dataset

Create a script:

```text
scripts/generate_demo_eligibility_file.py
```

It should generate:

```text
data/demo/messy_eligibility_file.csv
```

Requirements:

- 1,000 rows.
- Synthetic only.
- Realistic healthcare eligibility shape.
- Deterministic output using a random seed.
- Includes clean rows and intentional data quality issues.

Source columns:

- Member Number
- First
- Last
- DOB
- Sex
- Email Address
- Phone
- Plan Code
- Plan Name
- Plan Type
- Carrier
- Effective Date
- Term Date
- Status
- Relation
- Subscriber ID

Intentional issue types:

- Missing required fields.
- Invalid date_of_birth.
- Future date_of_birth.
- Coverage end before start.
- Invalid email.
- Invalid phone.
- Unknown status.
- Unknown relationship.
- Unknown plan type.
- Duplicate member ID with conflicting identity.
- Dependent missing subscriber ID.
- Subscriber ID not found.
- Mixed date formats.

## 20. Streamlit UI Requirements

General UI:

- Keep it workflow-oriented and implementation-tool focused.
- Avoid a marketing landing page.
- Use concise status metrics and tables.
- Use step-like navigation that renders only the active step.
- Make each step's next action obvious.

Workflow steps:

- Target
- Upload
- Profile
- Map
- Validate
- Transform
- Publish
- Report

Target step:

- Target schema dropdown.
- Output table dropdown.
- Canonical schema table.
- Counts for target tables, fields, required fields, and generated fields.
- Compact typography and column widths so schema fields remain readable.

Upload step:

- CSV uploader.
- Button to load demo file.
- Show file shape and preview.

Profile step:

- Source column profile table.
- Sample rows.
- Top values for selected column.

Map step:

- Mapping mode selector.
- Generate suggestions button.
- Mapping review editor.
- Source coverage panel with unused source column review.
- Mapping template load/save controls.
- Approval checkboxes.

Validate step:

- Run validation button.
- Summary metrics.
- Blocking error table.
- Warning table.

Transform step:

- Show output counts.
- Use an output table dropdown.
- Preview the selected output table.

Publish step:

- PostgreSQL connection status.
- Import replay/idempotency check with source file fingerprint.
- Replay acknowledgement checkbox when the same source file was previously published.
- Reviewer signoff form with reviewer name, role/team, decision, comment, and timestamp.
- Publish button.
- Import run summary after publish.

Report step:

- HTML report preview.
- Download HTML.
- Download PDF.
- Download canonical CSVs.
- Download rejected rows with original values CSV.
- Download field-level lineage CSV.

## 21. Configuration

Use environment variables and .env support.

Required for full functionality:

```text
DATABASE_URL=postgresql+psycopg://onboarding:onboarding@localhost:55432/onboarding
OPENAI_API_KEY=...
```

Optional:

```text
OPENAI_MODEL=gpt-5-mini
OPENAI_REASONING_EFFORT=low
```

AI mode requires OPENAI_API_KEY. Rules-based mode does not.

## 22. Proposed Project Structure

```text
customer-data-onboarding-copilot/
  app.py
  docker-compose.yml
  requirements.txt
  README.md
  .env.example
  docs/
    mvp-prd.md
  onboarding/
    __init__.py
    schema.py
    profiler.py
    rules_mapper.py
    mapping_quality.py
    source_coverage.py
    mapping_templates.py
    idempotency.py
    ai_mapper.py
    validation.py
    transform.py
    database.py
    reports.py
    exports.py
  scripts/
    generate_demo_eligibility_file.py
  data/
    demo/
      messy_eligibility_file.csv
  tests/
    test_profiler.py
    test_rules_mapper.py
    test_mapping_quality.py
    test_source_coverage.py
    test_mapping_templates.py
    test_idempotency.py
    test_validation.py
    test_transform.py
    test_reports.py
```

## 23. Testing Strategy

Minimum tests for v1:

- Profiler detects date, email, phone, enum, and ID-like columns.
- Rules-based mapper scores obvious mappings highly.
- Rules-based mapper flags ambiguous fields such as Status and Employee ID.
- Validation rejects missing required fields.
- Validation rejects invalid dates and impossible date ranges.
- Validation warns on bad email and phone.
- Transformation produces deduped members and plans.
- Transformation produces coverage rows and rejected rows.
- Transformation produces field-level lineage.
- Report generation returns non-empty HTML and PDF bytes.
- Demo data generator creates exactly 1,000 rows.

## 24. Acceptance Criteria

MVP is complete when:

- The repo can be installed locally from a fresh checkout.
- Docker Compose starts PostgreSQL.
- Streamlit app starts successfully.
- App clearly shows the fixed target schema before source upload and mapping.
- User can load the 1,000-row demo CSV.
- App profiles source columns.
- Rules-Based Mapping produces explainable mapping suggestions.
- AI-Assisted Mapping calls a real LLM when OPENAI_API_KEY is configured.
- Mapping review allows manual edits and approval.
- Source coverage shows mapped, suggested-only, and unused source columns.
- Validation blocks when unused source columns have not been reviewed.
- Mapping templates can be saved and loaded for the current target schema version.
- Validation produces blocking errors and warnings.
- Transformation outputs members, plans, member_coverage, rejected rows with original values, and field_lineage.
- Coverage IDs are deterministic for the same member_id, plan_id, and coverage_start_date.
- Reconciliation metrics are visible and internally consistent.
- Publish writes canonical and audit records to PostgreSQL.
- Publish detects exact source file replays and requires acknowledgement before republishing.
- Publish stores schema version, template name, source coverage audit, and reviewer signoff.
- HTML report is downloadable.
- PDF report is downloadable.
- HTML and PDF reports include source coverage and reviewer signoff.
- Canonical CSVs, rejected rows with original values CSV, and field-level lineage CSV are downloadable.
- Tests pass for core profiler, mapper, validation, transform, and report paths.

## 25. Implementation Milestones

### Milestone 1: Project Scaffold And Demo Data

- Create project structure.
- Add requirements, .env.example, docker-compose.yml.
- Add demo data generator.
- Generate 1,000-row messy eligibility CSV.

### Milestone 2: Schema, Profiler, And Rules Mapping

- Define canonical schema.
- Implement source profiler.
- Implement alias dictionary.
- Implement rules-based scoring.
- Add mapping output contract.

### Milestone 3: Streamlit Upload/Profile/Map UI

- Build upload step.
- Build profile step.
- Build mapping mode selector.
- Build mapping review editor.

### Milestone 4: Validation And Transformation

- Implement normalization.
- Implement blocking errors and warnings.
- Implement canonical transformations.
- Implement rejected row output.

### Milestone 5: PostgreSQL Publish

- Add Dockerized PostgreSQL.
- Create canonical and audit tables.
- Implement publish flow.
- Add import run audit records.

### Milestone 6: AI-Assisted Mapping

- Add OpenAI client wrapper.
- Add structured AI mapping request and response.
- Integrate AI suggestions into mapping review UI.
- Add clear missing-key behavior.

### Milestone 7: Reports And Exports

- Add report data model.
- Generate HTML report.
- Generate PDF report with ReportLab.
- Add download buttons for reports and CSV outputs.

### Milestone 8: README And Tests

- Add project README.
- Add setup and demo instructions.
- Add test coverage for core modules.
- Add screenshots later if desired.

### Milestone 9: V1.1 Audit Readiness

- Add source coverage and unused source column review.
- Add reviewer signoff with comments.
- Add schema version to app, reports, templates, and import runs.
- Add mapping template save/load.
- Add import replay/idempotency check before publish.
- Persist source column audit and signoff metadata to PostgreSQL.

### Milestone 10: V1.2 Exception Explainability

- Expand rejected row export with issue codes, target fields, mapped source columns, and original source values.
- Add field-level lineage export showing source value to normalized value for every target field.
- Add lineage preview to reports.
- Add tests for rejected row export metadata and field-level lineage.

## 26. Risks And Mitigations

Risk: Scope expands into a general schema mapping platform.  
Mitigation: Keep v1 fixed to one demo domain and one canonical schema.

Risk: AI output is inconsistent.  
Mitigation: Require structured JSON, validate output, and keep human approval mandatory.

Risk: PDF layout becomes time-consuming.  
Mitigation: Use the same report data model and keep PDF summary-focused.

Risk: PostgreSQL publish adds complexity.  
Mitigation: Keep schema simple and use import_run audit records.

Risk: Mapping scores feel arbitrary.  
Mitigation: Show score components, reasons, and review flags.

Risk: Healthcare domain seems too narrow.  
Mitigation: Use generic product and repo name, with healthcare as one concrete demo workflow.

## 27. Future Roadmap

Post-MVP enhancements:

- Excel and JSON upload.
- Multi-file onboarding packages.
- Template governance, sharing, and permissions across customers/vendors.
- Import history screen with run comparison.
- EDI 834 parsing.
- SFTP ingestion.
- API publish target.
- Real privacy controls for AI mode:
  - sample masking
  - pattern-only profiling
  - PII/PHI redaction
  - AI context preview before send
- User roles and approval workflow.
- Automated recurring feed monitoring.
- Field-level distribution drift checks.
- Change-only eligibility feed support.
- Next.js frontend upgrade.
