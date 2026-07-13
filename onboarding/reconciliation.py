from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from onboarding.contracts import ContractVersion, default_contract
from onboarding.transform import TransformOutputs, dataframe_to_records
from onboarding.validation import ValidationResult

RECONCILIATION_STATUSES = {"PASS", "WARNING", "FAIL"}


class ReconciliationError(RuntimeError):
    def __init__(self, message: str, result: ReconciliationResult | None = None):
        self.result = result
        super().__init__(message)


@dataclass(frozen=True)
class ReconciliationCheck:
    check_code: str
    severity: str
    status: str
    expected_value: Any
    actual_value: Any
    message: str


@dataclass
class ReconciliationResult:
    stage: str
    status: str
    database_available: bool
    source_metrics: dict[str, Any]
    table_metrics: dict[str, dict[str, Any]]
    checks: list[ReconciliationCheck]
    policy: dict[str, Any]
    transaction_status: str = "not_started"
    generated_at: str = ""
    reconciliation_run_id: int | None = None

    def __post_init__(self) -> None:
        if self.status not in RECONCILIATION_STATUSES:
            raise ValueError(f"Unsupported reconciliation status: {self.status}.")
        if not self.generated_at:
            self.generated_at = datetime.now(UTC).isoformat(timespec="seconds")

    @property
    def blocking_failures(self) -> list[ReconciliationCheck]:
        return [check for check in self.checks if check.severity == "error" and check.status == "failed"]

    @property
    def warnings(self) -> list[ReconciliationCheck]:
        return [check for check in self.checks if check.severity == "warning" and check.status == "failed"]

    @property
    def can_publish(self) -> bool:
        return self.database_available and self.status != "FAIL"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "database_available": self.database_available,
            "source_metrics": self.source_metrics,
            "table_metrics": self.table_metrics,
            "checks": [asdict(check) for check in self.checks],
            "policy": self.policy,
            "transaction_status": self.transaction_status,
            "generated_at": self.generated_at,
            "reconciliation_run_id": self.reconciliation_run_id,
        }


def _status_from_checks(checks: list[ReconciliationCheck]) -> str:
    if any(check.severity == "error" and check.status == "failed" for check in checks):
        return "FAIL"
    if any(check.severity == "warning" and check.status == "failed" for check in checks):
        return "WARNING"
    return "PASS"


def _check(
    check_code: str,
    *,
    passed: bool,
    severity: str,
    expected: Any,
    actual: Any,
    pass_message: str,
    failure_message: str,
) -> ReconciliationCheck:
    return ReconciliationCheck(
        check_code=check_code,
        severity=severity,
        status="passed" if passed else "failed",
        expected_value=expected,
        actual_value=actual,
        message=pass_message if passed else failure_message,
    )


def _output_tables(outputs: TransformOutputs) -> dict[str, pd.DataFrame]:
    if outputs.tables:
        return outputs.tables
    return {
        "members": outputs.members,
        "plans": outputs.plans,
        "member_coverage": outputs.member_coverage,
    }


def _foreign_key_orphans(
    output_tables: dict[str, pd.DataFrame],
    contract: ContractVersion,
    existing_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    existing_rows = existing_rows or {}
    orphan_counts: dict[str, int] = {table: 0 for table in output_tables}
    for foreign_key in contract.foreign_keys:
        child_table = foreign_key["table"]
        child_field = foreign_key["field"]
        parent_table = foreign_key["references_table"]
        parent_field = foreign_key["references_field"]
        child_df = output_tables.get(child_table, pd.DataFrame())
        parent_df = output_tables.get(parent_table, pd.DataFrame())
        if child_df.empty or child_field not in child_df.columns:
            continue
        parent_values = (
            {_comparable(value) for value in parent_df[parent_field].dropna().tolist()}
            if not parent_df.empty and parent_field in parent_df.columns
            else set()
        )
        parent_values.update(
            _comparable(row.get(parent_field))
            for row in existing_rows.get(parent_table, [])
            if row.get(parent_field) is not None
        )
        orphan_counts[child_table] = orphan_counts.get(child_table, 0) + sum(
            1
            for value in child_df[child_field].tolist()
            if value is not None and _comparable(value) not in parent_values
        )
    return orphan_counts


def build_transform_reconciliation(
    validation_result: ValidationResult,
    outputs: TransformOutputs,
    *,
    contract: ContractVersion | None = None,
    acknowledged_reject_count: int = 0,
    database_available: bool = False,
) -> ReconciliationResult:
    selected_contract = contract or default_contract()
    source_rows = len(validation_result.normalized_df)
    accepted_rows = validation_result.accepted_row_count
    rejected_rows = validation_result.rejected_row_count
    warning_rows = (
        int(
            validation_result.issues_df.loc[
                lambda frame: frame["severity"].eq("warning"), "source_row_number"
            ].nunique()
        )
        if not validation_result.issues_df.empty
        else 0
    )
    source_metrics = {
        "source_rows": source_rows,
        "accepted_rows": accepted_rows,
        "rejected_rows": rejected_rows,
        "acknowledged_rejected_rows": acknowledged_reject_count,
        "warning_rows": warning_rows,
        "warning_count": validation_result.warning_count,
        "reject_rate": (rejected_rows / source_rows) if source_rows else 0.0,
    }

    policy = {
        "max_reject_rate": float(selected_contract.reconciliation_policy.get("max_reject_rate", 0.10)),
        "max_reject_rate_severity": str(
            selected_contract.reconciliation_policy.get("max_reject_rate_severity", "warning")
        ),
        "require_zero_orphans": bool(selected_contract.reconciliation_policy.get("require_zero_orphans", True)),
        "require_exact_row_accounting": bool(
            selected_contract.reconciliation_policy.get("require_exact_row_accounting", True)
        ),
    }
    checks: list[ReconciliationCheck] = []
    if policy["require_exact_row_accounting"]:
        checks.append(
            _check(
                "source_row_accounting",
                passed=source_rows == accepted_rows + rejected_rows,
                severity="error",
                expected=source_rows,
                actual=accepted_rows + rejected_rows,
                pass_message="Every source row is accepted or rejected.",
                failure_message="Source rows do not equal accepted plus rejected rows.",
            )
        )

    max_reject_rate = float(policy["max_reject_rate"])
    checks.append(
        _check(
            "maximum_reject_rate",
            passed=source_metrics["reject_rate"] <= max_reject_rate,
            severity=str(policy["max_reject_rate_severity"]),
            expected=max_reject_rate,
            actual=source_metrics["reject_rate"],
            pass_message="Reject rate is within the contract tolerance.",
            failure_message="Reject rate exceeds the contract tolerance.",
        )
    )

    output_tables = _output_tables(outputs)
    orphan_counts = _foreign_key_orphans(output_tables, selected_contract)
    table_metrics: dict[str, dict[str, Any]] = {}
    for table_name, output_df in output_tables.items():
        metrics = dict(outputs.table_stats.get(table_name) or {})
        metrics.setdefault("candidate_count", len(output_df))
        metrics.setdefault("unique_business_key_count", len(output_df))
        metrics.setdefault("exact_duplicate_count", 0)
        metrics.setdefault("conflicting_duplicate_count", 0)
        metrics.setdefault("missing_output_count", 0)
        metrics["orphan_count"] = orphan_counts.get(table_name, 0)
        metrics.setdefault("expected_insert_count", None)
        metrics.setdefault("expected_update_count", None)
        metrics.setdefault("expected_unchanged_count", None)
        metrics.setdefault("actual_insert_count", None)
        metrics.setdefault("actual_update_count", None)
        metrics.setdefault("actual_unchanged_count", None)
        table_metrics[table_name] = metrics

        checks.append(
            _check(
                f"{table_name}_conflicting_business_keys",
                passed=int(metrics["conflicting_duplicate_count"]) == 0,
                severity="error",
                expected=0,
                actual=int(metrics["conflicting_duplicate_count"]),
                pass_message=f"{table_name} has no conflicting duplicate business keys.",
                failure_message=f"{table_name} has conflicting duplicate business keys.",
            )
        )
        checks.append(
            _check(
                f"{table_name}_required_outputs",
                passed=int(metrics["missing_output_count"]) == 0,
                severity="error",
                expected=0,
                actual=int(metrics["missing_output_count"]),
                pass_message=f"{table_name} has all required target outputs.",
                failure_message=f"{table_name} is missing required target outputs.",
            )
        )
        if policy["require_zero_orphans"]:
            checks.append(
                _check(
                    f"{table_name}_foreign_key_orphans",
                    passed=int(metrics["orphan_count"]) == 0,
                    severity="error",
                    expected=0,
                    actual=int(metrics["orphan_count"]),
                    pass_message=f"{table_name} has no orphaned relationships.",
                    failure_message=f"{table_name} has orphaned relationships.",
                )
            )

    if acknowledged_reject_count:
        checks.append(
            _check(
                "acknowledged_rejects",
                passed=False,
                severity="warning",
                expected=0,
                actual=acknowledged_reject_count,
                pass_message="No unresolved rejected rows were acknowledged.",
                failure_message="The run contains acknowledged unresolved rejects.",
            )
        )

    return ReconciliationResult(
        stage="pre_publish" if database_available else "transform_only",
        status=_status_from_checks(checks),
        database_available=database_available,
        source_metrics=source_metrics,
        table_metrics=table_metrics,
        checks=checks,
        policy=policy,
    )


def _quote(connection: Any, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _contract_fields(contract: ContractVersion, table_name: str) -> list[str]:
    return [field.field for field in contract.target_fields if field.table == table_name]


def _comparable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def _existing_rows(connection: Any, table_name: str, fields: list[str]) -> list[dict[str, Any]]:
    from sqlalchemy import inspect, text

    inspector = inspect(connection)
    if not inspector.has_table(table_name):
        raise ReconciliationError(f"Target table {table_name} does not exist.")
    available_columns = {column["name"] for column in inspector.get_columns(table_name)}
    missing_columns = [field for field in fields if field not in available_columns]
    if missing_columns:
        raise ReconciliationError(
            f"Target table {table_name} is missing contract fields: {', '.join(missing_columns)}."
        )
    quoted_fields = ", ".join(_quote(connection, field) for field in fields)
    quoted_table = _quote(connection, table_name)
    return [dict(row) for row in connection.execute(text(f"SELECT {quoted_fields} FROM {quoted_table}")).mappings()]


def _rows_by_key(rows: list[dict[str, Any]], business_key: list[str]) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {tuple(_comparable(row.get(field)) for field in business_key): row for row in rows}


def forecast_with_connection(
    connection: Any,
    validation_result: ValidationResult,
    outputs: TransformOutputs,
    *,
    contract: ContractVersion | None = None,
    acknowledged_reject_count: int = 0,
) -> ReconciliationResult:
    selected_contract = contract or default_contract()
    result = build_transform_reconciliation(
        validation_result,
        outputs,
        contract=selected_contract,
        acknowledged_reject_count=acknowledged_reject_count,
        database_available=True,
    )
    output_tables = _output_tables(outputs)
    existing_by_table: dict[str, list[dict[str, Any]]] = {}

    for table_name in selected_contract.table_names:
        fields = _contract_fields(selected_contract, table_name)
        try:
            existing_by_table[table_name] = _existing_rows(connection, table_name, fields)
        except ReconciliationError as exc:
            existing_by_table[table_name] = []
            result.checks.append(
                ReconciliationCheck(
                    check_code=f"{table_name}_target_contract_alignment",
                    severity="error",
                    status="failed",
                    expected_value=fields,
                    actual_value=[],
                    message=str(exc),
                )
            )
            continue

        output_df = output_tables.get(table_name, pd.DataFrame(columns=fields))
        business_key = selected_contract.business_keys.get(table_name) or selected_contract.primary_keys.get(table_name)
        existing_lookup = _rows_by_key(existing_by_table[table_name], business_key)
        insert_count = 0
        update_count = 0
        unchanged_count = 0
        for row in dataframe_to_records(output_df):
            key = tuple(_comparable(row.get(field)) for field in business_key)
            existing = existing_lookup.get(key)
            if existing is None:
                insert_count += 1
                continue
            if all(_comparable(existing.get(field)) == _comparable(row.get(field)) for field in fields):
                unchanged_count += 1
            else:
                update_count += 1
        metrics = result.table_metrics.setdefault(table_name, {})
        metrics["expected_insert_count"] = insert_count
        metrics["expected_update_count"] = update_count
        metrics["expected_unchanged_count"] = unchanged_count

    orphan_counts = _foreign_key_orphans(output_tables, selected_contract, existing_by_table)
    for table_name, orphan_count in orphan_counts.items():
        result.table_metrics.setdefault(table_name, {})["orphan_count"] = orphan_count
        for check_index, check in enumerate(result.checks):
            if check.check_code != f"{table_name}_foreign_key_orphans":
                continue
            result.checks[check_index] = _check(
                check.check_code,
                passed=orphan_count == 0,
                severity="error",
                expected=0,
                actual=orphan_count,
                pass_message=f"{table_name} has no orphaned relationships.",
                failure_message=f"{table_name} has orphaned relationships.",
            )
            break

    result.status = _status_from_checks(result.checks)
    return result


def build_pre_publish_reconciliation(
    engine: Any,
    validation_result: ValidationResult,
    outputs: TransformOutputs,
    *,
    contract: ContractVersion | None = None,
    acknowledged_reject_count: int = 0,
) -> ReconciliationResult:
    with engine.connect() as connection:
        return forecast_with_connection(
            connection,
            validation_result,
            outputs,
            contract=contract,
            acknowledged_reject_count=acknowledged_reject_count,
        )


def verify_post_publish_with_connection(
    connection: Any,
    outputs: TransformOutputs,
    forecast: ReconciliationResult,
    *,
    contract: ContractVersion | None = None,
) -> ReconciliationResult:
    selected_contract = contract or default_contract()
    checks = list(forecast.checks)
    table_metrics = {table: dict(metrics) for table, metrics in forecast.table_metrics.items()}
    output_tables = _output_tables(outputs)

    for table_name in selected_contract.table_names:
        fields = _contract_fields(selected_contract, table_name)
        business_key = selected_contract.business_keys.get(table_name) or selected_contract.primary_keys.get(table_name)
        stored_rows = _existing_rows(connection, table_name, fields)
        stored_lookup = _rows_by_key(stored_rows, business_key)
        output_df = output_tables.get(table_name, pd.DataFrame(columns=fields))
        mismatches = 0
        for row in dataframe_to_records(output_df):
            key = tuple(_comparable(row.get(field)) for field in business_key)
            stored = stored_lookup.get(key)
            if stored is None or any(_comparable(stored.get(field)) != _comparable(row.get(field)) for field in fields):
                mismatches += 1
        checks.append(
            _check(
                f"{table_name}_post_publish_values",
                passed=mismatches == 0,
                severity="error",
                expected=0,
                actual=mismatches,
                pass_message=f"{table_name} stored values match the transformed output.",
                failure_message=f"{table_name} stored values do not match the transformed output.",
            )
        )
        metrics = table_metrics.setdefault(table_name, {})
        metrics["actual_insert_count"] = metrics.get("expected_insert_count")
        metrics["actual_update_count"] = metrics.get("expected_update_count")
        metrics["actual_unchanged_count"] = metrics.get("expected_unchanged_count")
        forecast_total = sum(
            int(metrics.get(key) or 0)
            for key in ["expected_insert_count", "expected_update_count", "expected_unchanged_count"]
        )
        actual_total = len(output_df)
        checks.append(
            _check(
                f"{table_name}_forecast_actual_count",
                passed=forecast_total == actual_total,
                severity="error",
                expected=forecast_total,
                actual=actual_total,
                pass_message=f"{table_name} forecast and actual counts agree.",
                failure_message=f"{table_name} forecast and actual counts differ.",
            )
        )

    status = _status_from_checks(checks)
    return ReconciliationResult(
        stage="post_publish",
        status=status,
        database_available=True,
        source_metrics=dict(forecast.source_metrics),
        table_metrics=table_metrics,
        checks=checks,
        policy=dict(forecast.policy),
        transaction_status="committed" if status != "FAIL" else "rolled_back",
    )


def persist_reconciliation(connection: Any, import_run_id: int, result: ReconciliationResult) -> int:
    from sqlalchemy import text

    inserted = connection.execute(
        text("""
            INSERT INTO reconciliation_runs (
                import_run_id, stage, status, database_available, source_metrics_json,
                policy_json, blocking_failure_count, warning_count, transaction_status, created_at
            )
            VALUES (
                :import_run_id, :stage, :status, :database_available, :source_metrics_json,
                :policy_json, :blocking_failure_count, :warning_count, :transaction_status, :created_at
            )
            RETURNING id
            """),
        {
            "import_run_id": import_run_id,
            "stage": result.stage,
            "status": result.status,
            "database_available": result.database_available,
            "source_metrics_json": json.dumps(result.source_metrics, ensure_ascii=True),
            "policy_json": json.dumps(result.policy, ensure_ascii=True),
            "blocking_failure_count": len(result.blocking_failures),
            "warning_count": len(result.warnings),
            "transaction_status": result.transaction_status,
            "created_at": datetime.now(UTC).replace(tzinfo=None),
        },
    )
    reconciliation_run_id = int(inserted.scalar_one())
    for table_name, metrics in result.table_metrics.items():
        connection.execute(
            text("""
                INSERT INTO reconciliation_table_metrics (
                    reconciliation_run_id, target_table, candidate_count,
                    unique_business_key_count, exact_duplicate_count,
                    conflicting_duplicate_count, expected_insert_count,
                    expected_update_count, expected_unchanged_count,
                    actual_insert_count, actual_update_count, actual_unchanged_count,
                    orphan_count, missing_output_count
                )
                VALUES (
                    :reconciliation_run_id, :target_table, :candidate_count,
                    :unique_business_key_count, :exact_duplicate_count,
                    :conflicting_duplicate_count, :expected_insert_count,
                    :expected_update_count, :expected_unchanged_count,
                    :actual_insert_count, :actual_update_count, :actual_unchanged_count,
                    :orphan_count, :missing_output_count
                )
                """),
            {
                "reconciliation_run_id": reconciliation_run_id,
                "target_table": table_name,
                "candidate_count": metrics.get("candidate_count"),
                "unique_business_key_count": metrics.get("unique_business_key_count"),
                "exact_duplicate_count": metrics.get("exact_duplicate_count"),
                "conflicting_duplicate_count": metrics.get("conflicting_duplicate_count"),
                "expected_insert_count": metrics.get("expected_insert_count"),
                "expected_update_count": metrics.get("expected_update_count"),
                "expected_unchanged_count": metrics.get("expected_unchanged_count"),
                "actual_insert_count": metrics.get("actual_insert_count"),
                "actual_update_count": metrics.get("actual_update_count"),
                "actual_unchanged_count": metrics.get("actual_unchanged_count"),
                "orphan_count": metrics.get("orphan_count"),
                "missing_output_count": metrics.get("missing_output_count"),
            },
        )
    for check in result.checks:
        connection.execute(
            text("""
                INSERT INTO reconciliation_checks (
                    reconciliation_run_id, check_code, severity, status,
                    expected_value_json, actual_value_json, message
                )
                VALUES (
                    :reconciliation_run_id, :check_code, :severity, :status,
                    :expected_value_json, :actual_value_json, :message
                )
                """),
            {
                "reconciliation_run_id": reconciliation_run_id,
                "check_code": check.check_code,
                "severity": check.severity,
                "status": check.status,
                "expected_value_json": json.dumps(check.expected_value, ensure_ascii=True),
                "actual_value_json": json.dumps(check.actual_value, ensure_ascii=True),
                "message": check.message,
            },
        )
    result.reconciliation_run_id = reconciliation_run_id
    return reconciliation_run_id


def reconciliation_json_bytes(result: ReconciliationResult) -> bytes:
    return json.dumps(result.to_dict(), indent=2, ensure_ascii=True).encode("utf-8")
