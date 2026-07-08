from __future__ import annotations

from typing import Any


def _profile_lookup(profiles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(profile.get("column_name") or ""): profile for profile in profiles}


def _target_label(mapping: dict[str, Any]) -> str:
    return f"{mapping.get('target_table')}.{mapping.get('target_field')}"


def _unused_recommendation(profile: dict[str, Any] | None) -> str:
    if not profile:
        return "Review before ignoring: column was not profiled."
    inferred_type = str(profile.get("inferred_type") or "unknown")
    if inferred_type in {"identifier", "date", "email", "phone"}:
        return f"Review before ignoring: column looks like {inferred_type} data."
    if inferred_type == "enum":
        return "Review before ignoring: low-cardinality values may represent business status or category data."
    return "Low risk to ignore if not needed by the target schema."


def build_source_coverage(
    source_columns: list[str],
    profiles: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    profiles_by_column = _profile_lookup(profiles)
    mapped_targets: dict[str, list[str]] = {}
    approved_targets: dict[str, list[str]] = {}

    for mapping in mappings:
        source_column = str(mapping.get("source_column") or "").strip()
        if not source_column:
            continue
        mapped_targets.setdefault(source_column, []).append(_target_label(mapping))
        if mapping.get("approved"):
            approved_targets.setdefault(source_column, []).append(_target_label(mapping))

    rows: list[dict[str, Any]] = []
    for source_column in source_columns:
        profile = profiles_by_column.get(source_column)
        mapped = sorted(mapped_targets.get(source_column, []))
        approved = sorted(approved_targets.get(source_column, []))
        if approved:
            coverage_status = "approved_mapped"
            recommendation = "Used by approved target mapping."
        elif mapped:
            coverage_status = "suggested_mapped"
            recommendation = "Suggested for at least one target field but not approved yet."
        else:
            coverage_status = "unused"
            recommendation = _unused_recommendation(profile)

        rows.append(
            {
                "source_column": source_column,
                "normalized_name": profile.get("normalized_name") if profile else "",
                "inferred_type": profile.get("inferred_type") if profile else "",
                "null_rate": profile.get("null_rate") if profile else None,
                "unique_rate": profile.get("unique_rate") if profile else None,
                "coverage_status": coverage_status,
                "mapped_targets": ", ".join(mapped),
                "approved_targets": ", ".join(approved),
                "review_recommendation": recommendation,
            }
        )
    return rows


def source_coverage_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    unused_count = sum(1 for row in rows if row.get("coverage_status") == "unused")
    approved_count = sum(1 for row in rows if row.get("coverage_status") == "approved_mapped")
    suggested_count = sum(1 for row in rows if row.get("coverage_status") == "suggested_mapped")
    review_unused_count = sum(
        1
        for row in rows
        if row.get("coverage_status") == "unused"
        and str(row.get("review_recommendation") or "").startswith("Review before ignoring")
    )
    return {
        "source_columns": len(rows),
        "approved_mapped_columns": approved_count,
        "suggested_only_columns": suggested_count,
        "unused_columns": unused_count,
        "unused_columns_requiring_review": review_unused_count,
    }


def unused_source_columns(rows: list[dict[str, Any]]) -> list[str]:
    return [
        str(row.get("source_column") or "")
        for row in rows
        if row.get("coverage_status") == "unused"
    ]
