from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from onboarding.profiler import normalize_token
from onboarding.schema import (
    COVERAGE_STATUS_NORMALIZATION,
    GENDER_NORMALIZATION,
    PLAN_TYPE_NORMALIZATION,
    RELATIONSHIP_NORMALIZATION,
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


def normalize_canonical_frame(flat_df: pd.DataFrame) -> pd.DataFrame:
    normalized = flat_df.copy()
    text_fields = [
        "member_id",
        "first_name",
        "last_name",
        "plan_id",
        "plan_name",
        "carrier_name",
        "subscriber_id",
    ]
    for field in text_fields:
        normalized[field] = normalized[field].map(_clean_text)

    for field in ["date_of_birth", "coverage_start_date", "coverage_end_date"]:
        normalized[field] = normalized[field].map(_parse_date)

    normalized["gender"] = normalized["gender"].map(lambda value: _normalize_enum(value, GENDER_NORMALIZATION))
    normalized["coverage_status"] = normalized["coverage_status"].map(
        lambda value: _normalize_enum(value, COVERAGE_STATUS_NORMALIZATION)
    )
    normalized["relationship_to_subscriber"] = normalized["relationship_to_subscriber"].map(
        lambda value: _normalize_enum(value, RELATIONSHIP_NORMALIZATION)
    )
    normalized["plan_type"] = normalized["plan_type"].map(lambda value: _normalize_enum(value, PLAN_TYPE_NORMALIZATION))
    normalized["email"] = normalized["email"].map(_normalize_email)
    normalized["phone"] = normalized["phone"].map(_normalize_phone)
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
) -> ValidationResult:
    normalized = normalize_canonical_frame(flat_df)
    issues: list[ValidationIssue] = []
    known_member_ids = {str(value) for value in normalized["member_id"].dropna().tolist() if str(value).strip()}

    for idx, row in normalized.iterrows():
        raw = flat_df.iloc[idx]
        row_number = int(row["source_row_number"])

        required_fields = [
            "member_id",
            "first_name",
            "last_name",
            "date_of_birth",
            "plan_id",
            "plan_name",
            "coverage_start_date",
            "coverage_status",
            "relationship_to_subscriber",
        ]
        for field in required_fields:
            if is_blank(row[field]):
                _add_issue(
                    issues,
                    row_number,
                    "error",
                    f"{field}_missing_or_invalid",
                    f"{field} is missing or invalid.",
                    field,
                )

        dob = row["date_of_birth"]
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

        coverage_start = row["coverage_start_date"]
        coverage_end = row["coverage_end_date"]
        if coverage_start and coverage_end and coverage_end < coverage_start:
            _add_issue(
                issues,
                row_number,
                "error",
                "coverage_end_before_start",
                "coverage_end_date is before coverage_start_date.",
                "coverage_end_date",
            )

        relationship = row["relationship_to_subscriber"]
        subscriber_id = row["subscriber_id"]
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

        if is_blank(raw.get("email")):
            _add_issue(issues, row_number, "warning", "email_missing", "email is missing.", "email")
        elif is_blank(row["email"]):
            _add_issue(issues, row_number, "warning", "email_invalid", "email format is invalid.", "email")

        if not is_blank(raw.get("phone")) and is_blank(row["phone"]):
            _add_issue(issues, row_number, "warning", "phone_invalid", "phone format is invalid.", "phone")

        if is_blank(row["gender"]):
            _add_issue(issues, row_number, "warning", "gender_unknown", "gender is missing or unknown.", "gender")

        if is_blank(row["plan_type"]):
            _add_issue(
                issues, row_number, "warning", "plan_type_unknown", "plan_type is missing or unknown.", "plan_type"
            )

        if row["coverage_status"] == "terminated" and is_blank(row["coverage_end_date"]):
            _add_issue(
                issues,
                row_number,
                "warning",
                "terminated_missing_coverage_end_date",
                "coverage_status is terminated but coverage_end_date is blank.",
                "coverage_end_date",
            )

    _add_duplicate_identity_issues(normalized, issues)
    _add_duplicate_coverage_warnings(normalized, issues)
    _add_plan_conflict_warnings(normalized, issues)
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
