from __future__ import annotations

from typing import Any

from onboarding.rules_mapper import score_value_profile
from onboarding.schema import TARGET_FIELDS_BY_KEY, TargetField

BLOCKING_ALIGNMENT_STATUSES = {"mismatch", "unknown_source"}


def _profile_lookup(profiles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(profile.get("column_name") or ""): profile for profile in profiles}


def _append_text(existing: Any, addition: str) -> str:
    current = str(existing or "").strip()
    if not addition:
        return current
    if addition in current:
        return current
    return f"{current} {addition}".strip()


def _append_flag(flags: Any, flag: str) -> list[str]:
    if isinstance(flags, str):
        raw_flags = flags.split(",")
    else:
        raw_flags = flags or []
    values = [str(value).strip() for value in raw_flags if str(value).strip()]
    if flag not in values:
        values.append(flag)
    return values


def _alignment_from_score(value_score: int) -> str:
    if value_score < 0:
        return "mismatch"
    if value_score < 20:
        return "weak"
    return "aligned"


def evaluate_type_alignment(profile: dict[str, Any], target: TargetField) -> tuple[str, int, str]:
    value_score, reason = score_value_profile(profile, target.field)
    return _alignment_from_score(value_score), value_score, reason


def apply_mapping_type_alignment(
    mappings: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    profiles_by_column = _profile_lookup(profiles)
    annotated: list[dict[str, Any]] = []

    for mapping in mappings:
        row = dict(mapping)
        target_key = (str(row.get("target_table") or ""), str(row.get("target_field") or ""))
        target = TARGET_FIELDS_BY_KEY.get(target_key)
        source_column = str(row.get("source_column") or "").strip()

        if target is not None:
            row["target_data_type"] = target.data_type
            row["target_validation_kind"] = target.validation_kind

        if not source_column:
            row["source_inferred_type"] = ""
            row["type_alignment"] = "not_mapped"
            row["type_alignment_reason"] = "No source column selected."
            annotated.append(row)
            continue

        profile = profiles_by_column.get(source_column)
        if profile is None or target is None:
            row["source_inferred_type"] = ""
            row["type_alignment"] = "unknown_source"
            row["type_alignment_reason"] = "Selected source column is not present in the active source profile."
            row["needs_review"] = True
            row["review_flags"] = _append_flag(row.get("review_flags"), "unknown_source_column")
            row["review_reason"] = _append_text(row.get("review_reason"), row["type_alignment_reason"])
            annotated.append(row)
            continue

        status, score, reason = evaluate_type_alignment(profile, target)
        row["source_inferred_type"] = profile.get("inferred_type") or ""
        row["type_alignment"] = status
        row["type_alignment_score"] = score
        row["type_alignment_reason"] = reason

        if status == "mismatch":
            row["needs_review"] = True
            row["review_flags"] = _append_flag(row.get("review_flags"), "target_type_mismatch")
            row["review_reason"] = _append_text(row.get("review_reason"), reason)
        elif status == "weak":
            row["needs_review"] = True
            row["review_flags"] = _append_flag(row.get("review_flags"), "weak_type_alignment")
            row["review_reason"] = _append_text(row.get("review_reason"), reason)

        annotated.append(row)

    return annotated


def blocking_mapping_alignment_issues(mappings: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for mapping in mappings:
        if not mapping.get("approved") or not mapping.get("source_column"):
            continue
        if mapping.get("type_alignment") not in BLOCKING_ALIGNMENT_STATUSES:
            continue
        target = f"{mapping.get('target_table')}.{mapping.get('target_field')}"
        source = mapping.get("source_column")
        reason = mapping.get("type_alignment_reason") or "Source profile conflicts with target type."
        issues.append(f"{target} mapped to {source}: {reason}")
    return issues
