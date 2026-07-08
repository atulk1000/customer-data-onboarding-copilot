from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import pandas as pd

from onboarding.transform import TransformOutputs, dataframe_to_records
from onboarding.validation import ValidationResult

DEFAULT_DATABASE_URL = "postgresql+psycopg://onboarding:onboarding@localhost:55432/onboarding"


class DatabaseConfigurationError(RuntimeError):
    pass


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
    return create_engine(database_url or get_database_url(), future=True)


def init_db(engine: Any) -> None:
    from sqlalchemy import text

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
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS replay_acknowledged BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS target_schema_version TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS mapping_template_name TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS source_coverage_reviewed BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_reviewer_name TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_reviewer_role TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_decision TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_comment TEXT"))
        conn.execute(text("ALTER TABLE import_runs ADD COLUMN IF NOT EXISTS signoff_at TIMESTAMP"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_import_runs_source_file_hash ON import_runs (source_file_hash)"))


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
) -> int:
    from sqlalchemy import text

    signoff = signoff or {}
    result = conn.execute(
        text(
            """
            INSERT INTO import_runs (
                file_name, mapping_mode, started_at, completed_at, source_file_hash,
                is_replay, previous_import_run_id, replay_acknowledged, source_row_count,
                accepted_row_count, rejected_row_count, warning_count,
                target_schema_name, target_schema_version, mapping_template_name,
                source_coverage_reviewed, signoff_reviewer_name, signoff_reviewer_role,
                signoff_decision, signoff_comment, signoff_at, status
            )
            VALUES (
                :file_name, :mapping_mode, :started_at, :completed_at, :source_file_hash,
                :is_replay, :previous_import_run_id, :replay_acknowledged, :source_row_count,
                :accepted_row_count, :rejected_row_count, :warning_count,
                :target_schema_name, :target_schema_version, :mapping_template_name,
                :source_coverage_reviewed, :signoff_reviewer_name, :signoff_reviewer_role,
                :signoff_decision, :signoff_comment, :signoff_at, :status
            )
            RETURNING id
            """
        ),
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
            "status": "published",
        },
    )
    return int(result.scalar_one())


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
) -> int:
    from sqlalchemy import text

    init_db(engine)
    with engine.begin() as conn:
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
        )

        for mapping in mappings:
            conn.execute(
                text(
                    """
                    INSERT INTO mapping_decisions (
                        import_run_id, target_table, target_field, target_data_type,
                        target_validation_kind, source_inferred_type, type_alignment,
                        type_alignment_reason, source_column, confidence, mapping_mode,
                        approved, needs_review, reason
                    )
                    VALUES (
                        :import_run_id, :target_table, :target_field, :target_data_type,
                        :target_validation_kind, :source_inferred_type, :type_alignment,
                        :type_alignment_reason, :source_column, :confidence, :mapping_mode,
                        :approved, :needs_review, :reason
                    )
                    """
                ),
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
                    "confidence": mapping.get("confidence"),
                    "mapping_mode": mapping_mode,
                    "approved": bool(mapping.get("approved")),
                    "needs_review": bool(mapping.get("needs_review")),
                    "reason": mapping.get("reason") or mapping.get("rationale") or "",
                },
            )

        for source_column in source_coverage or []:
            conn.execute(
                text(
                    """
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
                    """
                ),
                {**source_column, "import_run_id": import_run_id},
            )

        issues_df = validation_result.issues_df
        for issue in dataframe_to_records(issues_df) if not issues_df.empty else []:
            conn.execute(
                text(
                    """
                    INSERT INTO validation_issues (
                        import_run_id, source_row_number, severity, issue_code,
                        issue_message, target_field, source_column
                    )
                    VALUES (
                        :import_run_id, :source_row_number, :severity, :issue_code,
                        :issue_message, :target_field, :source_column
                    )
                    """
                ),
                {**issue, "import_run_id": import_run_id},
            )

        for row in dataframe_to_records(outputs.rejected_rows) if not outputs.rejected_rows.empty else []:
            error_summary = str(row.get("errors") or "")
            source_row_number = int(row.get("source_row_number"))
            conn.execute(
                text(
                    """
                    INSERT INTO rejected_rows (
                        import_run_id, source_row_number, raw_payload_json, error_summary
                    )
                    VALUES (
                        :import_run_id, :source_row_number, CAST(:raw_payload_json AS jsonb), :error_summary
                    )
                    """
                ),
                {
                    "import_run_id": import_run_id,
                    "source_row_number": source_row_number,
                    "raw_payload_json": json.dumps(row),
                    "error_summary": error_summary,
                },
            )

        _upsert_members(conn, outputs.members, import_run_id)
        _upsert_plans(conn, outputs.plans, import_run_id)
        _upsert_member_coverage(conn, outputs.member_coverage, import_run_id)

    return import_run_id


def _upsert_members(conn: Any, members: pd.DataFrame, import_run_id: int) -> None:
    from sqlalchemy import text

    for row in dataframe_to_records(members):
        conn.execute(
            text(
                """
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
                """
            ),
            {**_clean_record(row), "import_run_id": import_run_id},
        )


def _upsert_plans(conn: Any, plans: pd.DataFrame, import_run_id: int) -> None:
    from sqlalchemy import text

    for row in dataframe_to_records(plans):
        conn.execute(
            text(
                """
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
                """
            ),
            {**_clean_record(row), "import_run_id": import_run_id},
        )


def _upsert_member_coverage(conn: Any, coverage: pd.DataFrame, import_run_id: int) -> None:
    from sqlalchemy import text

    for row in dataframe_to_records(coverage):
        conn.execute(
            text(
                """
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
                """
            ),
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
