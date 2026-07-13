from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_TEMPLATE_DIR = Path("data") / "mapping_templates"

MAPPING_FIELDS = [
    "target_table",
    "target_field",
    "required",
    "target_data_type",
    "target_validation_kind",
    "source_column",
    "confidence",
    "mapping_status",
    "needs_review",
    "review_flags",
    "reason",
    "review_reason",
    "score_breakdown",
    "transformation_hint",
    "source_columns",
    "transformation_steps",
    "failure_policy",
    "failure_default",
    "transformation_approved",
]


class MappingTemplateCompatibilityError(ValueError):
    pass


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "mapping-template"


def _clean_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _clean_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_clean_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _clean_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    return {field: _clean_value(mapping.get(field)) for field in MAPPING_FIELDS if field in mapping}


def _next_template_version(template_name: str, template_dir: Path) -> int:
    expected_slug = _slugify(template_name)
    versions = []
    for path in template_dir.glob(f"{expected_slug}__*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if _slugify(str(payload.get("template_name") or "")) != expected_slug:
            continue
        try:
            versions.append(int(payload.get("template_version") or 1))
        except (TypeError, ValueError):
            continue
    return max(versions, default=0) + 1


def mapping_configuration_checksum(mappings: list[dict[str, Any]]) -> str:
    import hashlib

    clean_mappings = [_clean_mapping(mapping) for mapping in mappings]
    payload = json.dumps(clean_mappings, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_mapping_template(
    *,
    template_name: str,
    schema_name: str,
    schema_version: str,
    source_columns: list[str],
    mappings: list[dict[str, Any]],
    contract_key: str = "",
    contract_checksum: str = "",
    template_version: int | None = None,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    template_dir.mkdir(parents=True, exist_ok=True)
    saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    resolved_version = template_version or _next_template_version(template_name, template_dir)
    payload = {
        "template_name": template_name.strip(),
        "template_version": resolved_version,
        "contract_key": contract_key,
        "contract_checksum": contract_checksum,
        "schema_name": schema_name,
        "schema_version": schema_version,
        "saved_at": saved_at,
        "source_columns": source_columns,
        "mappings": [_clean_mapping(mapping) for mapping in mappings],
    }
    file_name = f"{_slugify(template_name)}__{_slugify(schema_version)}__v{resolved_version}.json"
    path = template_dir / file_name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {**payload, "file_name": file_name}


def list_mapping_templates(template_dir: Path = DEFAULT_TEMPLATE_DIR) -> list[dict[str, Any]]:
    if not template_dir.exists():
        return []
    templates: list[dict[str, Any]] = []
    for path in sorted(template_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        templates.append(
            {
                "file_name": path.name,
                "template_name": payload.get("template_name") or path.stem,
                "template_version": int(payload.get("template_version") or 1),
                "contract_key": payload.get("contract_key") or "",
                "contract_checksum": payload.get("contract_checksum") or "",
                "schema_name": payload.get("schema_name") or "",
                "schema_version": payload.get("schema_version") or "",
                "saved_at": payload.get("saved_at") or "",
                "source_column_count": len(payload.get("source_columns") or []),
                "mapping_count": len(payload.get("mappings") or []),
            }
        )
    return templates


def load_mapping_template(
    file_name: str,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
) -> dict[str, Any]:
    safe_name = Path(file_name).name
    path = template_dir / safe_name
    return json.loads(path.read_text(encoding="utf-8"))


def apply_mapping_template(
    template: dict[str, Any],
    current_source_columns: list[str],
    *,
    contract_key: str = "",
    schema_version: str = "",
    contract_checksum: str = "",
) -> list[dict[str, Any]]:
    template_contract_key = str(template.get("contract_key") or "")
    template_schema_version = str(template.get("schema_version") or "")
    template_checksum = str(template.get("contract_checksum") or "")
    if contract_key and template_contract_key and contract_key != template_contract_key:
        raise MappingTemplateCompatibilityError(
            f"Template contract {template_contract_key} does not match selected contract {contract_key}."
        )
    if schema_version and template_schema_version and schema_version != template_schema_version:
        raise MappingTemplateCompatibilityError(
            f"Template schema version {template_schema_version} does not match selected version {schema_version}."
        )
    if contract_checksum and template_checksum and contract_checksum != template_checksum:
        raise MappingTemplateCompatibilityError("Template contract checksum does not match the published contract.")

    current_columns = set(current_source_columns)
    loaded: list[dict[str, Any]] = []
    for mapping in template.get("mappings") or []:
        row = dict(mapping)
        source_column = str(row.get("source_column") or "").strip()
        source_columns = row.get("source_columns") or ([source_column] if source_column else [])
        source_columns = [str(column).strip() for column in source_columns if str(column).strip()]
        row["approved"] = False
        row["mapping_status"] = "template_loaded" if source_column else "unmapped"
        row["needs_review"] = True
        flags = [str(flag) for flag in row.get("review_flags") or [] if str(flag).strip()]

        missing_source_columns = [column for column in source_columns if column not in current_columns]
        if missing_source_columns:
            row["source_column"] = ""
            row["source_columns"] = []
            row["mapping_status"] = "unmapped"
            flags.append("template_source_missing")
            missing_reason = "Template source columns are not present in the current file: " + ", ".join(
                missing_source_columns
            )
            row["review_reason"] = f"{row.get('review_reason') or ''} {missing_reason}".strip()
        else:
            row["source_columns"] = source_columns
            row["review_reason"] = (
                row.get("review_reason") or "Loaded from mapping template; reviewer approval required."
            )

        row["review_flags"] = sorted(set(flags))
        loaded.append(row)
    return loaded
