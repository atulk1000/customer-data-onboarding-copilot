from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from onboarding.contracts import ContractVersion
from onboarding.corrections import add_correction_columns, source_record_id
from onboarding.idempotency import source_dataframe_fingerprint
from onboarding.schema import FIELD_TABLES, TARGET_SCHEMA, TargetField
from onboarding.transformations import (
    TransformationConfigurationError,
    execute_transformation_pipeline,
    mapping_source_columns,
    validate_transformation_pipeline,
)


@dataclass
class TransformOutputs:
    members: pd.DataFrame
    plans: pd.DataFrame
    member_coverage: pd.DataFrame
    rejected_rows: pd.DataFrame
    field_lineage: pd.DataFrame
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    table_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    correction_work_queue: pd.DataFrame = field(default_factory=pd.DataFrame)


def _approved_mappings(mappings: list[dict[str, Any]]) -> dict[str, str]:
    approved: dict[str, str] = {}
    for mapping in mappings:
        sources = mapping_source_columns(mapping)
        if sources and bool(mapping.get("approved", False)):
            approved[mapping["target_field"]] = "; ".join(sources)
    return approved


def approved_source_columns_by_target_field(mappings: list[dict[str, Any]]) -> dict[str, str]:
    approved: dict[str, str] = {}
    for mapping in mappings:
        if not bool(mapping.get("approved", False)):
            continue
        source_columns = mapping_source_columns(mapping)
        if source_columns:
            approved[str(mapping.get("target_field") or "")] = "; ".join(source_columns)
    return approved


def _approved_mapping_keys(mappings: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    approved: dict[tuple[str, str], str] = {}
    for mapping in mappings:
        sources = mapping_source_columns(mapping)
        if sources and bool(mapping.get("approved", False)):
            key = (str(mapping.get("target_table") or ""), str(mapping.get("target_field") or ""))
            approved[key] = "; ".join(sources)
    return approved


def _approved_mapping_rows(mappings: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    approved: dict[tuple[str, str], dict[str, Any]] = {}
    for mapping in mappings:
        if not bool(mapping.get("approved", False)):
            continue
        source_columns = mapping_source_columns(mapping)
        if not source_columns and not mapping.get("transformation_steps"):
            continue
        key = (str(mapping.get("target_table") or ""), str(mapping.get("target_field") or ""))
        approved[key] = dict(mapping)
    return approved


def build_canonical_flat(
    source_df: pd.DataFrame,
    mappings: list[dict[str, Any]],
    target_schema: list[TargetField] | None = None,
    *,
    source_file_hash: str = "",
) -> pd.DataFrame:
    selected_schema = target_schema or TARGET_SCHEMA
    mapping_rows = _approved_mapping_rows(mappings)
    file_hash = source_file_hash or source_dataframe_fingerprint(source_df)
    flat = pd.DataFrame({"source_row_number": range(2, len(source_df) + 2)})
    flat["source_record_id"] = [source_record_id(file_hash, row_number) for row_number in flat["source_row_number"]]
    transformation_traces: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
    transformation_issues: list[dict[str, Any]] = []
    pipeline_values: dict[tuple[int, str, str], Any] = {}
    source_values: dict[tuple[int, str, str], dict[str, Any]] = {}
    values_by_field: dict[str, list[Any]] = {}

    for target in selected_schema:
        if target.field == "coverage_id":
            continue
        mapping = mapping_rows.get((target.table, target.field))
        if mapping is not None:
            configuration_errors = validate_transformation_pipeline(
                mapping,
                target,
                set(str(column) for column in source_df.columns),
            )
            if configuration_errors:
                raise TransformationConfigurationError("; ".join(configuration_errors))
        field_values: list[Any] = []
        for source_index, source_row in source_df.iterrows():
            row_number = int(source_index) + 2
            trace_key = (row_number, target.table, target.field)
            if mapping is None:
                value = None
                transformation_traces[trace_key] = []
                source_values[trace_key] = {}
            else:
                execution = execute_transformation_pipeline(
                    source_row.to_dict(),
                    mapping,
                    target_field=target,
                    validate_configuration=False,
                )
                value = execution.value
                transformation_traces[trace_key] = execution.trace
                mapped_columns = mapping_source_columns(mapping)
                source_values[trace_key] = {
                    column: source_row.get(column) for column in mapped_columns if column in source_df.columns
                }
                if execution.issue:
                    transformation_issues.append({**execution.issue, "source_row_number": row_number})
            pipeline_values[trace_key] = value
            field_values.append(value)

        existing_values = values_by_field.get(target.field)
        if existing_values is None:
            values_by_field[target.field] = field_values
        elif existing_values != field_values:
            transformation_issues.extend(
                {
                    "source_row_number": row_number,
                    "severity": "error",
                    "issue_code": "conflicting_duplicate_target_field",
                    "issue_message": (
                        f"{target.field} has different approved values across target tables; "
                        "use a consistent mapping for repeated logical fields."
                    ),
                    "target_field": target.field,
                    "source_column": "; ".join(mapping_source_columns(mapping or {})),
                }
                for row_number in flat["source_row_number"]
            )

    for field_name, field_values in values_by_field.items():
        flat[field_name] = field_values

    flat.attrs["source_file_hash"] = file_hash
    flat.attrs["transformation_traces"] = transformation_traces
    flat.attrs["transformation_issues"] = transformation_issues
    flat.attrs["pipeline_values"] = pipeline_values
    flat.attrs["source_values"] = source_values
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
                "warning_source_columns": "; ".join(
                    dict.fromkeys(column for column in warning_source_columns if column)
                ),
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
    *,
    target_schema: list[TargetField],
    source_file_hash: str,
    original_source_df: pd.DataFrame | None = None,
    correction_audit: list[dict[str, Any]] | None = None,
    contract_version: str = "",
    mapping_template_version: str = "",
    parent_import_run_id: int | None = None,
) -> pd.DataFrame:
    mapping_rows = _approved_mapping_rows(mappings)
    error_details_by_row = _issue_details(issues_df, "error")
    warning_details_by_row = _issue_details(issues_df, "warning")
    lineage_records: list[dict[str, Any]] = []
    original_source_df = original_source_df if original_source_df is not None else source_df
    traces = normalized_df.attrs.get("transformation_traces") or {}
    pipeline_values = normalized_df.attrs.get("pipeline_values") or {}
    stored_source_values = normalized_df.attrs.get("source_values") or {}
    corrected_by_key = {
        (int(row["source_row_number"]), str(row["source_column"])): row.get("corrected_value")
        for row in correction_audit or []
    }

    for row in normalized_df.to_dict("records"):
        row_number = int(row["source_row_number"])
        source_index = row_number - 2
        row_errors = error_details_by_row.get(row_number, [])
        row_warnings = warning_details_by_row.get(row_number, [])
        row_status = "rejected" if row_errors else "accepted"

        for target in target_schema:
            trace_key = (row_number, target.table, target.field)
            mapping = mapping_rows.get((target.table, target.field), {})
            source_columns = mapping_source_columns(mapping)
            source_column = "; ".join(source_columns)
            original_value = None
            if source_columns and 0 <= source_index < len(original_source_df):
                original_value = original_source_df.iloc[source_index].get(source_columns[0])

            if target.generated:
                normalized_value = _generated_field_value(row, target) if row_status == "accepted" else None
            else:
                normalized_value = row.get(target.field)

            transformation_trace = [dict(step) for step in traces.get(trace_key, [])]
            pipeline_value = pipeline_values.get(trace_key)
            if not target.generated and _format_value(pipeline_value) != _format_value(normalized_value):
                transformation_trace.append(
                    {
                        "operation": "canonical_normalize",
                        "input": _format_value(pipeline_value),
                        "output": _format_value(normalized_value),
                        "status": "applied",
                        "message": "Applied target data type and validation vocabulary normalization.",
                    }
                )
            if target.generated and normalized_value is not None:
                transformation_trace.append(
                    {
                        "operation": "generate_deterministic_id",
                        "input": None,
                        "output": _format_value(normalized_value),
                        "status": "applied",
                        "message": "Generated from the target table business values.",
                    }
                )

            source_value_payload = stored_source_values.get(trace_key) or {
                column: (
                    original_source_df.iloc[source_index].get(column)
                    if 0 <= source_index < len(original_source_df)
                    else None
                )
                for column in source_columns
            }
            corrected_values = {
                column: corrected_by_key[(row_number, column)]
                for column in source_columns
                if (row_number, column) in corrected_by_key
            }

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
                    "source_record_id": row.get("source_record_id") or source_record_id(source_file_hash, row_number),
                    "row_status": row_status,
                    "lineage_status": lineage_status,
                    "target_table": target.table,
                    "target_field": target.field,
                    "target_data_type": target.data_type,
                    "target_validation_kind": target.validation_kind,
                    "source_column": source_column,
                    "source_columns": source_column,
                    "source_values_json": json.dumps(
                        {key: _format_value(value) for key, value in source_value_payload.items()},
                        ensure_ascii=True,
                    ),
                    "corrected_values_json": json.dumps(
                        {key: _format_value(value) for key, value in corrected_values.items()},
                        ensure_ascii=True,
                    ),
                    "original_value": _format_value(original_value),
                    "normalized_value": _format_value(normalized_value),
                    "final_value": _format_value(normalized_value),
                    "transformation_applied": ", ".join(
                        str(step.get("operation") or "") for step in transformation_trace
                    )
                    or _transformation_description(
                        target.data_type, target.validation_kind, source_column, target.generated
                    ),
                    "transformation_trace_json": json.dumps(transformation_trace, ensure_ascii=True),
                    "contract_version": contract_version,
                    "mapping_template_version": mapping_template_version,
                    "parent_import_run_id": parent_import_run_id,
                    "issue_codes": _issue_values(field_issues, "issue_code"),
                    "issue_messages": _issue_values(field_issues, "issue_message"),
                }
            )

    columns = [
        "source_row_number",
        "source_record_id",
        "row_status",
        "lineage_status",
        "target_table",
        "target_field",
        "target_data_type",
        "target_validation_kind",
        "source_column",
        "source_columns",
        "source_values_json",
        "corrected_values_json",
        "original_value",
        "normalized_value",
        "final_value",
        "transformation_applied",
        "transformation_trace_json",
        "contract_version",
        "mapping_template_version",
        "parent_import_run_id",
        "issue_codes",
        "issue_messages",
    ]
    return pd.DataFrame(lineage_records, columns=columns)


def _generated_field_value(row: dict[str, Any], target: TargetField) -> str:
    if target.field == "coverage_id":
        return _coverage_id(row)
    candidate_values = [
        str(value).strip()
        for key, value in row.items()
        if key not in {"source_row_number", "source_record_id", target.field} and not is_missing_value(value)
    ]
    digest = hashlib.sha256("|".join(candidate_values).encode("utf-8")).hexdigest()[:12].upper()
    prefix = target.field.replace("_id", "").upper()[:8] or "ID"
    return f"{prefix}-{digest}"


def is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (dict, list)):
        return False
    return bool(pd.isna(value)) or str(value).strip() == ""


def _merge_business_key_group(
    group: pd.DataFrame,
    business_key: list[str],
) -> tuple[dict[str, Any], bool]:
    merged = group.iloc[0].to_dict()
    has_conflict = False

    for column in group.columns:
        if column in business_key:
            continue
        populated_values = [value for value in group[column].tolist() if not is_missing_value(value)]
        distinct_values = {
            json.dumps(_format_value(value), sort_keys=True, ensure_ascii=True) for value in populated_values
        }
        if len(distinct_values) > 1:
            has_conflict = True
        if populated_values:
            merged[column] = populated_values[0]

    return merged, has_conflict


def _table_candidate_stats(
    candidate: pd.DataFrame,
    table_fields: list[TargetField],
    business_key: list[str],
) -> tuple[pd.DataFrame, dict[str, int]]:
    required_fields = [target.field for target in table_fields if target.required]
    missing_output_count = 0
    if required_fields and not candidate.empty:
        missing_output_count = int(
            candidate[required_fields].apply(lambda row: any(is_missing_value(value) for value in row), axis=1).sum()
        )

    exact_duplicate_count = 0
    conflicting_duplicate_count = 0
    unique_business_key_count = len(candidate)
    if business_key and not candidate.empty and all(column in candidate.columns for column in business_key):
        unique_business_key_count = int(candidate[business_key].drop_duplicates().shape[0])
        merged_records: list[dict[str, Any]] = []
        for _, group in candidate.groupby(business_key, dropna=False, sort=False):
            merged_record, has_conflict = _merge_business_key_group(group, business_key)
            merged_records.append(merged_record)
            if len(group) <= 1:
                continue
            if has_conflict:
                conflicting_duplicate_count += len(group)
            else:
                exact_duplicate_count += len(group) - 1
        deduplicated = pd.DataFrame(merged_records, columns=candidate.columns)
    else:
        deduplicated = candidate.drop_duplicates(keep="first")

    if business_key and not deduplicated.empty:
        deduplicated = deduplicated.sort_values(business_key)
    deduplicated = deduplicated.reset_index(drop=True)
    return deduplicated, {
        "candidate_count": int(len(candidate)),
        "unique_business_key_count": int(unique_business_key_count),
        "exact_duplicate_count": int(exact_duplicate_count),
        "conflicting_duplicate_count": int(conflicting_duplicate_count),
        "missing_output_count": int(missing_output_count),
    }


def transform_outputs(
    normalized_df: pd.DataFrame,
    source_df: pd.DataFrame,
    issues_df: pd.DataFrame,
    mappings: list[dict[str, Any]] | None = None,
    *,
    target_schema: list[TargetField] | None = None,
    contract: ContractVersion | None = None,
    source_file_hash: str = "",
    original_source_df: pd.DataFrame | None = None,
    correction_audit: list[dict[str, Any]] | None = None,
    mapping_template_version: str = "",
    parent_import_run_id: int | None = None,
) -> TransformOutputs:
    mappings = mappings or []
    selected_schema = target_schema or (contract.target_fields if contract else TARGET_SCHEMA)
    file_hash = (
        source_file_hash
        or normalized_df.attrs.get("source_file_hash")
        or source_dataframe_fingerprint(original_source_df if original_source_df is not None else source_df)
    )
    field_lookup = _approved_mappings(mappings)
    error_details_by_row = _issue_details(issues_df, "error")
    warning_details_by_row = _issue_details(issues_df, "warning")
    error_by_row = _issue_summary(issues_df, "error")
    working_normalized = pd.DataFrame(
        normalized_df.to_numpy(copy=False),
        columns=normalized_df.columns,
        index=normalized_df.index,
    )
    accepted = working_normalized[~working_normalized["source_row_number"].isin(error_by_row)].copy()

    table_names = list(dict.fromkeys(target.table for target in selected_schema))
    default_business_keys = {"members": ["member_id"], "plans": ["plan_id"], "member_coverage": ["coverage_id"]}
    contract_business_keys = contract.business_keys if contract else default_business_keys
    tables: dict[str, pd.DataFrame] = {}
    table_stats: dict[str, dict[str, int]] = {}
    for table_name in table_names:
        table_fields = [target for target in selected_schema if target.table == table_name]
        candidate = pd.DataFrame(index=accepted.index)
        for target in table_fields:
            if target.generated:
                candidate[target.field] = [_generated_field_value(row, target) for row in accepted.to_dict("records")]
            elif target.field in accepted.columns:
                candidate[target.field] = accepted[target.field].values
            else:
                candidate[target.field] = None
        tables[table_name], table_stats[table_name] = _table_candidate_stats(
            candidate,
            table_fields,
            contract_business_keys.get(table_name, []),
        )

    members = tables.get("members", pd.DataFrame(columns=FIELD_TABLES["members"]))
    plans = tables.get("plans", pd.DataFrame(columns=FIELD_TABLES["plans"]))
    member_coverage = tables.get("member_coverage", pd.DataFrame(columns=FIELD_TABLES["member_coverage"]))

    rejected_rows = _build_rejected_rows(source_df, error_details_by_row, warning_details_by_row, field_lookup)
    correction_work_queue = add_correction_columns(
        rejected_rows,
        original_source_df if original_source_df is not None else source_df,
        file_hash,
    )
    field_lineage = _build_field_lineage(
        normalized_df,
        source_df,
        issues_df,
        mappings,
        target_schema=selected_schema,
        source_file_hash=file_hash,
        original_source_df=original_source_df,
        correction_audit=correction_audit,
        contract_version=contract.version if contract else "",
        mapping_template_version=mapping_template_version,
        parent_import_run_id=parent_import_run_id,
    )
    return TransformOutputs(
        members=members,
        plans=plans,
        member_coverage=member_coverage,
        rejected_rows=rejected_rows,
        field_lineage=field_lineage,
        tables=tables,
        table_stats=table_stats,
        correction_work_queue=correction_work_queue,
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
