from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from onboarding.profiler import normalize_column_name, normalize_token
from onboarding.schema import FIELD_ALIASES, TARGET_FIELDS_BY_FIELD, TARGET_SCHEMA, TargetField

AMBIGUOUS_EXACT_TERMS = {
    "id": "Could be member, subscriber, plan, employee, or internal row ID.",
    "date": "Too generic to identify a business date.",
    "status": "Could mean employment, member, eligibility, plan, or coverage status.",
    "type": "Could mean plan type, member type, relationship type, or coverage type.",
    "code": "Could mean plan code, group code, status code, or internal ID.",
}

AMBIGUOUS_PHRASES = {
    "employee id": "May be member_id for employees, but dependents may require subscriber_id.",
    "subscriber id": "May be member_id for self rows or subscriber_id for dependent rows.",
    "start date": "Could mean hire date, plan start date, or coverage start date.",
    "end date": "Could mean employment end, plan end, or coverage end date.",
}


def _target_aliases(target_field: str) -> list[str]:
    aliases = [target_field.replace("_", " ")]
    aliases.extend(FIELD_ALIASES.get(target_field, []))
    return [normalize_column_name(alias) for alias in aliases]


def _token_overlap(source: str, alias: str) -> float:
    source_tokens = set(source.split())
    alias_tokens = set(alias.split())
    if not source_tokens or not alias_tokens:
        return 0.0
    return len(source_tokens & alias_tokens) / len(alias_tokens)


def score_name_match(source_normalized: str, target_field: str) -> tuple[int, str]:
    aliases = _target_aliases(target_field)
    if source_normalized in aliases:
        return 70, f"Exact alias match for {target_field}."

    best_ratio = max(SequenceMatcher(None, source_normalized, alias).ratio() for alias in aliases)
    if best_ratio >= 0.84:
        return 60, f"Strong fuzzy match to a {target_field} alias."

    best_overlap = max(_token_overlap(source_normalized, alias) for alias in aliases)
    if best_overlap >= 0.65:
        return 45, f"Partial token match to a {target_field} alias."
    if best_overlap >= 0.35:
        return 25, f"Weak token overlap with a {target_field} alias."

    return 0, "No meaningful name match."


def _sample_values(profile: dict[str, Any]) -> list[str]:
    samples = list(profile.get("sample_values") or [])
    top_values = list((profile.get("top_values") or {}).keys())
    return [str(value) for value in samples + top_values if str(value).strip()]


def _code_like_rate(values: list[str]) -> float:
    if not values:
        return 0.0
    code_like = 0
    for value in values:
        text = str(value).strip()
        has_digit = any(char.isdigit() for char in text)
        has_alpha = any(char.isalpha() for char in text)
        compact = text.replace("-", "").replace("_", "").replace(" ", "")
        if has_digit and has_alpha and len(compact) <= 24:
            code_like += 1
        elif text.upper() == text and len(compact) <= 16:
            code_like += 1
    return code_like / len(values)


def _date_profile_score(profile: dict[str, Any], target: TargetField) -> tuple[int, str]:
    rate = float(profile.get("date_parse_rate") or 0.0)
    if rate >= 0.85:
        if target.validation_kind == "date_of_birth":
            return 30, "Values parse as dates and can support date of birth validation."
        return 30, "Values strongly parse as dates."
    if rate >= 0.5:
        return 20, "Many values parse as dates."
    if rate >= 0.15:
        return 10, "Some values parse as dates."
    return -10, "Values do not look date-like."


def score_value_profile(profile: dict[str, Any], target_field: str) -> tuple[int, str]:
    target = TARGET_FIELDS_BY_FIELD[target_field]
    inferred_type = str(profile.get("inferred_type") or "")
    unique_rate = float(profile.get("unique_rate") or 0.0)
    null_rate = float(profile.get("null_rate") or 0.0)
    enum_matches = profile.get("known_enum_matches") or {}
    values = _sample_values(profile)

    if target.data_type == "date":
        return _date_profile_score(profile, target)

    if target.data_type == "email":
        rate = float(profile.get("email_pattern_rate") or 0.0)
        if rate >= 0.8:
            return 30, "Most non-null values look like emails."
        if rate >= 0.3:
            return 15, "Some values look like emails."
        return -10, "Values do not look email-like."

    if target.data_type == "phone":
        rate = float(profile.get("phone_pattern_rate") or 0.0)
        if rate >= 0.8:
            return 30, "Most non-null values look like phone numbers."
        if rate >= 0.3:
            return 15, "Some values look like phone numbers."
        return -10, "Values do not look phone-like."

    if target.data_type == "enum":
        if target_field in enum_matches:
            return 30, f"Values normalize to known {target_field} values."
        if inferred_type == "enum":
            return 15, "Values are low-cardinality and enum-like."
        return -10, f"Values do not look like {target_field} values."

    if target.validation_kind == "member_identifier":
        if null_rate <= 0.05 and unique_rate >= 0.85:
            return 30, "Values are mostly non-null and highly unique."
        if unique_rate >= 0.5:
            return 20, "Values are moderately unique and ID-like enough to consider."
        if inferred_type == "enum":
            return -10, "Values are low-cardinality and unlikely to be member IDs."
        return 10, "Values provide weak identifier evidence."

    if target.validation_kind == "subscriber_identifier":
        if null_rate <= 0.2 and unique_rate >= 0.35:
            return 25, "Values are ID-like and may repeat across dependents."
        if unique_rate >= 0.15:
            return 15, "Values have some repeated identifier structure."
        return 5, "Values provide weak subscriber identifier evidence."

    if target.validation_kind == "plan_identifier":
        code_rate = _code_like_rate(values)
        if code_rate >= 0.6 and unique_rate <= 0.4:
            return 30, "Values look like repeated plan codes."
        if inferred_type in {"identifier", "enum"} and unique_rate <= 0.5:
            return 20, "Values look like reusable plan identifiers."
        return 5, "Values provide weak plan identifier evidence."

    if target.data_type == "text":
        if inferred_type in {"date", "email", "phone", "numeric"}:
            return -10, "Values contradict a text/name field."
        if null_rate <= 0.1:
            return 20, "Values are mostly present and text-like."
        return 10, "Values are text-like but incomplete."

    return 0, "No field-specific value evidence."


def ambiguity_penalty(source_normalized: str) -> tuple[int, list[str], str | None]:
    flags: list[str] = []
    reasons: list[str] = []

    if source_normalized in AMBIGUOUS_EXACT_TERMS:
        flags.append(f"ambiguous_{source_normalized.replace(' ', '_')}")
        reasons.append(AMBIGUOUS_EXACT_TERMS[source_normalized])

    if source_normalized in AMBIGUOUS_PHRASES:
        flags.append(f"ambiguous_{source_normalized.replace(' ', '_')}")
        reasons.append(AMBIGUOUS_PHRASES[source_normalized])

    penalty = -20 if flags else 0
    return penalty, flags, " ".join(reasons) if reasons else None


def _status_for_score(score: int) -> str:
    if score >= 85:
        return "strong_suggestion"
    if score >= 70:
        return "suggested"
    if score >= 50:
        return "weak_suggestion"
    return "unmapped"


def _type_alignment_from_value_score(value_score: int) -> str:
    if value_score < 0:
        return "mismatch"
    if value_score < 20:
        return "weak"
    return "aligned"


def _candidate_scores(profiles: list[dict[str, Any]], target: TargetField) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for profile in profiles:
        name_score, name_reason = score_name_match(profile["normalized_name"], target.field)
        value_score, value_reason = score_value_profile(profile, target.field)
        penalty, flags, review_reason = ambiguity_penalty(profile["normalized_name"])
        raw_score = name_score + value_score + penalty
        score = max(0, min(100, raw_score))
        candidates.append(
            {
                "source_column": profile["column_name"],
                "source_normalized": profile["normalized_name"],
                "source_inferred_type": profile.get("inferred_type") or "",
                "type_alignment": _type_alignment_from_value_score(value_score),
                "type_alignment_score": value_score,
                "type_alignment_reason": value_reason,
                "confidence": int(score),
                "name_score": name_score,
                "value_profile_score": value_score,
                "ambiguity_penalty": penalty,
                "conflict_penalty": 0,
                "review_flags": flags.copy(),
                "reason": f"{name_reason} {value_reason}".strip(),
                "review_reason": review_reason,
            }
        )
    return sorted(candidates, key=lambda item: item["confidence"], reverse=True)


def generate_rules_based_mappings(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []

    for target in TARGET_SCHEMA:
        if target.generated:
            continue

        candidates = _candidate_scores(profiles, target)
        best = candidates[0] if candidates else None
        if not best or best["confidence"] < 50:
            mappings.append(
                {
                    "target_table": target.table,
                    "target_field": target.field,
                    "required": target.required,
                    "target_data_type": target.data_type,
                    "target_validation_kind": target.validation_kind,
                    "source_inferred_type": "",
                    "type_alignment": "not_mapped",
                    "type_alignment_score": 0,
                    "type_alignment_reason": "No source column selected.",
                    "source_column": "",
                    "confidence": 0,
                    "mapping_status": "unmapped",
                    "needs_review": target.required,
                    "review_flags": ["required_unmapped"] if target.required else [],
                    "reason": "No source column reached the mapping threshold.",
                    "review_reason": "Required field is unmapped." if target.required else "",
                    "approved": False,
                    "score_breakdown": {},
                }
            )
            continue

        review_flags = list(best["review_flags"])
        review_reason = best["review_reason"] or ""
        if best["type_alignment"] == "mismatch":
            review_flags.append("target_type_mismatch")
            review_reason = f"{review_reason} {best['type_alignment_reason']}".strip()
        elif best["type_alignment"] == "weak":
            review_flags.append("weak_type_alignment")
            review_reason = f"{review_reason} {best['type_alignment_reason']}".strip()

        needs_review = best["confidence"] < 85 or bool(review_flags)
        mappings.append(
            {
                "target_table": target.table,
                "target_field": target.field,
                "required": target.required,
                "target_data_type": target.data_type,
                "target_validation_kind": target.validation_kind,
                "source_inferred_type": best["source_inferred_type"],
                "type_alignment": best["type_alignment"],
                "type_alignment_score": best["type_alignment_score"],
                "type_alignment_reason": best["type_alignment_reason"],
                "source_column": best["source_column"],
                "confidence": best["confidence"],
                "mapping_status": _status_for_score(best["confidence"]),
                "needs_review": needs_review,
                "review_flags": sorted(set(review_flags)),
                "reason": best["reason"],
                "review_reason": review_reason,
                "approved": False,
                "score_breakdown": {
                    "name_score": best["name_score"],
                    "value_profile_score": best["value_profile_score"],
                    "ambiguity_penalty": best["ambiguity_penalty"],
                    "conflict_penalty": best["conflict_penalty"],
                },
            }
        )

    _apply_conflict_penalties(mappings)
    return mappings


def _apply_conflict_penalties(mappings: list[dict[str, Any]]) -> None:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for mapping in mappings:
        source = mapping.get("source_column") or ""
        if source:
            by_source.setdefault(source, []).append(mapping)

    for source, source_mappings in by_source.items():
        distinct_fields = {mapping.get("target_field") for mapping in source_mappings}
        if len(source_mappings) <= 1 or len(distinct_fields) <= 1:
            continue
        sorted_mappings = sorted(source_mappings, key=lambda item: item["confidence"], reverse=True)
        for mapping in sorted_mappings[1:]:
            mapping["confidence"] = max(0, int(mapping["confidence"]) - 10)
            mapping["mapping_status"] = _status_for_score(int(mapping["confidence"]))
            mapping["needs_review"] = True
            flags = set(mapping.get("review_flags") or [])
            flags.add("multiple_candidate_targets")
            mapping["review_flags"] = sorted(flags)
            existing = mapping.get("review_reason") or ""
            conflict_reason = f"{source} is also suggested for another target field."
            mapping["review_reason"] = f"{existing} {conflict_reason}".strip()
            mapping.setdefault("score_breakdown", {})["conflict_penalty"] = -10


def mapping_payload_for_display(mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = []
    for mapping in mappings:
        clean = dict(mapping)
        clean["review_flags"] = ", ".join(mapping.get("review_flags") or [])
        clean["score_breakdown"] = str(mapping.get("score_breakdown") or {})
        payload.append(clean)
    return payload
