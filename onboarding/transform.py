from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from onboarding.schema import FIELD_TABLES, TARGET_SCHEMA


@dataclass
class TransformOutputs:
    members: pd.DataFrame
    plans: pd.DataFrame
    member_coverage: pd.DataFrame
    rejected_rows: pd.DataFrame
    field_lineage: pd.DataFrame


def _approved_mappings(mappings: list[dict[str, Any]]) -> dict[str, str]:
    approved: dict[str, str] = {}
    for mapping in mappings:
        source = str(mapping.get("source_column") or "").strip()
        if source and bool(mapping.get("approved", False)):
            approved[mapping["target_field"]] = source
    return approved


def approved_source_columns_by_target_field(mappings: list[dict[str, Any]]) -> dict[str, str]:
    return _approved_mappings(mappings)


def _approved_mapping_keys(mappings: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    approved: dict[tuple[str, str], str] = {}
    for mapping in mappings:
        source = str(mapping.get("source_column") or "").strip()
        if source and bool(mapping.get("approved", False)):
            key = (str(mapping.get("target_table") or ""), str(mapping.get("target_field") or ""))
            approved[key] = source
    return approved


def build_canonical_flat(source_df: pd.DataFrame, mappings: list[dict[str, Any]]) -> pd.DataFrame:
    lookup = _approved_mappings(mappings)
    flat = pd.DataFrame({"source_row_number": range(2, len(source_df) + 2)})
    for target in TARGET_SCHEMA:
        if target.field == "coverage_id":
            continue
        source_column = lookup.get(target.field)
        if source_column and source_column in source_df.columns:
            flat[target.field] = source_df[source_column].values
        else:
            flat[target.field] = None
    return flat


def _issue_details(issue_rows: pd.DataFrame, severity: str) -> dict[int, list[dict[str, Any]]]:
    if issue_rows.empty:
        return {}
    filtered = issue_rows[issue_rows["severity"].eq(severity)]
    grouped: dict[int, list[str]] = {}
    for row in filtered.to_dict("records"):
        grouped.setdefault(int(row["source_row_number"]), []).append(row)
    return grouped


def _issue_summary(issue_rows: pd.DataFrame, severity: str) -> dict[int, list[str]]:
    details = _issue_details(issue_rows, severity)
    return {
        row_number: [str(issue.get("issue_message") or "") for issue in issues]
        for row_number, issues in details.items()
    }


def _coverage_id(row: dict[str, Any]) -> str:
    key_parts = [
        str(row.get("member_id") or "").strip(),
        str(row.get("plan_id") or "").strip(),
        str(row.get("coverage_start_date") or "").strip(),
    ]
    digest = hashlib.sha256("|".join(key_parts).encode("utf-8")).hexdigest()[:12].upper()
    return f"COV-{digest}"


def _format_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if pd.isna(value) if not isinstance(value, (dict, list)) else False:
        return None
    return value


def _issue_values(issues: list[dict[str, Any]], key: str) -> str:
    values = []
    for issue in issues:
        value = str(issue.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return "; ".join(values)


def _source_column_for_target(
    target_table: str,
    target_field: str,
    field_lookup: dict[str, str],
    key_lookup: dict[tuple[str, str], str],
) -> str:
    return key_lookup.get((target_table, target_field)) or field_lookup.get(target_field) or ""


def _transformation_description(data_type: str, validation_kind: str, source_column: str, generated: bool) -> str:
    if generated:
        return "Generated from member_id, plan_id, and coverage_start_date."
    if not source_column:
        return "No approved source mapping."
    if data_type == "date":
        return "Parse source value to a canonical date."
    if data_type == "enum":
        return f"Normalize source value to the {validation_kind} allowed vocabulary."
    if data_type == "email":
        return "Lowercase and validate email format."
    if data_type == "phone":
        return "Normalize phone digits to ###-###-####."
    if data_type == "identifier":
        return "Trim whitespace and preserve identifier value."
    if data_type == "text":
        return "Trim whitespace and preserve text value."
    return "Normalize source value for the target field."


def _build_rejected_rows(
    source_df: pd.DataFrame,
    error_details_by_row: dict[int, list[dict[str, Any]]],
    warning_details_by_row: dict[int, list[dict[str, Any]]],
    field_lookup: dict[str, str],
) -> pd.DataFrame:
    rejected_records: list[dict[str, Any]] = []
    for row_number, errors in sorted(error_details_by_row.items()):
        source_index = row_number - 2
        raw_payload = (
            {
                f"original__{column}": _format_value(value)
                for column, value in source_df.iloc[source_index].to_dict().items()
            }
            if 0 <= source_index < len(source_df)
            else {}
        )
        warnings = warning_details_by_row.get(row_number, [])
        error_source_columns = [
            field_lookup.get(str(issue.get("target_field") or ""), str(issue.get("source_column") or ""))
            for issue in errors
        ]
        warning_source_columns = [
            field_lookup.get(str(issue.get("target_field") or ""), str(issue.get("source_column") or ""))
            for issue in warnings
        ]
        rejected_records.append(
            {
                "source_row_number": row_number,
                "row_status": "rejected",
                "error_count": len(errors),
                "error_codes": _issue_values(errors, "issue_code"),
                "error_target_fields": _issue_values(errors, "target_field"),
                "error_source_columns": "; ".join(dict.fromkeys(column for column in error_source_columns if column)),
                "errors": _issue_values(errors, "issue_message"),
                "warning_count": len(warnings),
                "warning_codes": _issue_values(warnings, "issue_code"),
                "warning_target_fields": _issue_values(warnings, "target_field"),
                "warning_source_columns": "; ".join(dict.fromkeys(column for column in warning_source_columns if column)),
                "warnings": _issue_values(warnings, "issue_message"),
                **raw_payload,
            }
        )

    return pd.DataFrame(rejected_records)


def _build_field_lineage(
    normalized_df: pd.DataFrame,
    source_df: pd.DataFrame,
    issues_df: pd.DataFrame,
    mappings: list[dict[str, Any]],
) -> pd.DataFrame:
    field_lookup = _approved_mappings(mappings)
    key_lookup = _approved_mapping_keys(mappings)
    error_details_by_row = _issue_details(issues_df, "error")
    warning_details_by_row = _issue_details(issues_df, "warning")
    lineage_records: list[dict[str, Any]] = []

    for row in normalized_df.to_dict("records"):
        row_number = int(row["source_row_number"])
        source_index = row_number - 2
        row_errors = error_details_by_row.get(row_number, [])
        row_warnings = warning_details_by_row.get(row_number, [])
        row_status = "rejected" if row_errors else "accepted"

        for target in TARGET_SCHEMA:
            source_column = _source_column_for_target(target.table, target.field, field_lookup, key_lookup)
            original_value = None
            if source_column and source_column in source_df.columns and 0 <= source_index < len(source_df):
                original_value = source_df.iloc[source_index][source_column]

            if target.generated:
                normalized_value = _coverage_id(row) if row_status == "accepted" else None
            else:
                normalized_value = row.get(target.field)

            field_errors = [issue for issue in row_errors if issue.get("target_field") == target.field]
            field_warnings = [issue for issue in row_warnings if issue.get("target_field") == target.field]
            if field_errors:
                lineage_status = "error"
            elif field_warnings:
                lineage_status = "warning"
            elif row_status == "rejected":
                lineage_status = "not_published_row_rejected"
            else:
                lineage_status = "accepted"

            field_issues = field_errors + field_warnings
            lineage_records.append(
                {
                    "source_row_number": row_number,
                    "row_status": row_status,
                    "lineage_status": lineage_status,
                    "target_table": target.table,
                    "target_field": target.field,
                    "target_data_type": target.data_type,
                    "target_validation_kind": target.validation_kind,
                    "source_column": source_column,
                    "original_value": _format_value(original_value),
                    "normalized_value": _format_value(normalized_value),
                    "transformation_applied": _transformation_description(
                        target.data_type,
                        target.validation_kind,
                        source_column,
                        target.generated,
                    ),
                    "issue_codes": _issue_values(field_issues, "issue_code"),
                    "issue_messages": _issue_values(field_issues, "issue_message"),
                }
            )

    columns = [
        "source_row_number",
        "row_status",
        "lineage_status",
        "target_table",
        "target_field",
        "target_data_type",
        "target_validation_kind",
        "source_column",
        "original_value",
        "normalized_value",
        "transformation_applied",
        "issue_codes",
        "issue_messages",
    ]
    return pd.DataFrame(lineage_records, columns=columns)


def transform_outputs(
    normalized_df: pd.DataFrame,
    source_df: pd.DataFrame,
    issues_df: pd.DataFrame,
    mappings: list[dict[str, Any]] | None = None,
) -> TransformOutputs:
    mappings = mappings or []
    field_lookup = _approved_mappings(mappings)
    error_details_by_row = _issue_details(issues_df, "error")
    warning_details_by_row = _issue_details(issues_df, "warning")
    error_by_row = _issue_summary(issues_df, "error")
    accepted = normalized_df[~normalized_df["source_row_number"].isin(error_by_row)].copy()

    members = (
        accepted[FIELD_TABLES["members"]]
        .drop_duplicates(subset=["member_id"], keep="first")
        .sort_values("member_id")
        .reset_index(drop=True)
    )
    plans = (
        accepted[FIELD_TABLES["plans"]]
        .drop_duplicates(subset=["plan_id"], keep="first")
        .sort_values("plan_id")
        .reset_index(drop=True)
    )

    coverage_fields = [field for field in FIELD_TABLES["member_coverage"] if field != "coverage_id"]
    member_coverage = accepted[coverage_fields].reset_index(drop=True)
    member_coverage.insert(
        0,
        "coverage_id",
        [_coverage_id(row) for row in member_coverage.to_dict("records")],
    )

    rejected_rows = _build_rejected_rows(source_df, error_details_by_row, warning_details_by_row, field_lookup)
    field_lineage = _build_field_lineage(normalized_df, source_df, issues_df, mappings)
    return TransformOutputs(
        members=members,
        plans=plans,
        member_coverage=member_coverage,
        rejected_rows=rejected_rows,
        field_lineage=field_lineage,
    )


def dataframe_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in df.to_dict("records"):
        clean: dict[str, Any] = {}
        for key, value in row.items():
            if value is None:
                clean[key] = None
            elif isinstance(value, (datetime, date)):
                clean[key] = value.isoformat()
            elif pd.isna(value) if not isinstance(value, (dict, list)) else False:
                clean[key] = None
            else:
                clean[key] = value
        records.append(clean)
    return json.loads(json.dumps(records))
