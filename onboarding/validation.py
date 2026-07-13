from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from onboarding.profiler import normalize_token
from onboarding.schema import (
    ENUM_NORMALIZERS,
    TARGET_SCHEMA,
    TargetField,
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_DIGITS_RE = re.compile(r"\D+")


@dataclass
class ValidationIssue:
    source_row_number: int
    severity: str
    issue_code: str
    issue_message: str
    target_field: str
    source_column: str = ""


@dataclass
class ValidationResult:
    normalized_df: pd.DataFrame
    issues: list[ValidationIssue]

    @property
    def issues_df(self) -> pd.DataFrame:
        return pd.DataFrame([issue.__dict__ for issue in self.issues])

    @property
    def error_row_numbers(self) -> set[int]:
        return {issue.source_row_number for issue in self.issues if issue.severity == "error"}

    @property
    def accepted_row_count(self) -> int:
        return int(len(self.normalized_df) - len(self.error_row_numbers))

    @property
    def rejected_row_count(self) -> int:
        return int(len(self.error_row_numbers))

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warning")


def is_blank(value: Any) -> bool:
    return (
        value is None or (pd.isna(value) if not isinstance(value, (list, dict)) else False) or str(value).strip() == ""
    )


def _clean_text(value: Any) -> str | None:
    if is_blank(value):
        return None
    return str(value).strip()


def _parse_date(value: Any) -> date | None:
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(pd.Series([value]), errors="coerce", format="mixed").iloc[0]
    if pd.isna(parsed):
        return None
    return parsed.date()


def _normalize_enum(value: Any, normalizer: dict[str, str]) -> str | None:
    if is_blank(value):
        return None
    return normalizer.get(normalize_token(value))


def _normalize_email(value: Any) -> str | None:
    if is_blank(value):
        return None
    email = str(value).strip().lower()
    return email if EMAIL_RE.match(email) else None


def _normalize_phone(value: Any) -> str | None:
    if is_blank(value):
        return None
    digits = PHONE_DIGITS_RE.sub("", str(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def _normalize_allowed_enum(value: Any, target: TargetField) -> str | None:
    normalizer = ENUM_NORMALIZERS.get(target.validation_kind)
    if normalizer is not None:
        return _normalize_enum(value, normalizer)
    if is_blank(value):
        return None
    normalized = normalize_token(value)
    for allowed_value in target.allowed_values:
        if normalize_token(allowed_value) == normalized:
            return allowed_value
    return None


def _normalize_numeric(value: Any) -> float | int | None:
    if is_blank(value):
        return None
    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(parsed):
        return None
    numeric = float(parsed)
    return int(numeric) if numeric.is_integer() else numeric


def _normalize_boolean(value: Any) -> bool | None:
    if is_blank(value):
        return None
    normalized = normalize_token(value)
    if normalized in {"true", "t", "yes", "y", "1"}:
        return True
    if normalized in {"false", "f", "no", "n", "0"}:
        return False
    return None


def normalize_canonical_frame(
    flat_df: pd.DataFrame,
    target_schema: list[TargetField] | None = None,
) -> pd.DataFrame:
    selected_schema = target_schema or TARGET_SCHEMA
    source_attrs = dict(flat_df.attrs)
    normalized = pd.DataFrame(
        flat_df.to_numpy(copy=True),
        columns=flat_df.columns.copy(),
        index=flat_df.index.copy(),
    )
    fields_by_name: dict[str, TargetField] = {}
    for target in selected_schema:
        fields_by_name.setdefault(target.field, target)
    for field_name, target in fields_by_name.items():
        if target.generated:
            continue
        if field_name not in normalized.columns:
            normalized[field_name] = None
        if target.data_type in {"text", "identifier"}:
            normalized[field_name] = normalized[field_name].map(_clean_text)
        elif target.data_type == "date":
            normalized[field_name] = normalized[field_name].map(_parse_date)
        elif target.data_type == "enum":
            normalized[field_name] = normalized[field_name].map(
                lambda value, selected_target=target: _normalize_allowed_enum(value, selected_target)
            )
        elif target.data_type == "email":
            normalized[field_name] = normalized[field_name].map(_normalize_email)
        elif target.data_type == "phone":
            normalized[field_name] = normalized[field_name].map(_normalize_phone)
        elif target.data_type == "numeric":
            normalized[field_name] = normalized[field_name].map(_normalize_numeric)
        elif target.data_type == "boolean":
            normalized[field_name] = normalized[field_name].map(_normalize_boolean)
    normalized.attrs.update(source_attrs)
    return normalized


def _add_issue(
    issues: list[ValidationIssue],
    row_number: int,
    severity: str,
    code: str,
    message: str,
    field: str,
) -> None:
    issues.append(
        ValidationIssue(
            source_row_number=int(row_number),
            severity=severity,
            issue_code=code,
            issue_message=message,
            target_field=field,
        )
    )


def _age_years(dob: date) -> int:
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _attach_source_columns(issues: list[ValidationIssue], field_source_columns: dict[str, str] | None) -> None:
    if not field_source_columns:
        return
    for issue in issues:
        issue.source_column = field_source_columns.get(issue.target_field, issue.source_column)


def validate_canonical_frame(
    flat_df: pd.DataFrame,
    field_source_columns: dict[str, str] | None = None,
    target_schema: list[TargetField] | None = None,
    preprocessing_issues: list[dict[str, Any]] | None = None,
) -> ValidationResult:
    selected_schema = target_schema or TARGET_SCHEMA
    normalized = normalize_canonical_frame(flat_df, selected_schema)
    fields_by_name: dict[str, TargetField] = {}
    for target in selected_schema:
        fields_by_name.setdefault(target.field, target)
    issues: list[ValidationIssue] = [
        ValidationIssue(
            source_row_number=int(issue["source_row_number"]),
            severity=str(issue.get("severity") or "error"),
            issue_code=str(issue.get("issue_code") or "transformation_failed"),
            issue_message=str(issue.get("issue_message") or "Transformation failed."),
            target_field=str(issue.get("target_field") or ""),
            source_column=str(issue.get("source_column") or ""),
        )
        for issue in list(flat_df.attrs.get("transformation_issues") or []) + list(preprocessing_issues or [])
    ]
    working_normalized = pd.DataFrame(
        normalized.to_numpy(copy=False),
        columns=normalized.columns,
        index=normalized.index,
    )
    working_raw = pd.DataFrame(
        flat_df.to_numpy(copy=False),
        columns=flat_df.columns,
        index=flat_df.index,
    )
    known_member_ids = (
        {str(value) for value in working_normalized["member_id"].dropna().tolist() if str(value).strip()}
        if "member_id" in working_normalized.columns
        else set()
    )

    for idx, row in working_normalized.iterrows():
        raw = working_raw.loc[idx]
        row_number = int(row["source_row_number"])

        for field_name, target in fields_by_name.items():
            if target.generated:
                continue
            if target.required and is_blank(row.get(field_name)):
                _add_issue(
                    issues,
                    row_number,
                    "error",
                    f"{field_name}_missing_or_invalid",
                    f"{field_name} is missing or invalid.",
                    field_name,
                )
            elif (
                not target.required
                and not is_blank(raw.get(field_name))
                and is_blank(row.get(field_name))
                and field_name not in {"email", "phone", "gender", "plan_type"}
            ):
                severity = "warning"
                if target.validation_rules:
                    severity = str(target.validation_rules[0].get("severity") or severity)
                _add_issue(
                    issues,
                    row_number,
                    severity,
                    f"{field_name}_invalid_{target.data_type}",
                    f"{field_name} could not be normalized as {target.data_type}.",
                    field_name,
                )

            for rule in target.validation_rules:
                rule_kind = str(rule.get("kind") or "")
                parameters = rule.get("parameters") or {}
                value = row.get(field_name)
                severity = str(rule.get("severity") or "error")
                if rule_kind == "not_future" and value and value > date.today():
                    _add_issue(
                        issues,
                        row_number,
                        severity,
                        f"{field_name}_future",
                        f"{field_name} is in the future.",
                        field_name,
                    )
                if rule_kind == "numeric_range" and value is not None:
                    minimum = parameters.get("minimum")
                    maximum = parameters.get("maximum")
                    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
                        _add_issue(
                            issues,
                            row_number,
                            severity,
                            f"{field_name}_out_of_range",
                            f"{field_name} is outside the configured numeric range.",
                            field_name,
                        )

        dob = row.get("date_of_birth")
        if dob:
            age = _age_years(dob)
            if dob > date.today():
                _add_issue(
                    issues,
                    row_number,
                    "error",
                    "date_of_birth_future",
                    "date_of_birth is in the future.",
                    "date_of_birth",
                )
            elif age < 0 or age > 120:
                _add_issue(
                    issues,
                    row_number,
                    "error",
                    "date_of_birth_age_out_of_range",
                    "Member age is outside the supported 0-120 range.",
                    "date_of_birth",
                )

        coverage_start = row.get("coverage_start_date")
        coverage_end = row.get("coverage_end_date")
        if coverage_start and coverage_end and coverage_end < coverage_start:
            _add_issue(
                issues,
                row_number,
                "error",
                "coverage_end_before_start",
                "coverage_end_date is before coverage_start_date.",
                "coverage_end_date",
            )

        relationship = row.get("relationship_to_subscriber")
        subscriber_id = row.get("subscriber_id")
        if relationship and relationship != "self":
            if is_blank(subscriber_id):
                _add_issue(
                    issues,
                    row_number,
                    "error",
                    "dependent_missing_subscriber_id",
                    "Dependent row is missing subscriber_id.",
                    "subscriber_id",
                )
            elif str(subscriber_id) not in known_member_ids:
                _add_issue(
                    issues,
                    row_number,
                    "error",
                    "subscriber_id_not_found",
                    "subscriber_id references no known member in the file.",
                    "subscriber_id",
                )

        if "email" in fields_by_name and is_blank(raw.get("email")):
            _add_issue(issues, row_number, "warning", "email_missing", "email is missing.", "email")
        elif "email" in fields_by_name and is_blank(row.get("email")):
            _add_issue(issues, row_number, "warning", "email_invalid", "email format is invalid.", "email")

        if "phone" in fields_by_name and not is_blank(raw.get("phone")) and is_blank(row.get("phone")):
            _add_issue(issues, row_number, "warning", "phone_invalid", "phone format is invalid.", "phone")

        if "gender" in fields_by_name and is_blank(row.get("gender")):
            _add_issue(issues, row_number, "warning", "gender_unknown", "gender is missing or unknown.", "gender")

        if "plan_type" in fields_by_name and is_blank(row.get("plan_type")):
            _add_issue(
                issues, row_number, "warning", "plan_type_unknown", "plan_type is missing or unknown.", "plan_type"
            )

        if row.get("coverage_status") == "terminated" and is_blank(row.get("coverage_end_date")):
            _add_issue(
                issues,
                row_number,
                "warning",
                "terminated_missing_coverage_end_date",
                "coverage_status is terminated but coverage_end_date is blank.",
                "coverage_end_date",
            )

    if {"member_id", "first_name", "last_name", "date_of_birth"}.issubset(working_normalized.columns):
        _add_duplicate_identity_issues(working_normalized, issues)
    if {"member_id", "plan_id", "coverage_start_date", "coverage_end_date"}.issubset(working_normalized.columns):
        _add_duplicate_coverage_warnings(working_normalized, issues)
    if {"plan_id", "plan_name", "plan_type"}.issubset(working_normalized.columns):
        _add_plan_conflict_warnings(working_normalized, issues)
    _attach_source_columns(issues, field_source_columns)
    return ValidationResult(normalized_df=normalized, issues=issues)


def _add_duplicate_identity_issues(normalized: pd.DataFrame, issues: list[ValidationIssue]) -> None:
    comparable = normalized.dropna(subset=["member_id"])
    for member_id, group in comparable.groupby("member_id", dropna=True):
        identities = group[["first_name", "last_name", "date_of_birth"]].drop_duplicates()
        if len(identities) <= 1:
            continue
        for row_number in group["source_row_number"]:
            _add_issue(
                issues,
                int(row_number),
                "error",
                "duplicate_member_id_conflicting_identity",
                f"member_id {member_id} appears with conflicting identity values.",
                "member_id",
            )


def _add_duplicate_coverage_warnings(normalized: pd.DataFrame, issues: list[ValidationIssue]) -> None:
    fields = ["member_id", "plan_id", "coverage_start_date", "coverage_end_date"]
    comparable = normalized.dropna(subset=["member_id", "plan_id", "coverage_start_date"])
    duplicate_mask = comparable.duplicated(subset=fields, keep=False)
    for row_number in comparable.loc[duplicate_mask, "source_row_number"]:
        _add_issue(
            issues,
            int(row_number),
            "warning",
            "duplicate_coverage_period",
            "Same member appears more than once with the same plan/date range.",
            "member_id",
        )


def _add_plan_conflict_warnings(normalized: pd.DataFrame, issues: list[ValidationIssue]) -> None:
    comparable = normalized.dropna(subset=["plan_id"])
    for plan_id, group in comparable.groupby("plan_id", dropna=True):
        attributes = group[["plan_name", "plan_type"]].drop_duplicates()
        if len(attributes) <= 1:
            continue
        for row_number in group["source_row_number"]:
            _add_issue(
                issues,
                int(row_number),
                "warning",
                "plan_id_conflicting_attributes",
                f"plan_id {plan_id} has conflicting plan_name or plan_type values.",
                "plan_id",
            )


def issue_summary(issues_df: pd.DataFrame) -> pd.DataFrame:
    if issues_df.empty:
        return pd.DataFrame(columns=["severity", "issue_code", "row_count"])
    return (
        issues_df.groupby(["severity", "issue_code"], as_index=False)["source_row_number"]
        .nunique()
        .rename(columns={"source_row_number": "row_count"})
        .sort_values(["severity", "row_count"], ascending=[True, False])
    )
