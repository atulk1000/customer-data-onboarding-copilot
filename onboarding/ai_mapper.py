from __future__ import annotations

import json
import os
import ssl
from typing import Any

from onboarding.mapping_quality import apply_mapping_type_alignment
from onboarding.schema import TARGET_FIELDS_BY_KEY, target_schema_payload

DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_REASONING_EFFORT = "low"
SUPPORTED_REASONING_EFFORTS = {"minimal", "low", "medium", "high"}


class OpenAIConfigurationError(RuntimeError):
    pass


class AIMapperValidationError(ValueError):
    pass


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(override=True)


def build_ai_mapping_payload(
    profiles: list[dict[str, Any]],
    rules_based_suggestions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "task": "Map customer healthcare eligibility file columns into the canonical target schema.",
        "target_schema": target_schema_payload(),
        "scoring_guidance": {
            "name_match_score": "0-70",
            "value_profile_score": "0-30",
            "ambiguity_penalty": "0 to -20",
            "conflict_penalty": "0 to -10",
            "confidence_meaning": "0-100 likelihood that source_column maps to target_field",
            "review_rule": "Set needs_review true for ambiguous healthcare terms even when confidence is high.",
        },
        "known_ambiguous_terms": [
            "ID",
            "Employee ID",
            "Subscriber ID",
            "Status",
            "Date",
            "Start Date",
            "End Date",
            "Type",
            "Code",
        ],
        "source_columns": [
            {
                "column_name": profile.get("column_name"),
                "normalized_name": profile.get("normalized_name"),
                "inferred_type": profile.get("inferred_type"),
                "null_rate": profile.get("null_rate"),
                "unique_rate": profile.get("unique_rate"),
                "sample_values": list(profile.get("sample_values") or [])[:5],
                "top_values": dict(list((profile.get("top_values") or {}).items())[:5]),
                "date_parse_rate": profile.get("date_parse_rate"),
                "email_pattern_rate": profile.get("email_pattern_rate"),
                "phone_pattern_rate": profile.get("phone_pattern_rate"),
                "numeric_parse_rate": profile.get("numeric_parse_rate"),
                "known_enum_matches": profile.get("known_enum_matches"),
            }
            for profile in profiles
        ],
        "rules_based_suggestions": rules_based_suggestions or [],
    }


def validate_ai_mapping_response(
    parsed: dict[str, Any],
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict):
        raise AIMapperValidationError("AI mapping response must be a JSON object.")

    mappings = parsed.get("mappings")
    if not isinstance(mappings, list):
        raise AIMapperValidationError("AI mapping response must include a mappings list.")

    source_columns = {str(profile.get("column_name")) for profile in profiles}
    seen_targets: set[tuple[str, str]] = set()
    validated: list[dict[str, Any]] = []

    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict):
            raise AIMapperValidationError(f"Mapping at index {index} must be an object.")

        target_table = str(mapping.get("target_table") or "").strip()
        target_field = str(mapping.get("target_field") or "").strip()
        source_column = str(mapping.get("source_column") or "").strip()
        target_key = (target_table, target_field)

        if target_key not in TARGET_FIELDS_BY_KEY:
            raise AIMapperValidationError(
                f"AI returned unknown target field: {target_table}.{target_field}."
            )
        if source_column and source_column not in source_columns:
            raise AIMapperValidationError(
                f"AI returned unknown source column for {target_table}.{target_field}: {source_column}."
            )
        if target_key in seen_targets:
            raise AIMapperValidationError(
                f"AI returned duplicate mapping for {target_table}.{target_field}."
            )
        seen_targets.add(target_key)

        confidence = mapping.get("confidence")
        if not isinstance(confidence, int) or confidence < 0 or confidence > 100:
            raise AIMapperValidationError(
                f"AI returned invalid confidence for {target_table}.{target_field}: {confidence}."
            )

        needs_review = mapping.get("needs_review")
        if not isinstance(needs_review, bool):
            raise AIMapperValidationError(
                f"AI returned non-boolean needs_review for {target_table}.{target_field}."
            )

        review_flags = mapping.get("review_flags")
        if not isinstance(review_flags, list) or not all(
            isinstance(flag, str) for flag in review_flags
        ):
            raise AIMapperValidationError(
                f"AI returned invalid review_flags for {target_table}.{target_field}."
            )

        rationale = str(mapping.get("rationale") or "").strip()
        transformation_hint = str(mapping.get("transformation_hint") or "").strip()
        validated.append(
            {
                "target_table": target_table,
                "target_field": target_field,
                "required": TARGET_FIELDS_BY_KEY[target_key].required,
                "target_data_type": TARGET_FIELDS_BY_KEY[target_key].data_type,
                "target_validation_kind": TARGET_FIELDS_BY_KEY[target_key].validation_kind,
                "source_column": source_column,
                "confidence": confidence,
                "mapping_status": "ai_suggested" if source_column else "unmapped",
                "needs_review": needs_review or confidence < 85,
                "review_flags": review_flags,
                "reason": rationale,
                "review_reason": "; ".join(review_flags),
                "approved": False,
                "transformation_hint": transformation_hint,
            }
        )

    for key in ["unmapped_required_fields", "ambiguous_mappings"]:
        value = parsed.get(key)
        if value is None:
            raise AIMapperValidationError(f"AI mapping response must include {key}.")
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise AIMapperValidationError(f"AI mapping response field {key} must be a list of strings.")

    return apply_mapping_type_alignment(validated, profiles)


def _response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["mappings", "unmapped_required_fields", "ambiguous_mappings"],
        "properties": {
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "target_table",
                        "target_field",
                        "source_column",
                        "confidence",
                        "needs_review",
                        "review_flags",
                        "rationale",
                        "transformation_hint",
                    ],
                    "properties": {
                        "target_table": {"type": "string"},
                        "target_field": {"type": "string"},
                        "source_column": {"type": "string"},
                        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                        "needs_review": {"type": "boolean"},
                        "review_flags": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                        "transformation_hint": {"type": "string"},
                    },
                },
            },
            "unmapped_required_fields": {"type": "array", "items": {"type": "string"}},
            "ambiguous_mappings": {"type": "array", "items": {"type": "string"}},
        },
    }


def _extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return str(output_text).strip()
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                return str(text).strip()
            if isinstance(content, dict) and content.get("text"):
                return str(content["text"]).strip()
    raise ValueError("OpenAI returned no text output.")


def _httpx_verify_context() -> str | ssl.SSLContext:
    configured_cert_file = os.environ.get("SSL_CERT_FILE")
    if configured_cert_file:
        return configured_cert_file
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        import certifi

        return certifi.where()


def _openai_model() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL


def _reasoning_effort() -> str | None:
    effort = os.environ.get("OPENAI_REASONING_EFFORT", DEFAULT_REASONING_EFFORT).strip().lower()
    if not effort:
        return None
    if effort not in SUPPORTED_REASONING_EFFORTS:
        raise OpenAIConfigurationError(
            "OPENAI_REASONING_EFFORT must be one of: "
            + ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
            + "."
        )
    return effort


def _response_create_kwargs(model: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if model.startswith("gpt-5"):
        effort = _reasoning_effort()
        if effort:
            kwargs["reasoning"] = {"effort": effort}
    else:
        kwargs["temperature"] = 0.1
    return kwargs


def suggest_mappings_with_ai(
    profiles: list[dict[str, Any]],
    rules_based_suggestions: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    _load_dotenv_if_available()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise OpenAIConfigurationError(
            "OPENAI_API_KEY is not configured. Set it in .env or the environment before using AI-Assisted Mapping."
        )

    import httpx
    from openai import APIConnectionError, AuthenticationError, OpenAI

    http_client = httpx.Client(verify=_httpx_verify_context())
    client = OpenAI(api_key=api_key, http_client=http_client)
    payload = build_ai_mapping_payload(profiles, rules_based_suggestions)
    selected_model = model or _openai_model()
    system_message = (
        "You are a careful data onboarding mapping assistant. Return only JSON. "
        "Suggest mappings, explain ambiguity, and never claim a mapping is approved."
    )
    try:
        response = client.responses.create(
            model=selected_model,
            input=[
                {"role": "developer", "content": system_message},
                {"role": "user", "content": json.dumps(payload, indent=2)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "ai_mapping_suggestions",
                    "schema": _response_schema(),
                    "strict": True,
                }
            },
            **_response_create_kwargs(selected_model),
        )
    except AuthenticationError as exc:
        raise OpenAIConfigurationError(
            "OpenAI rejected OPENAI_API_KEY. Check the key in .env and try AI-Assisted Mapping again."
        ) from exc
    except APIConnectionError as exc:
        raise OpenAIConfigurationError(
            "Could not connect to the OpenAI API. Check network access or SSL certificate configuration."
        ) from exc
    raw_text = _extract_response_text(response)
    parsed = json.loads(raw_text)
    return validate_ai_mapping_response(parsed, profiles)
