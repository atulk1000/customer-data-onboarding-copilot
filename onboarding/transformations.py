from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from onboarding.schema import TargetField

APPROVED_OPERATIONS = {
    "trim",
    "null_if",
    "uppercase",
    "lowercase",
    "title_case",
    "regex_replace",
    "parse_date",
    "parse_numeric",
    "normalize_phone",
    "normalize_email",
    "map_values",
    "default",
    "coalesce",
    "concatenate",
    "split",
    "generate_deterministic_id",
}

FAILURE_POLICIES = {"error", "warning_set_null", "warning_keep_original", "use_default"}
PHONE_DIGITS_RE = re.compile(r"\D+")


class TransformationConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class PipelineExecution:
    value: Any
    trace: list[dict[str, Any]]
    issue: dict[str, Any] | None = None


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (dict, list, tuple, set)):
        return False
    return bool(pd.isna(value)) or str(value).strip() == ""


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list)):
        return value
    if pd.isna(value):
        return None
    return value


def mapping_source_columns(mapping: Mapping[str, Any]) -> list[str]:
    configured = mapping.get("source_columns")
    if isinstance(configured, str):
        configured = [configured]
    if isinstance(configured, list):
        columns = [str(value).strip() for value in configured if str(value).strip()]
        if columns:
            return columns
    source_column = str(mapping.get("source_column") or "").strip()
    return [source_column] if source_column else []


def normalize_steps(raw_steps: Any) -> list[dict[str, Any]]:
    if raw_steps in (None, ""):
        return []
    if isinstance(raw_steps, str):
        try:
            raw_steps = json.loads(raw_steps)
        except json.JSONDecodeError as exc:
            raise TransformationConfigurationError("Transformation steps must be valid JSON.") from exc
    if not isinstance(raw_steps, list):
        raise TransformationConfigurationError("Transformation steps must be a list.")

    normalized: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise TransformationConfigurationError(f"Transformation step {index + 1} must be an object.")
        operation = str(raw_step.get("operation") or "").strip()
        parameters = raw_step.get("parameters") or {}
        if operation not in APPROVED_OPERATIONS:
            raise TransformationConfigurationError(f"Unsupported transformation operation: {operation or '(blank)' }.")
        if not isinstance(parameters, dict):
            raise TransformationConfigurationError(f"Parameters for {operation} must be an object.")
        normalized.append({"operation": operation, "parameters": parameters})
    return normalized


def validate_transformation_pipeline(
    mapping: Mapping[str, Any],
    target_field: TargetField | None = None,
    available_source_columns: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    try:
        steps = normalize_steps(mapping.get("transformation_steps"))
    except TransformationConfigurationError as exc:
        return [str(exc)]

    source_columns = mapping_source_columns(mapping)
    if available_source_columns is not None:
        missing = [column for column in source_columns if column not in available_source_columns]
        if missing:
            errors.append("Unknown source columns: " + ", ".join(missing) + ".")

    failure_policy = str(mapping.get("failure_policy") or "error")
    if failure_policy not in FAILURE_POLICIES:
        errors.append(f"Unsupported failure policy: {failure_policy}.")
    if failure_policy == "warning_set_null" and target_field is not None and not target_field.nullable:
        errors.append("warning_set_null is not allowed for a non-nullable target field.")
    if failure_policy == "use_default" and "failure_default" not in mapping:
        errors.append("use_default requires failure_default on the mapping.")

    for index, step in enumerate(steps):
        operation = step["operation"]
        parameters = step["parameters"]
        path = f"Step {index + 1} ({operation})"
        if operation == "regex_replace":
            if "pattern" not in parameters:
                errors.append(f"{path} requires pattern.")
            else:
                try:
                    re.compile(str(parameters["pattern"]))
                except re.error as exc:
                    errors.append(f"{path} has an invalid regular expression: {exc}.")
            if "replacement" not in parameters:
                errors.append(f"{path} requires replacement.")
        elif operation == "null_if" and not isinstance(parameters.get("values"), list):
            errors.append(f"{path} requires a values list.")
        elif operation == "parse_date" and parameters.get("accepted_formats") is not None:
            if not isinstance(parameters.get("accepted_formats"), list):
                errors.append(f"{path} accepted_formats must be a list.")
        elif operation == "map_values" and not isinstance(parameters.get("mapping"), dict):
            errors.append(f"{path} requires a mapping object.")
        elif operation == "default" and "value" not in parameters:
            errors.append(f"{path} requires value.")
        elif operation in {"coalesce", "concatenate"}:
            operation_columns = parameters.get("source_columns") or source_columns
            if not isinstance(operation_columns, list) or not operation_columns:
                errors.append(f"{path} requires source_columns.")
            elif available_source_columns is not None:
                unknown = [str(column) for column in operation_columns if str(column) not in available_source_columns]
                if unknown:
                    errors.append(f"{path} references unknown source columns: {', '.join(unknown)}.")
        elif operation == "split":
            if "delimiter" not in parameters:
                errors.append(f"{path} requires delimiter.")
            try:
                int(parameters.get("part_index", 0))
            except (TypeError, ValueError):
                errors.append(f"{path} part_index must be an integer.")
        elif operation == "generate_deterministic_id":
            if target_field is not None and not target_field.generated:
                errors.append(f"{path} is allowed only for generated target fields.")
            if not isinstance(parameters.get("input_fields"), list) or not parameters.get("input_fields"):
                errors.append(f"{path} requires input_fields.")

    if not source_columns and not any(step["operation"] == "generate_deterministic_id" for step in steps):
        if target_field is None or not target_field.generated:
            errors.append("At least one source column is required.")
    return errors


def recommended_steps(target_field: TargetField) -> list[dict[str, Any]]:
    if target_field.generated:
        return []
    if target_field.data_type in {"text", "identifier"}:
        return [{"operation": "trim", "parameters": {}}]
    if target_field.data_type == "date":
        return [{"operation": "parse_date", "parameters": {"accepted_formats": []}}]
    if target_field.data_type == "numeric":
        return [{"operation": "parse_numeric", "parameters": {}}]
    if target_field.data_type == "email":
        return [
            {"operation": "trim", "parameters": {}},
            {"operation": "normalize_email", "parameters": {}},
        ]
    if target_field.data_type == "phone":
        return [{"operation": "normalize_phone", "parameters": {"default_country": "US"}}]
    if target_field.data_type == "enum":
        return [{"operation": "trim", "parameters": {}}]
    return []


def _row_values(row: Mapping[str, Any], columns: list[str]) -> list[Any]:
    return [row.get(column) for column in columns]


def _parse_date(value: Any, parameters: dict[str, Any]) -> date | None:
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    accepted_formats = parameters.get("accepted_formats") or []
    if accepted_formats:
        for date_format in accepted_formats:
            try:
                return datetime.strptime(str(value).strip(), str(date_format)).date()
            except ValueError:
                continue
        raise ValueError("Value does not match an accepted date format.")
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for date_format in ["%m/%d/%Y", "%Y/%m/%d", "%m-%d-%Y", "%d-%b-%Y"]:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    parsed = pd.to_datetime(pd.Series([value]), errors="coerce", format="mixed").iloc[0]
    if pd.isna(parsed):
        raise ValueError("Value could not be parsed as a date.")
    return parsed.date()


def _parse_numeric(value: Any, parameters: dict[str, Any]) -> Decimal | None:
    if _is_blank(value):
        return None
    text = str(value).strip()
    grouping_separator = str(parameters.get("grouping_separator", ","))
    decimal_separator = str(parameters.get("decimal_separator", "."))
    for symbol in parameters.get("currency_symbols") or ["$", "GBP", "EUR"]:
        text = text.replace(str(symbol), "")
    if grouping_separator:
        text = text.replace(grouping_separator, "")
    if decimal_separator and decimal_separator != ".":
        text = text.replace(decimal_separator, ".")
    try:
        return Decimal(text.strip())
    except InvalidOperation as exc:
        raise ValueError("Value could not be parsed as numeric.") from exc


def _map_value(value: Any, parameters: dict[str, Any]) -> Any:
    if _is_blank(value):
        return None
    mapping = parameters.get("mapping") or {}
    text = str(value).strip()
    if text in mapping:
        return mapping[text]
    if bool(parameters.get("case_insensitive", True)):
        normalized = text.casefold()
        for source_value, target_value in mapping.items():
            if str(source_value).strip().casefold() == normalized:
                return target_value
    if bool(parameters.get("passthrough_unknown", False)):
        return value
    raise ValueError("Value is not present in the configured crosswalk.")


def _apply_operation(
    operation: str,
    value: Any,
    parameters: dict[str, Any],
    source_row: Mapping[str, Any],
    default_source_columns: list[str],
    context: Mapping[str, Any],
) -> Any:
    if operation == "trim":
        return None if _is_blank(value) else str(value).strip()
    if operation == "null_if":
        if _is_blank(value):
            return None
        normalized_values = {str(item).strip().casefold() for item in parameters.get("values") or []}
        return None if str(value).strip().casefold() in normalized_values else value
    if operation == "uppercase":
        return None if _is_blank(value) else str(value).upper()
    if operation == "lowercase":
        return None if _is_blank(value) else str(value).lower()
    if operation == "title_case":
        return None if _is_blank(value) else str(value).title()
    if operation == "regex_replace":
        return (
            None if _is_blank(value) else re.sub(str(parameters["pattern"]), str(parameters["replacement"]), str(value))
        )
    if operation == "parse_date":
        return _parse_date(value, parameters)
    if operation == "parse_numeric":
        return _parse_numeric(value, parameters)
    if operation == "normalize_phone":
        if _is_blank(value):
            return None
        digits = PHONE_DIGITS_RE.sub("", str(value))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise ValueError("Phone value does not contain ten digits.")
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if operation == "normalize_email":
        return None if _is_blank(value) else str(value).strip().lower()
    if operation == "map_values":
        return _map_value(value, parameters)
    if operation == "default":
        return parameters.get("value") if _is_blank(value) else value
    if operation == "coalesce":
        columns = [str(column) for column in parameters.get("source_columns") or default_source_columns]
        return next((candidate for candidate in _row_values(source_row, columns) if not _is_blank(candidate)), None)
    if operation == "concatenate":
        columns = [str(column) for column in parameters.get("source_columns") or default_source_columns]
        separator = str(parameters.get("separator", " "))
        values = _row_values(source_row, columns)
        if bool(parameters.get("skip_nulls", True)):
            values = [candidate for candidate in values if not _is_blank(candidate)]
        elif any(_is_blank(candidate) for candidate in values):
            return None
        return separator.join(str(candidate).strip() for candidate in values)
    if operation == "split":
        if _is_blank(value):
            return None
        parts = str(value).split(str(parameters["delimiter"]))
        part_index = int(parameters.get("part_index", 0))
        try:
            return parts[part_index].strip()
        except IndexError as exc:
            raise ValueError("Split value does not contain the requested part.") from exc
    if operation == "generate_deterministic_id":
        input_fields = [str(field) for field in parameters.get("input_fields") or []]
        values = [context.get(field, source_row.get(field)) for field in input_fields]
        if any(_is_blank(candidate) for candidate in values):
            raise ValueError("Generated ID input is missing.")
        prefix = str(parameters.get("prefix") or "ID")
        length = int(parameters.get("length", 12))
        digest = hashlib.sha256("|".join(str(candidate).strip() for candidate in values).encode("utf-8")).hexdigest()
        return f"{prefix}-{digest[:length].upper()}"
    raise TransformationConfigurationError(f"Unsupported transformation operation: {operation}.")


def execute_transformation_pipeline(
    source_row: Mapping[str, Any],
    mapping: Mapping[str, Any],
    *,
    target_field: TargetField | None = None,
    context: Mapping[str, Any] | None = None,
    validate_configuration: bool = True,
) -> PipelineExecution:
    if validate_configuration:
        errors = validate_transformation_pipeline(mapping, target_field)
        if errors:
            raise TransformationConfigurationError("; ".join(errors))

    source_columns = mapping_source_columns(mapping)
    steps = normalize_steps(mapping.get("transformation_steps"))
    value = source_row.get(source_columns[0]) if source_columns else None
    trace: list[dict[str, Any]] = []
    context = context or {}

    for step in steps:
        operation = step["operation"]
        parameters = step["parameters"]
        before = value
        try:
            value = _apply_operation(operation, value, parameters, source_row, source_columns, context)
            trace.append(
                {
                    "operation": operation,
                    "input": _json_value(before),
                    "output": _json_value(value),
                    "status": "applied",
                    "message": "",
                }
            )
        except (KeyError, TypeError, ValueError, re.error) as exc:
            failure_policy = str(mapping.get("failure_policy") or "error")
            severity = "error" if failure_policy == "error" else "warning"
            if failure_policy in {"error", "warning_set_null"}:
                value = None
            elif failure_policy == "warning_keep_original":
                value = before
            elif failure_policy == "use_default":
                value = mapping.get("failure_default")
            message = str(exc)
            trace.append(
                {
                    "operation": operation,
                    "input": _json_value(before),
                    "output": _json_value(value),
                    "status": "failed",
                    "message": message,
                }
            )
            target_name = target_field.field if target_field is not None else str(mapping.get("target_field") or "")
            return PipelineExecution(
                value=value,
                trace=trace,
                issue={
                    "severity": severity,
                    "issue_code": f"transformation_{operation}_failed",
                    "issue_message": f"{target_name} transformation {operation} failed: {message}",
                    "target_field": target_name,
                    "source_column": "; ".join(source_columns),
                },
            )

    return PipelineExecution(value=value, trace=trace)


def preview_transformation_pipeline(
    source_df: pd.DataFrame,
    mapping: Mapping[str, Any],
    *,
    target_field: TargetField | None = None,
    limit: int = 10,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    source_columns = mapping_source_columns(mapping)
    for index, source_row in source_df.head(limit).iterrows():
        execution = execute_transformation_pipeline(source_row.to_dict(), mapping, target_field=target_field)
        rows.append(
            {
                "source_row_number": int(index) + 2,
                "source_values": json.dumps(
                    {column: _json_value(source_row.get(column)) for column in source_columns}, ensure_ascii=True
                ),
                "final_value": _json_value(execution.value),
                "status": "failed" if execution.issue else "ready",
                "message": execution.issue.get("issue_message", "") if execution.issue else "",
                "transformation_trace": json.dumps(execution.trace, ensure_ascii=True),
            }
        )
    return pd.DataFrame(rows)
