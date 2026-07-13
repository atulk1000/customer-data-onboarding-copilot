from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from onboarding.contracts import ContractVersion, ensure_default_contract, init_contract_registry
from onboarding.reconciliation import (
    ReconciliationError,
    ReconciliationResult,
    forecast_with_connection,
    persist_reconciliation,
    verify_post_publish_with_connection,
)
from onboarding.transform import TransformOutputs, dataframe_to_records
from onboarding.validation import ValidationResult

DEFAULT_DATABASE_URL = "postgresql+psycopg://onboarding:onboarding@localhost:55432/onboarding"


class DatabaseConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishOutcome:
    import_run_id: int
    pre_reconciliation: ReconciliationResult
    post_reconciliation: ReconciliationResult


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(override=True)


def get_database_url() -> str:
    _load_dotenv_if_available()
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_engine(database_url: str | None = None):
    try:
        from sqlalchemy import create_engine
    except Exception as exc:
        raise DatabaseConfigurationError(
            "SQLAlchemy is not installed. Install project requirements before publishing to PostgreSQL."
        ) from exc
    resolved_url = database_url or get_database_url()
    engine_options: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if resolved_url.startswith("postgresql"):
        engine_options["connect_args"] = {"connect_timeout": 3}
    return create_engine(resolved_url, **engine_options)


def init_db(engine: Any) -> None:
    from sqlalchemy import text

    init_contract_registry(engine)
    statements = [
        """
        CREATE TABLE IF NOT EXISTS import_runs (
            id SERIAL PRIMARY KEY,
            file_name TEXT NOT NULL,
            mapping_mode TEXT NOT NULL,
            started_at TIMESTAMP NOT NULL,
            completed_at TIMESTAMP,
            source_file_hash TEXT,
            is_replay BOOLEAN NOT NULL DEFAULT FALSE,
            previous_import_run_id INTEGER,
            replay_acknowledged BOOLEAN NOT NULL DEFAULT FALSE,
            source_row_count INTEGER NOT NULL,
            accepted_row_count INTEGER NOT NULL,
            rejected_row_count INTEGER NOT NULL,
            warning_count INTEGER NOT NULL,
            target_schema_name TEXT,
            target_schema_version TEXT,
            mapping_template_name TEXT,
            source_coverage_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
            signoff_reviewer_name TEXT,
            signoff_reviewer_role TEXT,
            signoff_decision TEXT,
            signoff_comment TEXT,
            signoff_at TIMESTAMP,
            status TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS members (
            member_id TEXT PRIMARY KEY,
            first_name TEXT NOT NULL,
            last_name TEXT NOT NULL,
            date_of_birth DATE NOT NULL,
            gender TEXT,
            email TEXT,
            phone TEXT,
            latest_import_run_id INTEGER REFERENCES import_runs(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS plans (
            plan_id TEXT PRIMARY KEY,
            plan_name TEXT NOT NULL,
            plan_type TEXT,
            carrier_name TEXT,
            latest_import_run_id INTEGER REFERENCES import_runs(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS member_coverage (
            coverage_id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            coverage_start_date DATE NOT NULL,
            coverage_end_date DATE,
            coverage_status TEXT NOT NULL,
            relationship_to_subscriber TEXT NOT NULL,
            subscriber_id TEXT,
            latest_import_run_id INTEGER REFERENCES import_runs(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS mapping_decisions (
            id SERIAL PRIMARY KEY,
            import_run_id INTEGER REFERENCES import_runs(id),
            target_table TEXT NOT NULL,
            target_field TEXT NOT NULL,
            target_data_type TEXT,
            target_validation_kind TEXT,
            source_inferred_type TEXT,
            type_alignment TEXT,
            type_alignment_reason TEXT,
            source_column TEXT,
            confidence INTEGER,
            mapping_mode TEXT NOT NULL,
            approved BOOLEAN NOT NULL,
            needs_review BOOLEAN NOT NULL,
            reason TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS source_column_audit (
            id SERIAL PRIMARY KEY,
            import_run_id INTEGER REFERENCES import_runs(id),
            source_column TEXT NOT NULL,
            normalized_name TEXT,
            inferred_type TEXT,
            null_rate FLOAT,
            unique_rate FLOAT,
            coverage_status TEXT NOT NULL,
            mapped_targets TEXT,
            approved_targets TEXT,
            review_recommendation TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS validation_issues (
            id SERIAL PRIMARY KEY,
            import_run_id INTEGER REFERENCES import_runs(id),
            source_row_number INTEGER NOT NULL,
            severity TEXT NOT NULL,
            issue_code TEXT NOT NULL,
            issue_message TEXT NOT NULL,
            target_field TEXT,
            source_column TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rejected_rows (
            id SERIAL PRIMARY KEY,
            import_run_id INTEGER REFERENCES import_runs(id),
            source_row_number INTEGER NOT NULL,
            raw_payload_json JSONB NOT NULL,
            error_summary TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS mapping_template_versions (
            id SERIAL PRIMARY KEY,
            template_name TEXT NOT NULL,
            template_version INTEGER NOT NULL,
            contract_version_id INTEGER REFERENCES schema_contract_versions(id),
            contract_checksum TEXT,
            mapping_mode TEXT,
            mappings_json TEXT NOT NULL,
            transformation_rules_json TEXT NOT NULL,
            created_by TEXT,
            created_at TIMESTAMP NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(template_name, template_version)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reconciliation_runs (
            id SERIAL PRIMARY KEY,
            import_run_id INTEGER REFERENCES import_runs(id),
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            database_available BOOLEAN NOT NULL,
            source_metrics_json TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            blocking_failure_count INTEGER NOT NULL,
            warning_count INTEGER NOT NULL,
            transaction_status TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reconciliation_table_metrics (
            id SERIAL PRIMARY KEY,
            reconciliation_run_id INTEGER REFERENCES reconciliation_runs(id),
            target_table TEXT NOT NULL,
            candidate_count INTEGER,
            unique_business_key_count INTEGER,
            exact_duplicate_count INTEGER,
            conflicting_duplicate_count INTEGER,
            expected_insert_count INTEGER,
            expected_update_count INTEGER,
            expected_unchanged_count INTEGER,
            actual_insert_count INTEGER,
            actual_update_count INTEGER,
            actual_unchanged_count INTEGER,
            orphan_count INTEGER,
            missing_output_count INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reconciliation_checks (
            id SERIAL PRIMARY KEY,
            reconciliation_run_id INTEGER REFERENCES reconciliation_runs(id),
            check_code TEXT NOT NULL,
            severity TEXT NOT NULL,
            status TEXT NOT NULL,
            expected_value_json TEXT,
            actual_value_json TEXT,
            message TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS row_corrections (
            id SERIAL PRIMARY KEY,
            parent_import_run_id INTEGER REFERENCES import_runs(id),
            child_import_run_id INTEGER REFERENCES import_runs(id),
            source_record_id TEXT NOT NULL,
            source_row_number INTEGER NOT NULL,
            original_row_fingerprint TEXT NOT NULL,
            source_column TEXT NOT NULL,
            original_value_json TEXT,
            corrected_value_json TEXT,
            correction_reason TEXT NOT NULL,
            correction_status TEXT NOT NULL,
            corrected_by TEXT,
            corrected_at TIMESTAMP,
            revalidated_at TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS field_lineage (
            id SERIAL PRIMARY KEY,
            import_run_id INTEGER REFERENCES import_runs(id),
            source_record_id TEXT,
            source_row_number INTEGER NOT NULL,
            row_status TEXT,
            lineage_status TEXT,
            target_table TEXT NOT NULL,
            target_field TEXT NOT NULL,
            source_columns TEXT,
            source_values_json TEXT,
            corrected_values_json TEXT,
            final_value TEXT,
            transformation_trace_json TEXT,
            issue_codes TEXT
        )
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
        conn.execute(text("ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS target_data_type TEXT"))
        conn.execute(text("ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS target_validation_kind TEXT"))
        conn.execute(text("ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS source_inferred_type TEXT"))
        conn.execute(text("ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS type_alignment TEXT"))
        conn.execute(text("ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS type_alignment_reason TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS target_schema_name TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS source_file_hash TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS is_replay BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS previous_import_run_id INTEGER"))
        conn.execute(
            text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS replay_acknowledged BOOLEAN NOT NULL DEFAULT FALSE")
        )
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS target_schema_version TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS mapping_template_name TEXT"))
        conn.execute(
            text(
                "ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS source_coverage_reviewed BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_reviewer_name TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_reviewer_role TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_decision TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_comment TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS parent_import_run_id INTEGER"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS run_kind TEXT NOT NULL DEFAULT 'original'"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS correction_attempt_number INTEGER"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS contract_version_id INTEGER"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS mapping_template_version_id INTEGER"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS reconciliation_status TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS publish_transaction_status TEXT"))
        conn.execute(text("ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS source_columns TEXT"))
        conn.execute(text("ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS transformation_rules_json TEXT"))
        conn.execute(text("ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS failure_policy TEXT"))
        conn.execute(
            text(
                "ALTER TABLE mapping_decisions ADD COLUMN IF NOT EXISTS transformation_approved BOOLEAN NOT NULL DEFAULT FALSE"
            )
        )
        conn.execute(text("ALTER TABLE rejected_rows ADD COLUMN IF NOT EXISTS source_record_id TEXT"))
        conn.execute(text("ALTER TABLE rejected_rows ADD COLUMN IF NOT EXISTS original_row_fingerprint TEXT"))
        conn.execute(text("ALTER TABLE rejected_rows ADD COLUMN IF NOT EXISTS correction_status TEXT"))
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS idx_import_runs_source_file_hash ON import_runs (source_file_hash)")
        )


def _clean_record(record: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in record.items():
        if pd.isna(value) if not isinstance(value, (dict, list)) else False:
            clean[key] = None
        else:
            clean[key] = value
    return clean


def _insert_import_run(
    conn: Any,
    *,
    file_name: str,
    mapping_mode: str,
    validation_result: ValidationResult,
    target_schema_name: str = "",
    target_schema_version: str = "",
    mapping_template_name: str = "",
    source_file_hash: str = "",
    is_replay: bool = False,
    previous_import_run_id: int | None = None,
    replay_acknowledged: bool = False,
    source_coverage_reviewed: bool = False,
    signoff: dict[str, Any] | None = None,
    status: str = "publishing",
    contract_version_id: int | None = None,
    mapping_template_version_id: int | None = None,
    parent_import_run_id: int | None = None,
    run_kind: str = "original",
    correction_attempt_number: int | None = None,
    reconciliation_status: str = "",
    publish_transaction_status: str = "not_started",
) -> int:
    from sqlalchemy import text

    signoff = signoff or {}
    result = conn.execute(
        text("""
            INSERT INTO import_runs (
                file_name, mapping_mode, started_at, completed_at, source_file_hash,
                is_replay, previous_import_run_id, replay_acknowledged, source_row_count,
                accepted_row_count, rejected_row_count, warning_count,
                target_schema_name, target_schema_version, mapping_template_name,
                source_coverage_reviewed, signoff_reviewer_name, signoff_reviewer_role,
                signoff_decision, signoff_comment, signoff_at, status,
                contract_version_id, mapping_template_version_id, parent_import_run_id,
                run_kind, correction_attempt_number, reconciliation_status,
                publish_transaction_status
            )
            VALUES (
                :file_name, :mapping_mode, :started_at, :completed_at, :source_file_hash,
                :is_replay, :previous_import_run_id, :replay_acknowledged, :source_row_count,
                :accepted_row_count, :rejected_row_count, :warning_count,
                :target_schema_name, :target_schema_version, :mapping_template_name,
                :source_coverage_reviewed, :signoff_reviewer_name, :signoff_reviewer_role,
                :signoff_decision, :signoff_comment, :signoff_at, :status,
                :contract_version_id, :mapping_template_version_id, :parent_import_run_id,
                :run_kind, :correction_attempt_number, :reconciliation_status,
                :publish_transaction_status
            )
            RETURNING id
            """),
        {
            "file_name": file_name,
            "mapping_mode": mapping_mode,
            "started_at": datetime.now(),
            "completed_at": datetime.now(),
            "source_file_hash": source_file_hash,
            "is_replay": is_replay,
            "previous_import_run_id": previous_import_run_id,
            "replay_acknowledged": replay_acknowledged,
            "source_row_count": len(validation_result.normalized_df),
            "accepted_row_count": validation_result.accepted_row_count,
            "rejected_row_count": validation_result.rejected_row_count,
            "warning_count": validation_result.warning_count,
            "target_schema_name": target_schema_name,
            "target_schema_version": target_schema_version,
            "mapping_template_name": mapping_template_name,
            "source_coverage_reviewed": source_coverage_reviewed,
            "signoff_reviewer_name": signoff.get("reviewer_name"),
            "signoff_reviewer_role": signoff.get("reviewer_role"),
            "signoff_decision": signoff.get("decision"),
            "signoff_comment": signoff.get("comment"),
            "signoff_at": signoff.get("signed_off_at") or None,
            "status": status,
            "contract_version_id": contract_version_id,
            "mapping_template_version_id": mapping_template_version_id,
            "parent_import_run_id": parent_import_run_id,
            "run_kind": run_kind,
            "correction_attempt_number": correction_attempt_number,
            "reconciliation_status": reconciliation_status,
            "publish_transaction_status": publish_transaction_status,
        },
    )
    return int(result.scalar_one())


def _quote(connection: Any, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _target_sql_type(data_type: str) -> str:
    return {
        "date": "DATE",
        "numeric": "NUMERIC",
        "boolean": "BOOLEAN",
    }.get(data_type, "TEXT")


def ensure_contract_target_tables(engine: Any, contract: ContractVersion) -> None:
    from sqlalchemy import text

    fields_by_table: dict[str, list[Any]] = {}
    for target in contract.target_fields:
        fields_by_table.setdefault(target.table, []).append(target)

    with engine.begin() as conn:
        for table_definition in contract.definition.get("tables") or []:
            table_name = str(table_definition["name"])
            field_definitions = []
            for target in fields_by_table.get(table_name, []):
                nullable_sql = "" if target.nullable else " NOT NULL"
                field_definitions.append(
                    f"{_quote(conn, target.field)} {_target_sql_type(target.data_type)}{nullable_sql}"
                )
            primary_key = [str(value) for value in table_definition.get("primary_key") or []]
            if primary_key:
                quoted_key = ", ".join(_quote(conn, value) for value in primary_key)
                field_definitions.append(f"PRIMARY KEY ({quoted_key})")
            for foreign_key in table_definition.get("foreign_keys") or []:
                field_definitions.append(
                    "FOREIGN KEY "
                    f"({_quote(conn, str(foreign_key['field']))}) REFERENCES "
                    f"{_quote(conn, str(foreign_key['references_table']))}"
                    f"({_quote(conn, str(foreign_key['references_field']))})"
                )
            field_definitions.append("latest_import_run_id INTEGER REFERENCES import_runs(id)")
            conn.execute(
                text(f"CREATE TABLE IF NOT EXISTS {_quote(conn, table_name)} " f"({', '.join(field_definitions)})")
            )


def _persist_mapping_template_version(
    conn: Any,
    *,
    contract: ContractVersion,
    template_name: str,
    template_version: int,
    mapping_mode: str,
    mappings: list[dict[str, Any]],
    created_by: str,
) -> int:
    from sqlalchemy import text

    existing = conn.execute(
        text("""
            SELECT id FROM mapping_template_versions
            WHERE template_name = :template_name AND template_version = :template_version
            """),
        {"template_name": template_name, "template_version": template_version},
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)

    transformation_rules = [
        {
            "target_table": mapping.get("target_table"),
            "target_field": mapping.get("target_field"),
            "source_columns": mapping.get("source_columns") or [],
            "transformation_steps": mapping.get("transformation_steps") or [],
            "failure_policy": mapping.get("failure_policy") or "error",
            "transformation_approved": bool(mapping.get("transformation_approved", False)),
        }
        for mapping in mappings
    ]
    inserted = conn.execute(
        text("""
            INSERT INTO mapping_template_versions (
                template_name, template_version, contract_version_id, contract_checksum,
                mapping_mode, mappings_json, transformation_rules_json,
                created_by, created_at, status
            )
            VALUES (
                :template_name, :template_version, :contract_version_id, :contract_checksum,
                :mapping_mode, :mappings_json, :transformation_rules_json,
                :created_by, :created_at, 'approved'
            )
            RETURNING id
            """),
        {
            "template_name": template_name,
            "template_version": template_version,
            "contract_version_id": contract.database_id,
            "contract_checksum": contract.checksum,
            "mapping_mode": mapping_mode,
            "mappings_json": json.dumps(mappings, default=str, ensure_ascii=True),
            "transformation_rules_json": json.dumps(transformation_rules, default=str, ensure_ascii=True),
            "created_by": created_by,
            "created_at": datetime.now(),
        },
    )
    return int(inserted.scalar_one())


def _persist_corrections(
    conn: Any,
    *,
    parent_import_run_id: int | None,
    child_import_run_id: int,
    corrections: list[dict[str, Any]],
) -> None:
    from sqlalchemy import text

    for correction in corrections:
        conn.execute(
            text("""
                INSERT INTO row_corrections (
                    parent_import_run_id, child_import_run_id, source_record_id,
                    source_row_number, original_row_fingerprint, source_column,
                    original_value_json, corrected_value_json, correction_reason,
                    correction_status, corrected_by, corrected_at, revalidated_at
                )
                VALUES (
                    :parent_import_run_id, :child_import_run_id, :source_record_id,
                    :source_row_number, :original_row_fingerprint, :source_column,
                    :original_value_json, :corrected_value_json, :correction_reason,
                    :correction_status, :corrected_by, :corrected_at, :revalidated_at
                )
                """),
            {
                "parent_import_run_id": parent_import_run_id,
                "child_import_run_id": child_import_run_id,
                "source_record_id": correction.get("source_record_id"),
                "source_row_number": correction.get("source_row_number"),
                "original_row_fingerprint": correction.get("original_row_fingerprint"),
                "source_column": correction.get("source_column"),
                "original_value_json": json.dumps(correction.get("original_value"), default=str),
                "corrected_value_json": json.dumps(correction.get("corrected_value"), default=str),
                "correction_reason": correction.get("correction_reason") or "",
                "correction_status": correction.get("correction_status") or "recovered",
                "corrected_by": correction.get("corrected_by") or "",
                "corrected_at": correction.get("corrected_at") or datetime.now(),
                "revalidated_at": datetime.now(),
            },
        )


def _persist_field_lineage(conn: Any, import_run_id: int, lineage: pd.DataFrame) -> None:
    from sqlalchemy import text

    for row in dataframe_to_records(lineage):
        conn.execute(
            text("""
                INSERT INTO field_lineage (
                    import_run_id, source_record_id, source_row_number, row_status,
                    lineage_status, target_table, target_field, source_columns,
                    source_values_json, corrected_values_json, final_value,
                    transformation_trace_json, issue_codes
                )
                VALUES (
                    :import_run_id, :source_record_id, :source_row_number, :row_status,
                    :lineage_status, :target_table, :target_field, :source_columns,
                    :source_values_json, :corrected_values_json, :final_value,
                    :transformation_trace_json, :issue_codes
                )
                """),
            {
                "import_run_id": import_run_id,
                "source_record_id": row.get("source_record_id"),
                "source_row_number": row.get("source_row_number"),
                "row_status": row.get("row_status"),
                "lineage_status": row.get("lineage_status"),
                "target_table": row.get("target_table"),
                "target_field": row.get("target_field"),
                "source_columns": row.get("source_columns") or row.get("source_column"),
                "source_values_json": row.get("source_values_json") or "{}",
                "corrected_values_json": row.get("corrected_values_json") or "{}",
                "final_value": None if row.get("final_value") is None else str(row.get("final_value")),
                "transformation_trace_json": row.get("transformation_trace_json") or "[]",
                "issue_codes": row.get("issue_codes") or "",
            },
        )


def _upsert_contract_tables(
    conn: Any,
    outputs: TransformOutputs,
    contract: ContractVersion,
    import_run_id: int,
) -> None:
    from sqlalchemy import text

    output_tables = outputs.tables or {
        "members": outputs.members,
        "plans": outputs.plans,
        "member_coverage": outputs.member_coverage,
    }
    for table_name in contract.table_names:
        output_df = output_tables.get(table_name, pd.DataFrame())
        if output_df.empty:
            continue
        target_fields = [field.field for field in contract.target_fields if field.table == table_name]
        business_key = contract.business_keys.get(table_name) or contract.primary_keys.get(table_name)
        quoted_table = _quote(conn, table_name)
        quoted_columns = [_quote(conn, field) for field in target_fields]
        insert_columns = quoted_columns + [_quote(conn, "latest_import_run_id")]
        value_parameters = [f":{field}" for field in target_fields] + [":import_run_id"]
        conflict_columns = ", ".join(_quote(conn, field) for field in business_key)
        update_fields = [field for field in target_fields if field not in business_key]
        update_assignments = [f"{_quote(conn, field)} = EXCLUDED.{_quote(conn, field)}" for field in update_fields]
        update_assignments.append(
            f"{_quote(conn, 'latest_import_run_id')} = EXCLUDED.{_quote(conn, 'latest_import_run_id')}"
        )
        statement = text(
            f"INSERT INTO {quoted_table} ({', '.join(insert_columns)}) "
            f"VALUES ({', '.join(value_parameters)}) "
            f"ON CONFLICT ({conflict_columns}) DO UPDATE SET {', '.join(update_assignments)}"
        )
        for row in dataframe_to_records(output_df):
            conn.execute(statement, {**_clean_record(row), "import_run_id": import_run_id})


def _persist_run_details(
    conn: Any,
    *,
    import_run_id: int,
    mapping_mode: str,
    mappings: list[dict[str, Any]],
    validation_result: ValidationResult,
    outputs: TransformOutputs,
    source_coverage: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    parent_import_run_id: int | None,
) -> None:
    from sqlalchemy import text

    for mapping in mappings:
        source_columns = mapping.get("source_columns") or []
        if not source_columns and mapping.get("source_column"):
            source_columns = [mapping.get("source_column")]
        conn.execute(
            text("""
                INSERT INTO mapping_decisions (
                    import_run_id, target_table, target_field, target_data_type,
                    target_validation_kind, source_inferred_type, type_alignment,
                    type_alignment_reason, source_column, source_columns, confidence,
                    mapping_mode, approved, needs_review, reason,
                    transformation_rules_json, failure_policy, transformation_approved
                )
                VALUES (
                    :import_run_id, :target_table, :target_field, :target_data_type,
                    :target_validation_kind, :source_inferred_type, :type_alignment,
                    :type_alignment_reason, :source_column, :source_columns, :confidence,
                    :mapping_mode, :approved, :needs_review, :reason,
                    :transformation_rules_json, :failure_policy, :transformation_approved
                )
                """),
            {
                "import_run_id": import_run_id,
                "target_table": mapping.get("target_table"),
                "target_field": mapping.get("target_field"),
                "target_data_type": mapping.get("target_data_type"),
                "target_validation_kind": mapping.get("target_validation_kind"),
                "source_inferred_type": mapping.get("source_inferred_type"),
                "type_alignment": mapping.get("type_alignment"),
                "type_alignment_reason": mapping.get("type_alignment_reason"),
                "source_column": mapping.get("source_column"),
                "source_columns": "; ".join(str(value) for value in source_columns),
                "confidence": mapping.get("confidence"),
                "mapping_mode": mapping_mode,
                "approved": bool(mapping.get("approved")),
                "needs_review": bool(mapping.get("needs_review")),
                "reason": mapping.get("reason") or mapping.get("rationale") or "",
                "transformation_rules_json": json.dumps(
                    mapping.get("transformation_steps") or [], default=str, ensure_ascii=True
                ),
                "failure_policy": mapping.get("failure_policy") or "error",
                "transformation_approved": bool(mapping.get("transformation_approved", False)),
            },
        )

    for source_column in source_coverage:
        conn.execute(
            text("""
                INSERT INTO source_column_audit (
                    import_run_id, source_column, normalized_name, inferred_type,
                    null_rate, unique_rate, coverage_status, mapped_targets,
                    approved_targets, review_recommendation
                )
                VALUES (
                    :import_run_id, :source_column, :normalized_name, :inferred_type,
                    :null_rate, :unique_rate, :coverage_status, :mapped_targets,
                    :approved_targets, :review_recommendation
                )
                """),
            {**source_column, "import_run_id": import_run_id},
        )

    issues_df = validation_result.issues_df
    for issue in dataframe_to_records(issues_df) if not issues_df.empty else []:
        conn.execute(
            text("""
                INSERT INTO validation_issues (
                    import_run_id, source_row_number, severity, issue_code,
                    issue_message, target_field, source_column
                )
                VALUES (
                    :import_run_id, :source_row_number, :severity, :issue_code,
                    :issue_message, :target_field, :source_column
                )
                """),
            {**issue, "import_run_id": import_run_id},
        )

    correction_queue_by_row = {
        int(row["source_row_number"]): row for row in outputs.correction_work_queue.to_dict("records")
    }
    for row in dataframe_to_records(outputs.rejected_rows) if not outputs.rejected_rows.empty else []:
        source_row_number = int(row.get("source_row_number"))
        correction_identity = correction_queue_by_row.get(source_row_number, {})
        conn.execute(
            text("""
                INSERT INTO rejected_rows (
                    import_run_id, source_row_number, source_record_id,
                    original_row_fingerprint, correction_status,
                    raw_payload_json, error_summary
                )
                VALUES (
                    :import_run_id, :source_row_number, :source_record_id,
                    :original_row_fingerprint, :correction_status,
                    CAST(:raw_payload_json AS jsonb), :error_summary
                )
                """),
            {
                "import_run_id": import_run_id,
                "source_row_number": source_row_number,
                "source_record_id": correction_identity.get("source_record_id"),
                "original_row_fingerprint": correction_identity.get("original_row_fingerprint"),
                "correction_status": "rejected",
                "raw_payload_json": json.dumps(row, default=str),
                "error_summary": str(row.get("errors") or ""),
            },
        )

    _persist_field_lineage(conn, import_run_id, outputs.field_lineage)
    _persist_corrections(
        conn,
        parent_import_run_id=parent_import_run_id,
        child_import_run_id=import_run_id,
        corrections=corrections,
    )


def publish_import(
    *,
    engine: Any,
    file_name: str,
    mapping_mode: str,
    mappings: list[dict[str, Any]],
    validation_result: ValidationResult,
    outputs: TransformOutputs,
    target_schema_name: str = "",
    target_schema_version: str = "",
    mapping_template_name: str = "",
    source_file_hash: str = "",
    import_replay_check: dict[str, Any] | None = None,
    replay_acknowledged: bool = False,
    source_coverage: list[dict[str, Any]] | None = None,
    source_coverage_reviewed: bool = False,
    signoff: dict[str, Any] | None = None,
    contract: ContractVersion | None = None,
    mapping_template_version: int = 1,
    parent_import_run_id: int | None = None,
    run_kind: str = "original",
    correction_attempt_number: int | None = None,
    corrections: list[dict[str, Any]] | None = None,
    acknowledged_reject_count: int = 0,
    return_outcome: bool = False,
) -> int | PublishOutcome:
    from sqlalchemy import text

    init_db(engine)
    selected_contract = contract or ensure_default_contract(engine)
    if selected_contract.database_id is None:
        persisted_contract = ensure_default_contract(engine)
        if (
            selected_contract.contract_key != persisted_contract.contract_key
            or selected_contract.version != persisted_contract.version
            or selected_contract.checksum != persisted_contract.checksum
        ):
            raise DatabaseConfigurationError("Selected target contract must be persisted before publishing.")
        selected_contract = persisted_contract
    ensure_contract_target_tables(engine, selected_contract)

    target_schema_name = target_schema_name or selected_contract.name
    target_schema_version = target_schema_version or selected_contract.version
    mapping_template_name = mapping_template_name or "Ad hoc mapping"
    signoff = signoff or {}
    corrections = corrections or []
    source_coverage = source_coverage or []
    pre_reconciliation: ReconciliationResult | None = None
    post_reconciliation: ReconciliationResult | None = None
    import_run_id: int | None = None

    try:
        with engine.begin() as conn:
            pre_reconciliation = forecast_with_connection(
                conn,
                validation_result,
                outputs,
                contract=selected_contract,
                acknowledged_reject_count=acknowledged_reject_count,
            )
            if pre_reconciliation.status == "FAIL":
                pre_reconciliation.transaction_status = "blocked_before_write"
                raise ReconciliationError("Pre-publish reconciliation failed.", pre_reconciliation)
            if pre_reconciliation.status == "WARNING" and not str(signoff.get("reviewer_name") or "").strip():
                raise DatabaseConfigurationError("Reviewer signoff is required for reconciliation warnings.")

            mapping_template_version_id = _persist_mapping_template_version(
                conn,
                contract=selected_contract,
                template_name=mapping_template_name,
                template_version=mapping_template_version,
                mapping_mode=mapping_mode,
                mappings=mappings,
                created_by=str(signoff.get("reviewer_name") or "system"),
            )
            import_run_id = _insert_import_run(
                conn,
                file_name=file_name,
                mapping_mode=mapping_mode,
                validation_result=validation_result,
                target_schema_name=target_schema_name,
                target_schema_version=target_schema_version,
                mapping_template_name=mapping_template_name,
                source_file_hash=source_file_hash,
                is_replay=bool((import_replay_check or {}).get("is_replay")),
                previous_import_run_id=(import_replay_check or {}).get("previous_import_run_id"),
                replay_acknowledged=replay_acknowledged,
                source_coverage_reviewed=source_coverage_reviewed,
                signoff=signoff,
                status="publishing",
                contract_version_id=selected_contract.database_id,
                mapping_template_version_id=mapping_template_version_id,
                parent_import_run_id=parent_import_run_id,
                run_kind=run_kind,
                correction_attempt_number=correction_attempt_number,
                reconciliation_status=pre_reconciliation.status,
                publish_transaction_status="in_progress",
            )
            pre_reconciliation.transaction_status = "in_progress"
            persist_reconciliation(conn, import_run_id, pre_reconciliation)
            _persist_run_details(
                conn,
                import_run_id=import_run_id,
                mapping_mode=mapping_mode,
                mappings=mappings,
                validation_result=validation_result,
                outputs=outputs,
                source_coverage=source_coverage,
                corrections=corrections,
                parent_import_run_id=parent_import_run_id,
            )
            _upsert_contract_tables(conn, outputs, selected_contract, import_run_id)
            post_reconciliation = verify_post_publish_with_connection(
                conn,
                outputs,
                pre_reconciliation,
                contract=selected_contract,
            )
            if post_reconciliation.status == "FAIL":
                post_reconciliation.transaction_status = "rolled_back"
                raise ReconciliationError(
                    "Post-publish reconciliation failed; canonical writes were rolled back.", post_reconciliation
                )
            post_reconciliation.transaction_status = "committed"
            persist_reconciliation(conn, import_run_id, post_reconciliation)
            conn.execute(
                text("""
                    UPDATE import_runs
                    SET completed_at = :completed_at, status = 'published',
                        reconciliation_status = :reconciliation_status,
                        publish_transaction_status = 'committed'
                    WHERE id = :import_run_id
                    """),
                {
                    "completed_at": datetime.now(),
                    "reconciliation_status": post_reconciliation.status,
                    "import_run_id": import_run_id,
                },
            )
    except ReconciliationError as exc:
        failed_result = exc.result or pre_reconciliation
        if failed_result is not None:
            with engine.begin() as failed_conn:
                failed_template_id = _persist_mapping_template_version(
                    failed_conn,
                    contract=selected_contract,
                    template_name=mapping_template_name,
                    template_version=mapping_template_version,
                    mapping_mode=mapping_mode,
                    mappings=mappings,
                    created_by=str(signoff.get("reviewer_name") or "system"),
                )
                failed_run_id = _insert_import_run(
                    failed_conn,
                    file_name=file_name,
                    mapping_mode=mapping_mode,
                    validation_result=validation_result,
                    target_schema_name=target_schema_name,
                    target_schema_version=target_schema_version,
                    mapping_template_name=mapping_template_name,
                    source_file_hash=source_file_hash,
                    is_replay=bool((import_replay_check or {}).get("is_replay")),
                    previous_import_run_id=(import_replay_check or {}).get("previous_import_run_id"),
                    replay_acknowledged=replay_acknowledged,
                    source_coverage_reviewed=source_coverage_reviewed,
                    signoff=signoff,
                    status="reconciliation_failed",
                    contract_version_id=selected_contract.database_id,
                    mapping_template_version_id=failed_template_id,
                    parent_import_run_id=parent_import_run_id,
                    run_kind=run_kind,
                    correction_attempt_number=correction_attempt_number,
                    reconciliation_status="FAIL",
                    publish_transaction_status=failed_result.transaction_status,
                )
                persist_reconciliation(failed_conn, failed_run_id, failed_result)
            exc.failed_import_run_id = failed_run_id
        raise

    if import_run_id is None or pre_reconciliation is None or post_reconciliation is None:
        raise DatabaseConfigurationError("Publish transaction completed without reconciliation evidence.")
    outcome = PublishOutcome(import_run_id, pre_reconciliation, post_reconciliation)
    return outcome if return_outcome else import_run_id


def _upsert_members(conn: Any, members: pd.DataFrame, import_run_id: int) -> None:
    from sqlalchemy import text

    for row in dataframe_to_records(members):
        conn.execute(
            text("""
                INSERT INTO members (
                    member_id, first_name, last_name, date_of_birth, gender, email, phone, latest_import_run_id
                )
                VALUES (
                    :member_id, :first_name, :last_name, :date_of_birth, :gender, :email, :phone, :import_run_id
                )
                ON CONFLICT (member_id) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    date_of_birth = EXCLUDED.date_of_birth,
                    gender = EXCLUDED.gender,
                    email = EXCLUDED.email,
                    phone = EXCLUDED.phone,
                    latest_import_run_id = EXCLUDED.latest_import_run_id
                """),
            {**_clean_record(row), "import_run_id": import_run_id},
        )


def _upsert_plans(conn: Any, plans: pd.DataFrame, import_run_id: int) -> None:
    from sqlalchemy import text

    for row in dataframe_to_records(plans):
        conn.execute(
            text("""
                INSERT INTO plans (
                    plan_id, plan_name, plan_type, carrier_name, latest_import_run_id
                )
                VALUES (
                    :plan_id, :plan_name, :plan_type, :carrier_name, :import_run_id
                )
                ON CONFLICT (plan_id) DO UPDATE SET
                    plan_name = EXCLUDED.plan_name,
                    plan_type = EXCLUDED.plan_type,
                    carrier_name = EXCLUDED.carrier_name,
                    latest_import_run_id = EXCLUDED.latest_import_run_id
                """),
            {**_clean_record(row), "import_run_id": import_run_id},
        )


def _upsert_member_coverage(conn: Any, coverage: pd.DataFrame, import_run_id: int) -> None:
    from sqlalchemy import text

    for row in dataframe_to_records(coverage):
        conn.execute(
            text("""
                INSERT INTO member_coverage (
                    coverage_id, member_id, plan_id, coverage_start_date, coverage_end_date,
                    coverage_status, relationship_to_subscriber, subscriber_id, latest_import_run_id
                )
                VALUES (
                    :coverage_id, :member_id, :plan_id, :coverage_start_date, :coverage_end_date,
                    :coverage_status, :relationship_to_subscriber, :subscriber_id, :import_run_id
                )
                ON CONFLICT (coverage_id) DO UPDATE SET
                    member_id = EXCLUDED.member_id,
                    plan_id = EXCLUDED.plan_id,
                    coverage_start_date = EXCLUDED.coverage_start_date,
                    coverage_end_date = EXCLUDED.coverage_end_date,
                    coverage_status = EXCLUDED.coverage_status,
                    relationship_to_subscriber = EXCLUDED.relationship_to_subscriber,
                    subscriber_id = EXCLUDED.subscriber_id,
                    latest_import_run_id = EXCLUDED.latest_import_run_id
                """),
            {**_clean_record(row), "import_run_id": import_run_id},
        )


def connection_status() -> tuple[bool, str]:
    try:
        from sqlalchemy import text

        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "Connected to PostgreSQL."
    except Exception as exc:
        return False, str(exc)
