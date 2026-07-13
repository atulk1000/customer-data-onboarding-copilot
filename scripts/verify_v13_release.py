from __future__ import annotations

# ruff: noqa: E402, I001

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from onboarding.contracts import ensure_default_contract
from onboarding.corrections import (
    apply_correction_overlays,
    correction_audit_rows,
    validate_correction_upload,
)
from onboarding.database import PublishOutcome, get_engine, init_db, publish_import
from onboarding.idempotency import build_import_replay_check, source_dataframe_fingerprint
from onboarding.mapping_quality import apply_mapping_type_alignment
from onboarding.profiler import profile_dataframe
from onboarding.reconciliation import ReconciliationError, build_pre_publish_reconciliation
from onboarding.rules_mapper import generate_rules_based_mappings
from onboarding.source_coverage import build_source_coverage
from onboarding.transform import approved_source_columns_by_target_field, build_canonical_flat, transform_outputs
from onboarding.validation import ValidationResult, validate_canonical_frame


def synthetic_clean_source() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Member Number": ["V13-M001", "V13-M002", "V13-M003"],
            "First": ["Ana", "Sam", "Priya"],
            "Last": ["Patel", "Lee", "Khan"],
            "DOB": ["1988-01-01", "1977-02-03", "1991-04-05"],
            "Sex": ["F", "M", "F"],
            "Email Address": ["ana.v13@example.com", "sam.v13@example.com", "priya.v13@example.com"],
            "Phone": ["555-111-2222", "555-222-3333", "555-333-4444"],
            "Plan Code": ["V13-PPO", "V13-HMO", "V13-PPO"],
            "Plan Name": ["V1.3 Silver PPO", "V1.3 Basic HMO", "V1.3 Silver PPO"],
            "Plan Type": ["PPO", "HMO", "PPO"],
            "Carrier": ["Example Health", "Example Health", "Example Health"],
            "Effective Date": ["2026-01-01", "2026-01-01", "2026-01-01"],
            "Term Date": ["", "", ""],
            "Status": ["Active", "Active", "Active"],
            "Relation": ["Self", "Self", "Self"],
            "Subscriber ID": ["V13-M001", "V13-M002", "V13-M003"],
        }
    )


def build_verified_pipeline(source: pd.DataFrame, contract):
    started = perf_counter()
    profiles = profile_dataframe(source)
    print(f"profile_seconds={perf_counter() - started:.2f}", flush=True)
    stage_started = perf_counter()
    mappings = apply_mapping_type_alignment(
        generate_rules_based_mappings(profiles, contract.target_fields),
        profiles,
        contract.target_fields,
    )
    for mapping in mappings:
        if mapping.get("source_column"):
            mapping["approved"] = True
            mapping["transformation_approved"] = True
    print(f"mapping_seconds={perf_counter() - stage_started:.2f}", flush=True)

    stage_started = perf_counter()
    source_hash = source_dataframe_fingerprint(source)
    flat = build_canonical_flat(
        source,
        mappings,
        contract.target_fields,
        source_file_hash=source_hash,
    )
    print(f"canonical_build_seconds={perf_counter() - stage_started:.2f}", flush=True)
    stage_started = perf_counter()
    validation = validate_canonical_frame(
        flat,
        approved_source_columns_by_target_field(mappings),
        contract.target_fields,
    )
    print(f"validation_seconds={perf_counter() - stage_started:.2f}", flush=True)
    stage_started = perf_counter()
    outputs = transform_outputs(
        validation.normalized_df,
        source,
        validation.issues_df,
        mappings,
        target_schema=contract.target_fields,
        contract=contract,
        source_file_hash=source_hash,
        mapping_template_version="1",
    )
    print(f"transform_seconds={perf_counter() - stage_started:.2f}", flush=True)
    print(f"pipeline_seconds={perf_counter() - started:.2f}", flush=True)
    return profiles, mappings, validation, outputs, source_hash


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the v1.3 contract, transformation, and publish workflow.")
    parser.add_argument("--publish", action="store_true", help="Publish the synthetic verification records.")
    parser.add_argument(
        "--demo-file",
        action="store_true",
        help="Use the repository's 1,000-row messy demo file for transform-only verification.",
    )
    parser.add_argument(
        "--verify-rollback",
        action="store_true",
        help="Force a hard reconciliation failure and verify its audit record.",
    )
    parser.add_argument(
        "--verify-correction",
        action="store_true",
        help="Publish a parent run with a reject, then correct and publish its child run.",
    )
    args = parser.parse_args()

    engine = get_engine()
    init_db(engine)
    contract = ensure_default_contract(engine)
    source = (
        pd.read_csv(ROOT / "data" / "demo" / "messy_eligibility_file.csv")
        if args.demo_file
        else synthetic_clean_source()
    )
    profiles, mappings, validation, outputs, source_hash = build_verified_pipeline(source, contract)
    reconciliation = build_pre_publish_reconciliation(
        engine,
        validation,
        outputs,
        contract=contract,
    )

    summary = {
        "contract": f"{contract.contract_key}:{contract.version}",
        "source_rows": len(source),
        "profiled_columns": len(profiles),
        "accepted_rows": validation.accepted_row_count,
        "rejected_rows": validation.rejected_row_count,
        "reconciliation_status": reconciliation.status,
        "table_metrics": reconciliation.table_metrics,
    }
    print(json.dumps(summary, indent=2, default=str))

    if not args.publish:
        return
    if args.demo_file:
        raise RuntimeError(
            "The messy demo file is transform-only in this verifier; review hard reconciliation failures in the app."
        )
    replay_check = build_import_replay_check(engine, source_hash)
    outcome = publish_import(
        engine=engine,
        file_name="v1.3-verification.csv",
        mapping_mode="Rules-Based",
        mappings=mappings,
        validation_result=validation,
        outputs=outputs,
        target_schema_name=contract.name,
        target_schema_version=contract.version,
        mapping_template_name="V1.3 verification",
        mapping_template_version=1,
        source_file_hash=source_hash,
        import_replay_check=replay_check,
        replay_acknowledged=bool(replay_check.get("is_replay")),
        source_coverage=build_source_coverage(list(source.columns), profiles, mappings),
        source_coverage_reviewed=True,
        signoff={
            "reviewer_name": "V1.3 verifier",
            "reviewer_role": "Automated local verification",
            "decision": "Approved to publish accepted records",
            "comment": "Synthetic release verification.",
        },
        contract=contract,
        return_outcome=True,
    )
    if not isinstance(outcome, PublishOutcome):
        raise RuntimeError("Publish did not return a detailed outcome.")
    print(
        json.dumps(
            {
                "import_run_id": outcome.import_run_id,
                "pre_publish": outcome.pre_reconciliation.status,
                "post_publish": outcome.post_reconciliation.status,
                "transaction": outcome.post_reconciliation.transaction_status,
            },
            indent=2,
        )
    )
    with engine.connect() as connection:
        reconciliation_count = connection.execute(
            text("SELECT count(*) FROM reconciliation_runs WHERE import_run_id = :import_run_id"),
            {"import_run_id": outcome.import_run_id},
        ).scalar_one()
        lineage_count = connection.execute(
            text("SELECT count(*) FROM field_lineage WHERE import_run_id = :import_run_id"),
            {"import_run_id": outcome.import_run_id},
        ).scalar_one()
    print(json.dumps({"reconciliation_audit_rows": reconciliation_count, "lineage_rows": lineage_count}, indent=2))

    if args.verify_rollback:
        outputs.table_stats["plans"]["conflicting_duplicate_count"] = 1
        try:
            publish_import(
                engine=engine,
                file_name="v1.3-rollback-verification.csv",
                mapping_mode="Rules-Based",
                mappings=mappings,
                validation_result=validation,
                outputs=outputs,
                target_schema_name=contract.name,
                target_schema_version=contract.version,
                mapping_template_name="V1.3 rollback verification",
                mapping_template_version=1,
                source_file_hash=source_hash + "-rollback",
                source_coverage=build_source_coverage(list(source.columns), profiles, mappings),
                source_coverage_reviewed=True,
                signoff={"reviewer_name": "V1.3 verifier", "decision": "Approved with warnings"},
                contract=contract,
                return_outcome=True,
            )
        except ReconciliationError as exc:
            failed_run_id = getattr(exc, "failed_import_run_id", None)
            if failed_run_id is None:
                raise RuntimeError("Rollback verification did not retain a failed import run.") from exc
            with engine.connect() as connection:
                failed_status = (
                    connection.execute(
                        text("""
                        SELECT status, reconciliation_status, publish_transaction_status
                        FROM import_runs WHERE id = :import_run_id
                        """),
                        {"import_run_id": failed_run_id},
                    )
                    .mappings()
                    .one()
                )
            print(json.dumps({"failed_import_run_id": failed_run_id, **dict(failed_status)}, indent=2))
        else:
            raise RuntimeError("Rollback verification unexpectedly published canonical records.")

    if not args.verify_correction:
        return
    rejected_source = synthetic_clean_source().copy()
    rejected_source.loc[0, "DOB"] = "invalid-date"
    correction_profiles, correction_mappings, parent_validation, parent_outputs, parent_hash = build_verified_pipeline(
        rejected_source,
        contract,
    )
    parent_outcome = publish_import(
        engine=engine,
        file_name="v1.3-correction-parent.csv",
        mapping_mode="Rules-Based",
        mappings=correction_mappings,
        validation_result=parent_validation,
        outputs=parent_outputs,
        target_schema_name=contract.name,
        target_schema_version=contract.version,
        mapping_template_name="V1.3 correction verification",
        mapping_template_version=1,
        source_file_hash=parent_hash,
        import_replay_check=build_import_replay_check(engine, parent_hash),
        replay_acknowledged=True,
        source_coverage=build_source_coverage(list(rejected_source.columns), correction_profiles, correction_mappings),
        source_coverage_reviewed=True,
        signoff={"reviewer_name": "V1.3 verifier", "decision": "Approved with warnings"},
        contract=contract,
        return_outcome=True,
    )
    if not isinstance(parent_outcome, PublishOutcome):
        raise RuntimeError("Correction parent publish did not return a detailed outcome.")

    correction_file = parent_outputs.correction_work_queue.copy()
    correction_file.loc[:, "corrected__DOB"] = "1988-01-01"
    correction_file.loc[:, "correction_comment"] = "Verified date of birth."
    correction_result = validate_correction_upload(correction_file, parent_outputs.correction_work_queue)
    if not correction_result.is_valid:
        raise RuntimeError("Correction verification file was invalid: " + "; ".join(correction_result.errors))
    corrected_source = apply_correction_overlays(rejected_source, parent_hash, correction_result.overlays)
    corrected_flat = build_canonical_flat(
        corrected_source,
        correction_mappings,
        contract.target_fields,
        source_file_hash=parent_hash,
    )
    corrected_full_result = validate_canonical_frame(
        corrected_flat,
        approved_source_columns_by_target_field(correction_mappings),
        contract.target_fields,
    )
    selected_rows = {int(overlay["source_row_number"]) for overlay in correction_result.overlays}
    corrected_normalized = corrected_full_result.normalized_df[
        corrected_full_result.normalized_df["source_row_number"].isin(selected_rows)
    ].copy()
    corrected_normalized.attrs.update(corrected_full_result.normalized_df.attrs)
    recovery_validation = ValidationResult(
        normalized_df=corrected_normalized,
        issues=[issue for issue in corrected_full_result.issues if issue.source_row_number in selected_rows],
    )
    correction_audit = correction_audit_rows(
        correction_result.overlays,
        rejected_source,
        corrected_by="V1.3 verifier",
    )
    for row in correction_audit:
        row["correction_status"] = "recovered"
    recovery_outputs = transform_outputs(
        recovery_validation.normalized_df,
        corrected_source,
        recovery_validation.issues_df,
        correction_mappings,
        target_schema=contract.target_fields,
        contract=contract,
        source_file_hash=parent_hash,
        original_source_df=rejected_source,
        correction_audit=correction_audit,
        mapping_template_version="1",
        parent_import_run_id=parent_outcome.import_run_id,
    )
    child_outcome = publish_import(
        engine=engine,
        file_name="v1.3-correction-child.csv",
        mapping_mode="Rules-Based",
        mappings=correction_mappings,
        validation_result=recovery_validation,
        outputs=recovery_outputs,
        target_schema_name=contract.name,
        target_schema_version=contract.version,
        mapping_template_name="V1.3 correction verification",
        mapping_template_version=1,
        source_file_hash=parent_hash,
        source_coverage=build_source_coverage(list(rejected_source.columns), correction_profiles, correction_mappings),
        source_coverage_reviewed=True,
        signoff={"reviewer_name": "V1.3 verifier", "decision": "Approved to publish accepted records"},
        contract=contract,
        parent_import_run_id=parent_outcome.import_run_id,
        run_kind="row_correction",
        correction_attempt_number=1,
        corrections=correction_audit,
        return_outcome=True,
    )
    if not isinstance(child_outcome, PublishOutcome):
        raise RuntimeError("Correction child publish did not return a detailed outcome.")
    with engine.connect() as connection:
        correction_count = connection.execute(
            text("SELECT count(*) FROM row_corrections WHERE child_import_run_id = :child_import_run_id"),
            {"child_import_run_id": child_outcome.import_run_id},
        ).scalar_one()
    print(
        json.dumps(
            {
                "parent_import_run_id": parent_outcome.import_run_id,
                "parent_rejected_rows": parent_validation.rejected_row_count,
                "child_import_run_id": child_outcome.import_run_id,
                "child_recovered_rows": recovery_validation.accepted_row_count,
                "persisted_corrections": correction_count,
                "child_transaction": child_outcome.post_reconciliation.transaction_status,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
