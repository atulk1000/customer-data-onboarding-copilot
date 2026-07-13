from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from onboarding.schema import FIELD_ALIASES, TARGET_SCHEMA, TargetField

DEFAULT_CONTRACT_KEY = "healthcare_eligibility"
DEFAULT_CONTRACT_NAME = "Healthcare Eligibility Canonical"
DEFAULT_CONTRACT_DOMAIN = "healthcare"
DEFAULT_CONTRACT_VERSION = "1.0.0"

CONTRACT_STATUSES = {"draft", "published", "retired"}
SUPPORTED_DATA_TYPES = {
    "text",
    "identifier",
    "enum",
    "date",
    "numeric",
    "email",
    "phone",
    "boolean",
}
SUPPORTED_GENERATION_STRATEGIES = {"", "deterministic_business_key"}
SUPPORTED_VALIDATION_KINDS = {
    "allowed_values",
    "boolean",
    "coverage_end_date",
    "coverage_start_date",
    "coverage_status",
    "date_of_birth",
    "email",
    "generated_identifier",
    "gender",
    "member_identifier",
    "not_future",
    "numeric_range",
    "organization_name",
    "person_name",
    "phone",
    "plan_identifier",
    "plan_name",
    "plan_type",
    "relationship_to_subscriber",
    "required",
    "subscriber_identifier",
}

IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ContractValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class ContractRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContractVersion:
    contract_key: str
    name: str
    domain: str
    version: str
    status: str
    definition: dict[str, Any]
    checksum: str
    database_id: int | None = None
    created_by: str = ""
    created_at: str = ""
    published_by: str = ""
    published_at: str = ""
    retired_by: str = ""
    retired_at: str = ""
    lifecycle_comment: str = ""

    @property
    def target_fields(self) -> list[TargetField]:
        return target_fields_from_contract(self.definition)

    @property
    def table_names(self) -> list[str]:
        return [str(table["name"]) for table in self.definition.get("tables", [])]

    @property
    def business_keys(self) -> dict[str, list[str]]:
        return {
            str(table["name"]): [str(value) for value in table.get("business_key") or table.get("primary_key") or []]
            for table in self.definition.get("tables", [])
        }

    @property
    def primary_keys(self) -> dict[str, list[str]]:
        return {
            str(table["name"]): [str(value) for value in table.get("primary_key") or []]
            for table in self.definition.get("tables", [])
        }

    @property
    def foreign_keys(self) -> list[dict[str, str]]:
        keys: list[dict[str, str]] = []
        for table in self.definition.get("tables", []):
            for foreign_key in table.get("foreign_keys") or []:
                keys.append(
                    {
                        "table": str(table["name"]),
                        "field": str(foreign_key["field"]),
                        "references_table": str(foreign_key["references_table"]),
                        "references_field": str(foreign_key["references_field"]),
                    }
                )
        return keys

    @property
    def reconciliation_policy(self) -> dict[str, Any]:
        return dict(self.definition.get("reconciliation_policy") or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def contract_checksum(definition: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(definition).encode("utf-8")).hexdigest()


def _require_identifier(value: Any, path: str, errors: list[str]) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        errors.append(f"{path} is required.")
    elif not IDENTIFIER_RE.fullmatch(normalized):
        errors.append(f"{path} must use lowercase snake_case.")
    return normalized


def _normalize_validation_rules(field: dict[str, Any], path: str, errors: list[str]) -> list[dict[str, Any]]:
    raw_rules = field.get("validation_rules")
    if raw_rules is None:
        legacy_kind = str(field.get("validation_kind") or "").strip()
        raw_rules = [{"kind": legacy_kind, "severity": "error", "parameters": {}}] if legacy_kind else []
    if not isinstance(raw_rules, list):
        errors.append(f"{path}.validation_rules must be a list.")
        return []

    normalized: list[dict[str, Any]] = []
    for rule_index, raw_rule in enumerate(raw_rules):
        rule_path = f"{path}.validation_rules[{rule_index}]"
        if not isinstance(raw_rule, dict):
            errors.append(f"{rule_path} must be an object.")
            continue
        kind = str(raw_rule.get("kind") or "").strip()
        severity = str(raw_rule.get("severity") or "error").strip().lower()
        parameters = raw_rule.get("parameters") or {}
        if kind not in SUPPORTED_VALIDATION_KINDS:
            errors.append(f"{rule_path}.kind '{kind}' is not supported.")
        if severity not in {"error", "warning"}:
            errors.append(f"{rule_path}.severity must be error or warning.")
        if not isinstance(parameters, dict):
            errors.append(f"{rule_path}.parameters must be an object.")
            parameters = {}
        normalized.append({"kind": kind, "severity": severity, "parameters": parameters})
    return normalized


def validate_contract_definition(definition: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(definition, dict):
        raise ContractValidationError(["Contract must be a JSON object."])

    errors: list[str] = []
    contract_key = _require_identifier(definition.get("contract_key"), "contract_key", errors)
    name = str(definition.get("name") or "").strip()
    domain = str(definition.get("domain") or "").strip()
    version = str(definition.get("version") or "").strip()
    if not name:
        errors.append("name is required.")
    if not domain:
        errors.append("domain is required.")
    if not SEMVER_RE.fullmatch(version):
        errors.append("version must use semantic version format such as 1.2.0.")

    raw_tables = definition.get("tables")
    if not isinstance(raw_tables, list) or not raw_tables:
        errors.append("tables must be a non-empty list.")
        raw_tables = []

    normalized_tables: list[dict[str, Any]] = []
    table_fields: dict[str, set[str]] = {}
    seen_tables: set[str] = set()

    for table_index, raw_table in enumerate(raw_tables):
        table_path = f"tables[{table_index}]"
        if not isinstance(raw_table, dict):
            errors.append(f"{table_path} must be an object.")
            continue
        table_name = _require_identifier(raw_table.get("name"), f"{table_path}.name", errors)
        if table_name in seen_tables:
            errors.append(f"Duplicate table name: {table_name}.")
        seen_tables.add(table_name)

        raw_fields = raw_table.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields:
            errors.append(f"{table_path}.fields must be a non-empty list.")
            raw_fields = []

        normalized_fields: list[dict[str, Any]] = []
        seen_fields: set[str] = set()
        for field_index, raw_field in enumerate(raw_fields):
            field_path = f"{table_path}.fields[{field_index}]"
            if not isinstance(raw_field, dict):
                errors.append(f"{field_path} must be an object.")
                continue
            field_name = _require_identifier(raw_field.get("name"), f"{field_path}.name", errors)
            if field_name in seen_fields:
                errors.append(f"Duplicate field name in {table_name}: {field_name}.")
            seen_fields.add(field_name)

            data_type = str(raw_field.get("data_type") or raw_field.get("type") or "").strip().lower()
            if data_type not in SUPPORTED_DATA_TYPES:
                errors.append(f"{field_path}.data_type '{data_type}' is not supported.")
            required = bool(raw_field.get("required", False))
            nullable = bool(raw_field.get("nullable", not required))
            if required and nullable:
                errors.append(f"{field_path} cannot be both required and nullable.")
            generated = bool(raw_field.get("generated", False))
            generation_strategy = str(raw_field.get("generation_strategy") or "").strip()
            if generated and not generation_strategy:
                generation_strategy = "deterministic_business_key"
            if generation_strategy not in SUPPORTED_GENERATION_STRATEGIES:
                errors.append(f"{field_path}.generation_strategy '{generation_strategy}' is not supported.")
            if generation_strategy and not generated:
                errors.append(f"{field_path}.generation_strategy requires generated=true.")

            allowed_values = raw_field.get("allowed_values") or []
            aliases = raw_field.get("aliases") or []
            expected_evidence = raw_field.get("expected_evidence") or []
            if not isinstance(allowed_values, list):
                errors.append(f"{field_path}.allowed_values must be a list.")
                allowed_values = []
            if not isinstance(aliases, list):
                errors.append(f"{field_path}.aliases must be a list.")
                aliases = []
            if not isinstance(expected_evidence, list):
                errors.append(f"{field_path}.expected_evidence must be a list.")
                expected_evidence = []
            if data_type == "enum" and not allowed_values:
                errors.append(f"{field_path}.allowed_values is required for enum fields.")

            validation_rules = _normalize_validation_rules(raw_field, field_path, errors)
            validation_kind = str(raw_field.get("validation_kind") or "").strip()
            if not validation_kind and validation_rules:
                validation_kind = str(validation_rules[0]["kind"])
            if validation_kind and validation_kind not in SUPPORTED_VALIDATION_KINDS:
                errors.append(f"{field_path}.validation_kind '{validation_kind}' is not supported.")

            normalized_fields.append(
                {
                    "name": field_name,
                    "data_type": data_type,
                    "required": required,
                    "nullable": nullable,
                    "generated": generated,
                    "generation_strategy": generation_strategy,
                    "allowed_values": [str(value) for value in allowed_values],
                    "aliases": [str(value) for value in aliases],
                    "expected_evidence": [str(value) for value in expected_evidence],
                    "validation_kind": validation_kind,
                    "validation_rules": validation_rules,
                    "description": str(raw_field.get("description") or "").strip(),
                }
            )

        primary_key = [str(value).strip() for value in raw_table.get("primary_key") or []]
        business_key = [str(value).strip() for value in raw_table.get("business_key") or primary_key]
        if not primary_key:
            errors.append(f"{table_path}.primary_key must contain at least one field.")
        if not business_key:
            errors.append(f"{table_path}.business_key must contain at least one field.")
        for key_field in primary_key + business_key:
            if key_field not in seen_fields:
                errors.append(f"{table_path} key field '{key_field}' does not exist.")

        raw_foreign_keys = raw_table.get("foreign_keys") or []
        if not isinstance(raw_foreign_keys, list):
            errors.append(f"{table_path}.foreign_keys must be a list.")
            raw_foreign_keys = []
        foreign_keys: list[dict[str, str]] = []
        for foreign_index, raw_foreign_key in enumerate(raw_foreign_keys):
            foreign_path = f"{table_path}.foreign_keys[{foreign_index}]"
            if not isinstance(raw_foreign_key, dict):
                errors.append(f"{foreign_path} must be an object.")
                continue
            field_name = str(raw_foreign_key.get("field") or "").strip()
            references_table = str(raw_foreign_key.get("references_table") or "").strip()
            references_field = str(raw_foreign_key.get("references_field") or "").strip()
            if field_name not in seen_fields:
                errors.append(f"{foreign_path}.field '{field_name}' does not exist in {table_name}.")
            foreign_keys.append(
                {
                    "field": field_name,
                    "references_table": references_table,
                    "references_field": references_field,
                }
            )

        table_fields[table_name] = seen_fields
        normalized_tables.append(
            {
                "name": table_name,
                "primary_key": primary_key,
                "business_key": business_key,
                "foreign_keys": foreign_keys,
                "fields": normalized_fields,
            }
        )

    for table in normalized_tables:
        for foreign_key in table["foreign_keys"]:
            referenced_fields = table_fields.get(foreign_key["references_table"])
            if referenced_fields is None:
                errors.append(
                    f"Foreign key {table['name']}.{foreign_key['field']} references unknown table "
                    f"{foreign_key['references_table']}."
                )
            elif foreign_key["references_field"] not in referenced_fields:
                errors.append(
                    f"Foreign key {table['name']}.{foreign_key['field']} references unknown field "
                    f"{foreign_key['references_table']}.{foreign_key['references_field']}."
                )

    raw_policy = definition.get("reconciliation_policy") or {}
    if not isinstance(raw_policy, dict):
        errors.append("reconciliation_policy must be an object.")
        raw_policy = {}
    try:
        max_reject_rate = float(raw_policy.get("max_reject_rate", 0.10))
    except (TypeError, ValueError):
        max_reject_rate = -1
    if max_reject_rate < 0 or max_reject_rate > 1:
        errors.append("reconciliation_policy.max_reject_rate must be between 0 and 1.")
    reject_severity = str(raw_policy.get("max_reject_rate_severity") or "warning").strip().lower()
    if reject_severity not in {"warning", "error"}:
        errors.append("reconciliation_policy.max_reject_rate_severity must be warning or error.")

    if errors:
        raise ContractValidationError(errors)

    return {
        "contract_key": contract_key,
        "name": name,
        "domain": domain,
        "version": version,
        "tables": normalized_tables,
        "reconciliation_policy": {
            "max_reject_rate": max_reject_rate,
            "max_reject_rate_severity": reject_severity,
            "require_zero_orphans": bool(raw_policy.get("require_zero_orphans", True)),
            "require_exact_row_accounting": bool(raw_policy.get("require_exact_row_accounting", True)),
        },
    }


def target_fields_from_contract(definition: dict[str, Any]) -> list[TargetField]:
    fields: list[TargetField] = []
    for table in definition.get("tables") or []:
        table_name = str(table.get("name") or "")
        for field in table.get("fields") or []:
            fields.append(
                TargetField(
                    table=table_name,
                    field=str(field.get("name") or ""),
                    required=bool(field.get("required", False)),
                    data_type=str(field.get("data_type") or "text"),
                    nullable=bool(field.get("nullable", True)),
                    validation_kind=str(field.get("validation_kind") or ""),
                    description=str(field.get("description") or ""),
                    expected_evidence=[str(value) for value in field.get("expected_evidence") or []],
                    allowed_values=[str(value) for value in field.get("allowed_values") or []],
                    generated=bool(field.get("generated", False)),
                    aliases=[str(value) for value in field.get("aliases") or []],
                    generation_strategy=str(field.get("generation_strategy") or ""),
                    validation_rules=[dict(value) for value in field.get("validation_rules") or []],
                )
            )
    return fields


def default_contract_definition() -> dict[str, Any]:
    table_metadata: dict[str, dict[str, Any]] = {
        "members": {
            "primary_key": ["member_id"],
            "business_key": ["member_id"],
            "foreign_keys": [],
        },
        "plans": {
            "primary_key": ["plan_id"],
            "business_key": ["plan_id"],
            "foreign_keys": [],
        },
        "member_coverage": {
            "primary_key": ["coverage_id"],
            "business_key": ["coverage_id"],
            "foreign_keys": [
                {
                    "field": "member_id",
                    "references_table": "members",
                    "references_field": "member_id",
                },
                {
                    "field": "plan_id",
                    "references_table": "plans",
                    "references_field": "plan_id",
                },
            ],
        },
    }
    tables: list[dict[str, Any]] = []
    for table_name in table_metadata:
        field_rows: list[dict[str, Any]] = []
        for field in [target for target in TARGET_SCHEMA if target.table == table_name]:
            generation_strategy = "deterministic_business_key" if field.generated else ""
            field_rows.append(
                {
                    "name": field.field,
                    "data_type": field.data_type,
                    "required": field.required,
                    "nullable": field.nullable,
                    "generated": field.generated,
                    "generation_strategy": generation_strategy,
                    "allowed_values": field.allowed_values,
                    "aliases": FIELD_ALIASES.get(field.field, []),
                    "expected_evidence": field.expected_evidence,
                    "validation_kind": field.validation_kind,
                    "validation_rules": [{"kind": field.validation_kind, "severity": "error", "parameters": {}}],
                    "description": field.description,
                }
            )
        tables.append({"name": table_name, **table_metadata[table_name], "fields": field_rows})

    return validate_contract_definition(
        {
            "contract_key": DEFAULT_CONTRACT_KEY,
            "name": DEFAULT_CONTRACT_NAME,
            "domain": DEFAULT_CONTRACT_DOMAIN,
            "version": DEFAULT_CONTRACT_VERSION,
            "tables": tables,
            "reconciliation_policy": {
                "max_reject_rate": 0.10,
                "max_reject_rate_severity": "warning",
                "require_zero_orphans": True,
                "require_exact_row_accounting": True,
            },
        }
    )


def default_contract() -> ContractVersion:
    definition = default_contract_definition()
    return ContractVersion(
        contract_key=definition["contract_key"],
        name=definition["name"],
        domain=definition["domain"],
        version=definition["version"],
        status="published",
        definition=definition,
        checksum=contract_checksum(definition),
        created_by="system",
        published_by="system",
    )


def contract_from_definition(
    definition: dict[str, Any],
    *,
    status: str = "draft",
    database_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> ContractVersion:
    normalized = validate_contract_definition(definition)
    if status not in CONTRACT_STATUSES:
        raise ContractValidationError([f"Unsupported contract status: {status}."])
    metadata = metadata or {}
    return ContractVersion(
        contract_key=normalized["contract_key"],
        name=normalized["name"],
        domain=normalized["domain"],
        version=normalized["version"],
        status=status,
        definition=normalized,
        checksum=contract_checksum(normalized),
        database_id=database_id,
        created_by=str(metadata.get("created_by") or ""),
        created_at=str(metadata.get("created_at") or ""),
        published_by=str(metadata.get("published_by") or ""),
        published_at=str(metadata.get("published_at") or ""),
        retired_by=str(metadata.get("retired_by") or ""),
        retired_at=str(metadata.get("retired_at") or ""),
        lifecycle_comment=str(metadata.get("lifecycle_comment") or ""),
    )


def contract_json_bytes(contract: ContractVersion) -> bytes:
    return json.dumps(contract.definition, indent=2, ensure_ascii=True).encode("utf-8")


def init_contract_registry(engine: Any) -> None:
    from sqlalchemy import text

    id_column = "INTEGER PRIMARY KEY AUTOINCREMENT" if engine.dialect.name == "sqlite" else "SERIAL PRIMARY KEY"
    with engine.begin() as conn:
        conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS schema_contracts (
                    id {id_column},
                    contract_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL
                )
                """))
        conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS schema_contract_versions (
                    id {id_column},
                    schema_contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    definition_checksum TEXT NOT NULL,
                    created_by TEXT,
                    created_at TIMESTAMP NOT NULL,
                    published_by TEXT,
                    published_at TIMESTAMP,
                    retired_by TEXT,
                    retired_at TIMESTAMP,
                    lifecycle_comment TEXT,
                    UNIQUE(schema_contract_id, version)
                )
                """))


def _contract_from_row(row: dict[str, Any]) -> ContractVersion:
    definition = json.loads(str(row["definition_json"]))
    contract = contract_from_definition(
        definition,
        status=str(row["status"]),
        database_id=int(row["version_id"]),
        metadata=row,
    )
    if contract.checksum != str(row["definition_checksum"]):
        raise ContractRegistryError(f"Stored checksum mismatch for {contract.contract_key} {contract.version}.")
    return contract


def save_contract_version(
    engine: Any,
    definition: dict[str, Any],
    *,
    actor: str,
    comment: str = "",
    status: str = "draft",
) -> ContractVersion:
    from sqlalchemy import text

    contract = contract_from_definition(definition, status=status)
    init_contract_registry(engine)
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as conn:
        contract_row = (
            conn.execute(
                text("SELECT id FROM schema_contracts WHERE contract_key = :contract_key"),
                {"contract_key": contract.contract_key},
            )
            .mappings()
            .first()
        )
        if contract_row is None:
            result = conn.execute(
                text("""
                    INSERT INTO schema_contracts (contract_key, name, domain, created_at)
                    VALUES (:contract_key, :name, :domain, :created_at)
                    RETURNING id
                    """),
                {
                    "contract_key": contract.contract_key,
                    "name": contract.name,
                    "domain": contract.domain,
                    "created_at": now,
                },
            )
            contract_id = int(result.scalar_one())
        else:
            contract_id = int(contract_row["id"])

        duplicate = conn.execute(
            text("""
                SELECT id FROM schema_contract_versions
                WHERE schema_contract_id = :contract_id AND version = :version
                """),
            {"contract_id": contract_id, "version": contract.version},
        ).first()
        if duplicate is not None:
            raise ContractRegistryError(f"Contract {contract.contract_key} version {contract.version} already exists.")

        published_at = now if status == "published" else None
        result = conn.execute(
            text("""
                INSERT INTO schema_contract_versions (
                    schema_contract_id, version, status, definition_json, definition_checksum,
                    created_by, created_at, published_by, published_at, lifecycle_comment
                )
                VALUES (
                    :schema_contract_id, :version, :status, :definition_json, :definition_checksum,
                    :created_by, :created_at, :published_by, :published_at, :lifecycle_comment
                )
                RETURNING id
                """),
            {
                "schema_contract_id": contract_id,
                "version": contract.version,
                "status": status,
                "definition_json": _canonical_json(contract.definition),
                "definition_checksum": contract.checksum,
                "created_by": actor,
                "created_at": now,
                "published_by": actor if status == "published" else None,
                "published_at": published_at,
                "lifecycle_comment": comment,
            },
        )
        version_id = int(result.scalar_one())

    return load_contract_version(engine, contract.contract_key, contract.version, version_id=version_id)


def ensure_default_contract(engine: Any) -> ContractVersion:
    init_contract_registry(engine)
    existing = load_contract_version(
        engine,
        DEFAULT_CONTRACT_KEY,
        DEFAULT_CONTRACT_VERSION,
        missing_ok=True,
    )
    if existing is not None:
        return existing
    return save_contract_version(
        engine,
        default_contract_definition(),
        actor="system",
        comment="Seeded built-in healthcare eligibility contract.",
        status="published",
    )


def list_contract_versions(engine: Any, statuses: set[str] | None = None) -> list[ContractVersion]:
    from sqlalchemy import text

    init_contract_registry(engine)
    query = """
        SELECT
            sc.contract_key, sc.name, sc.domain,
            scv.id AS version_id, scv.version, scv.status, scv.definition_json,
            scv.definition_checksum, scv.created_by, scv.created_at,
            scv.published_by, scv.published_at, scv.retired_by, scv.retired_at,
            scv.lifecycle_comment
        FROM schema_contract_versions scv
        JOIN schema_contracts sc ON sc.id = scv.schema_contract_id
        ORDER BY sc.name, scv.id DESC
    """
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text(query)).mappings().all()]
    contracts = [_contract_from_row(row) for row in rows]
    if statuses is not None:
        contracts = [contract for contract in contracts if contract.status in statuses]
    return contracts


def load_contract_version(
    engine: Any,
    contract_key: str,
    version: str,
    *,
    version_id: int | None = None,
    missing_ok: bool = False,
) -> ContractVersion | None:
    from sqlalchemy import text

    init_contract_registry(engine)
    version_id_filter = " AND scv.id = :version_id" if version_id is not None else ""
    parameters: dict[str, Any] = {"contract_key": contract_key, "version": version}
    if version_id is not None:
        parameters["version_id"] = version_id
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(f"""
                SELECT
                    sc.contract_key, sc.name, sc.domain,
                    scv.id AS version_id, scv.version, scv.status, scv.definition_json,
                    scv.definition_checksum, scv.created_by, scv.created_at,
                    scv.published_by, scv.published_at, scv.retired_by, scv.retired_at,
                    scv.lifecycle_comment
                FROM schema_contract_versions scv
                JOIN schema_contracts sc ON sc.id = scv.schema_contract_id
                WHERE sc.contract_key = :contract_key AND scv.version = :version
                {version_id_filter}
                """),
                parameters,
            )
            .mappings()
            .first()
        )
    if row is None:
        if missing_ok:
            return None
        raise ContractRegistryError(f"Contract {contract_key} version {version} was not found.")
    return _contract_from_row(dict(row))


def transition_contract_status(
    engine: Any,
    contract: ContractVersion,
    *,
    new_status: str,
    actor: str,
    comment: str,
) -> ContractVersion:
    from sqlalchemy import text

    allowed_transitions = {("draft", "published"), ("published", "retired")}
    if (contract.status, new_status) not in allowed_transitions:
        raise ContractRegistryError(f"Cannot transition contract from {contract.status} to {new_status}.")
    if contract.database_id is None:
        raise ContractRegistryError("Contract must be persisted before its status can change.")

    now = datetime.now(UTC).replace(tzinfo=None)
    values: dict[str, Any] = {
        "version_id": contract.database_id,
        "status": new_status,
        "actor": actor,
        "transitioned_at": now,
        "comment": comment,
    }
    if new_status == "published":
        lifecycle_sql = "published_by = :actor, published_at = :transitioned_at"
    else:
        lifecycle_sql = "retired_by = :actor, retired_at = :transitioned_at"

    with engine.begin() as conn:
        result = conn.execute(
            text(f"""
                UPDATE schema_contract_versions
                SET status = :status, {lifecycle_sql}, lifecycle_comment = :comment
                WHERE id = :version_id AND status = :current_status
                """),
            {**values, "current_status": contract.status},
        )
        if result.rowcount != 1:
            raise ContractRegistryError("Contract status changed concurrently. Refresh and try again.")
    return load_contract_version(engine, contract.contract_key, contract.version)
