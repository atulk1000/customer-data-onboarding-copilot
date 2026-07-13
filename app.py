from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from onboarding.ai_mapper import AIMapperValidationError, OpenAIConfigurationError, suggest_mappings_with_ai
from onboarding.contracts import (
    ContractRegistryError,
    ContractValidationError,
    ContractVersion,
    contract_json_bytes,
    default_contract,
    ensure_default_contract,
    list_contract_versions,
    save_contract_version,
    transition_contract_status,
)
from onboarding.corrections import (
    apply_correction_overlays,
    correction_audit_rows,
    validate_correction_upload,
)
from onboarding.database import (
    PublishOutcome,
    connection_status,
    ensure_contract_target_tables,
    get_engine,
    init_db,
    publish_import,
)
from onboarding.exports import dataframe_to_csv_bytes
from onboarding.idempotency import build_import_replay_check, source_dataframe_fingerprint
from onboarding.mapping_quality import apply_mapping_type_alignment, blocking_mapping_alignment_issues
from onboarding.mapping_templates import (
    MappingTemplateCompatibilityError,
    apply_mapping_template,
    list_mapping_templates,
    load_mapping_template,
    mapping_configuration_checksum,
    save_mapping_template,
)
from onboarding.profiler import profile_dataframe
from onboarding.reconciliation import (
    ReconciliationError,
    build_pre_publish_reconciliation,
    build_transform_reconciliation,
    reconciliation_json_bytes,
)
from onboarding.reports import build_report_data, render_html_report, render_pdf_report
from onboarding.rules_mapper import generate_rules_based_mappings
from onboarding.schema import TARGET_SCHEMA, TargetField, target_fields_by_key
from onboarding.source_coverage import build_source_coverage, source_coverage_summary, unused_source_columns
from onboarding.transform import approved_source_columns_by_target_field, build_canonical_flat, transform_outputs
from onboarding.transformations import (
    APPROVED_OPERATIONS,
    FAILURE_POLICIES,
    normalize_steps,
    preview_transformation_pipeline,
    recommended_steps,
    validate_transformation_pipeline,
)
from onboarding.validation import ValidationResult, issue_summary, validate_canonical_frame

ROOT = Path(__file__).resolve().parent
DEMO_FILE = ROOT / "data" / "demo" / "messy_eligibility_file.csv"
FALLBACK_CONTRACT = default_contract()
TARGET_SCHEMA_NAME = FALLBACK_CONTRACT.name
TARGET_SCHEMA_VERSION = FALLBACK_CONTRACT.version
WORKFLOW_STEPS = ["Target", "Upload", "Profile", "Map", "Validate", "Transform", "Publish", "Report"]
SIGNOFF_DECISIONS = [
    "Approved to publish accepted records",
    "Approved with warnings",
    "Needs customer correction",
]
PROFILE_TABLE_VIEWS = {
    "Summary": [
        "column_name",
        "normalized_name",
        "inferred_type",
        "null_rate_pct",
        "unique_rate_pct",
        "enum_match_fields",
    ],
    "Value pattern signals": [
        "column_name",
        "inferred_type",
        "date_parse_rate_pct",
        "email_pattern_rate_pct",
        "phone_pattern_rate_pct",
        "numeric_parse_rate_pct",
        "text_value_rate_pct",
        "enum_match_fields",
    ],
    "Cardinality & dates": [
        "column_name",
        "non_null_count",
        "null_rate_pct",
        "unique_count",
        "unique_rate_pct",
        "min_date",
        "max_date",
    ],
}
PROFILE_COLUMN_CONFIG = {
    "column_name": st.column_config.TextColumn("source column", width="medium"),
    "normalized_name": st.column_config.TextColumn("normalized", width="medium"),
    "inferred_type": st.column_config.TextColumn("inferred type", width="small"),
    "non_null_count": st.column_config.NumberColumn("non-null", width="small"),
    "unique_count": st.column_config.NumberColumn("unique", width="small"),
    "null_rate_pct": st.column_config.NumberColumn("null %", format="%.1f", width="small"),
    "unique_rate_pct": st.column_config.NumberColumn("unique %", format="%.1f", width="small"),
    "date_parse_rate_pct": st.column_config.NumberColumn("date %", format="%.1f", width="small"),
    "email_pattern_rate_pct": st.column_config.NumberColumn("email %", format="%.1f", width="small"),
    "phone_pattern_rate_pct": st.column_config.NumberColumn("phone %", format="%.1f", width="small"),
    "numeric_parse_rate_pct": st.column_config.NumberColumn("numeric %", format="%.1f", width="small"),
    "text_value_rate_pct": st.column_config.NumberColumn("text %", format="%.1f", width="small"),
    "enum_match_fields": st.column_config.TextColumn("enum hints", width="large"),
    "min_date": st.column_config.TextColumn("min date", width="small"),
    "max_date": st.column_config.TextColumn("max date", width="small"),
}


def init_state() -> None:
    defaults = {
        "contract": FALLBACK_CONTRACT,
        "contract_registry": [FALLBACK_CONTRACT],
        "contract_registry_error": "",
        "contract_registry_loaded": False,
        "source_df": None,
        "file_name": None,
        "profiles": None,
        "mapping_mode": "Rules-Based",
        "mappings": None,
        "validation_result": None,
        "outputs": None,
        "published": False,
        "import_run_id": None,
        "source_coverage_reviewed": False,
        "signoff": None,
        "mapping_template_name": "",
        "mapping_template_version": 1,
        "validated_mapping_checksum": "",
        "import_replay_acknowledged": False,
        "import_replay_check": None,
        "pre_reconciliation": None,
        "post_reconciliation": None,
        "correction_overlays": [],
        "correction_audit": [],
        "corrected_source_df": None,
        "recovery_validation_result": None,
        "recovery_outputs": None,
        "acknowledged_rejects": {},
        "parent_import_run_id": None,
        "run_kind": "original",
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def reset_downstream() -> None:
    for key in [
        "profiles",
        "mappings",
        "validation_result",
        "outputs",
        "published",
        "import_run_id",
        "signoff",
        "pre_reconciliation",
        "post_reconciliation",
        "corrected_source_df",
        "recovery_validation_result",
        "recovery_outputs",
    ]:
        st.session_state[key] = None if key not in {"published"} else False
    st.session_state.source_coverage_reviewed = False
    st.session_state.mapping_template_name = ""
    st.session_state.mapping_template_version = 1
    st.session_state.validated_mapping_checksum = ""
    st.session_state.import_replay_acknowledged = False
    st.session_state.import_replay_check = None
    st.session_state.correction_overlays = []
    st.session_state.correction_audit = []
    st.session_state.acknowledged_rejects = {}
    st.session_state.parent_import_run_id = None
    st.session_state.run_kind = "original"


def load_source_df(df: pd.DataFrame, file_name: str) -> None:
    st.session_state.source_df = df
    st.session_state.file_name = file_name
    reset_downstream()


def current_contract() -> ContractVersion:
    contract = st.session_state.get("contract")
    return contract if isinstance(contract, ContractVersion) else FALLBACK_CONTRACT


def current_target_schema() -> list[TargetField]:
    return current_contract().target_fields


def refresh_contract_registry() -> None:
    try:
        engine = get_engine()
        init_db(engine)
        ensure_default_contract(engine)
        contracts = list_contract_versions(engine)
        st.session_state.contract_registry = contracts
        st.session_state.contract_registry_error = ""
        st.session_state.contract_registry_loaded = True
        selected = current_contract()
        replacement = next(
            (
                contract
                for contract in contracts
                if contract.contract_key == selected.contract_key and contract.version == selected.version
            ),
            None,
        )
        if replacement is not None:
            st.session_state.contract = replacement
    except Exception as exc:
        st.session_state.contract_registry = [FALLBACK_CONTRACT]
        st.session_state.contract_registry_error = str(exc)
        st.session_state.contract_registry_loaded = True


def set_selected_contract(contract: ContractVersion) -> None:
    previous = current_contract()
    st.session_state.contract = contract
    if previous.contract_key == contract.contract_key and previous.version == contract.version:
        return
    for key in [
        "mappings",
        "validation_result",
        "outputs",
        "pre_reconciliation",
        "post_reconciliation",
        "recovery_validation_result",
        "recovery_outputs",
    ]:
        st.session_state[key] = None
    st.session_state.mapping_template_name = ""
    st.session_state.mapping_template_version = 1
    st.session_state.validated_mapping_checksum = ""
    st.session_state.published = False
    st.session_state.import_run_id = None


def active_validation_result():
    return st.session_state.recovery_validation_result or st.session_state.validation_result


def active_outputs():
    return st.session_state.recovery_outputs or st.session_state.outputs


def active_source_df() -> pd.DataFrame | None:
    return (
        st.session_state.corrected_source_df
        if st.session_state.recovery_outputs is not None
        else st.session_state.source_df
    )


def invalidate_after_mapping_change() -> None:
    if st.session_state.import_run_id:
        st.session_state.parent_import_run_id = st.session_state.import_run_id
        st.session_state.run_kind = "full_rerun"
    for key in [
        "validation_result",
        "outputs",
        "pre_reconciliation",
        "post_reconciliation",
        "recovery_validation_result",
        "recovery_outputs",
        "corrected_source_df",
    ]:
        st.session_state[key] = None
    st.session_state.validated_mapping_checksum = ""
    st.session_state.published = False


def mapping_editor_rows(mappings: list[dict[str, Any]]) -> pd.DataFrame:
    display_rows = []
    for mapping in mappings:
        row = dict(mapping)
        row["review_flags"] = ", ".join(mapping.get("review_flags") or [])
        row["score_breakdown"] = str(mapping.get("score_breakdown") or "")
        row["source_columns"] = "; ".join(
            str(value) for value in (mapping.get("source_columns") or [mapping.get("source_column")]) if value
        )
        row["transformation_steps"] = json.dumps(mapping.get("transformation_steps") or [], ensure_ascii=True)
        display_rows.append(row)
    return pd.DataFrame(display_rows)


def rows_to_mappings(rows: pd.DataFrame) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    for row in rows.to_dict("records"):
        flags = row.get("review_flags", "")
        row["review_flags"] = [flag.strip() for flag in str(flags).split(",") if flag.strip()]
        if pd.isna(row.get("source_column")):
            row["source_column"] = ""
        source_columns = [value.strip() for value in str(row.get("source_columns") or "").split(";") if value.strip()]
        source_column = str(row.get("source_column") or "").strip()
        if source_column and (not source_columns or len(source_columns) == 1):
            source_columns = [source_column]
        row["source_columns"] = source_columns
        try:
            row["transformation_steps"] = normalize_steps(row.get("transformation_steps") or "[]")
        except ValueError:
            row["transformation_steps"] = []
        row["approved"] = bool(row.get("approved", False))
        mappings.append(row)
    return mappings


def required_mapping_gaps(
    mappings: list[dict[str, Any]],
    target_schema: list[TargetField] | None = None,
) -> list[str]:
    approved_by_key = {
        (mapping.get("target_table"), mapping.get("target_field"))
        for mapping in mappings
        if mapping.get("approved") and mapping.get("source_column")
    }
    gaps = []
    for field in target_schema or TARGET_SCHEMA:
        if field.field == "coverage_id":
            continue
        if field.required and (field.table, field.field) not in approved_by_key:
            gaps.append(f"{field.table}.{field.field}")
    return gaps


def target_schema_rows(contract: ContractVersion | None = None) -> pd.DataFrame:
    rows = []
    selected_contract = contract or current_contract()
    for field in selected_contract.target_fields:
        rows.append(
            {
                "target_table": field.table,
                "target_field": field.field,
                "required": "generated" if field.generated else field.required,
                "target_data_type": field.data_type,
                "nullable": field.nullable,
                "validation_kind": field.validation_kind,
                "allowed_values": ", ".join(field.allowed_values),
                "description": field.description,
            }
        )
    return pd.DataFrame(rows)


def profile_display_rows(profiles: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    rows = []
    for profile in profiles:
        known_enum_matches = profile.get("known_enum_matches") or {}
        row = dict(profile)
        row["enum_match_fields"] = ", ".join(known_enum_matches.keys())
        for rate_column in [
            "null_rate",
            "unique_rate",
            "date_parse_rate",
            "email_pattern_rate",
            "phone_pattern_rate",
            "numeric_parse_rate",
        ]:
            row[f"{rate_column}_pct"] = round(float(row.get(rate_column) or 0) * 100, 1)
        row["text_value_rate_pct"] = 100.0 if row.get("inferred_type") == "text" else 0.0
        rows.append(row)
    return pd.DataFrame(rows)[columns] if rows else pd.DataFrame(columns=columns)


def current_source_coverage(
    source_df: pd.DataFrame | None, mappings: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    if source_df is None or mappings is None:
        return []
    profiles = st.session_state.profiles or profile_dataframe(source_df)
    return build_source_coverage(list(source_df.columns), profiles, mappings)


def render_source_coverage_review(source_df: pd.DataFrame, mappings: list[dict[str, Any]]) -> None:
    coverage_rows = current_source_coverage(source_df, mappings)
    summary = source_coverage_summary(coverage_rows)
    show_metric_row(
        {
            "Source columns": summary["source_columns"],
            "Approved mapped": summary["approved_mapped_columns"],
            "Suggested only": summary["suggested_only_columns"],
            "Unused": summary["unused_columns"],
            "Unused needing review": summary["unused_columns_requiring_review"],
        }
    )
    st.dataframe(pd.DataFrame(coverage_rows), use_container_width=True, hide_index=True, height=300)
    unused_columns = unused_source_columns(coverage_rows)
    if unused_columns:
        st.warning("Unused source columns need reviewer acceptance: " + ", ".join(unused_columns))
        st.checkbox(
            "I reviewed the unused source columns and accept excluding them from the target outputs.",
            key="source_coverage_reviewed",
        )
    else:
        st.session_state.source_coverage_reviewed = True
        st.success("All source columns are used by at least one mapping suggestion or approval.")


def save_current_signoff() -> None:
    reviewer_name = str(st.session_state.get("reviewer_name") or "").strip()
    if not reviewer_name:
        st.error("Reviewer name is required before signoff can be saved.")
        return
    contract = current_contract()
    st.session_state.signoff = {
        "reviewer_name": reviewer_name,
        "reviewer_role": str(st.session_state.get("reviewer_role") or "").strip(),
        "decision": st.session_state.get("signoff_decision"),
        "comment": str(st.session_state.get("signoff_comment") or "").strip(),
        "signed_off_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_schema_name": contract.name,
        "target_schema_version": contract.version,
    }
    st.success("Reviewer signoff saved.")


def render_reviewer_signoff() -> None:
    st.write("Reviewer Signoff")
    st.text_input("Reviewer name", key="reviewer_name")
    st.text_input("Reviewer role/team", key="reviewer_role")
    st.selectbox("Signoff decision", SIGNOFF_DECISIONS, key="signoff_decision")
    st.text_area("Signoff comment", key="signoff_comment")
    st.button("Save reviewer signoff", on_click=save_current_signoff, use_container_width=True)
    if st.session_state.signoff:
        st.success(
            "Saved signoff: "
            + st.session_state.signoff["decision"]
            + " by "
            + st.session_state.signoff["reviewer_name"]
        )


def render_import_replay_check(source_df: pd.DataFrame | None, ok: bool) -> dict[str, Any]:
    st.write("Import Replay / Idempotency Check")
    if source_df is None:
        st.info("Load a source file before replay checks can run.")
        return {}

    source_file_hash = source_dataframe_fingerprint(source_df)
    st.caption(f"Source file fingerprint: {source_file_hash[:12]}")
    if not ok:
        st.info("Connect to PostgreSQL to check prior import runs for this source file.")
        return {
            "source_file_hash": source_file_hash,
            "source_file_hash_short": source_file_hash[:12],
            "is_replay": False,
        }

    try:
        engine = get_engine()
        init_db(engine)
        check = build_import_replay_check(engine, source_file_hash)
    except Exception as exc:
        st.warning(f"Replay check could not run: {exc}")
        return {
            "source_file_hash": source_file_hash,
            "source_file_hash_short": source_file_hash[:12],
            "is_replay": False,
        }

    if check["is_replay"]:
        st.warning(check["message"])
        st.caption(
            "Rerun behavior: this publish creates a new import_run audit record. "
            "Canonical members, plans, and coverage rows are upserted by stable keys."
        )
        st.checkbox(
            "I understand this is a replay of a previously published source file and want to continue.",
            key="import_replay_acknowledged",
        )
    else:
        st.success(check["message"])
    return check


def expand_ai_mappings(
    ai_mappings: list[dict[str, Any]], fallback_mappings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    ai_by_key = {(mapping.get("target_table"), mapping.get("target_field")): mapping for mapping in ai_mappings}
    expanded = []
    for fallback in fallback_mappings:
        key = (fallback.get("target_table"), fallback.get("target_field"))
        if key in ai_by_key:
            merged = dict(fallback)
            merged.update(ai_by_key[key])
            merged["required"] = fallback.get("required", False)
            merged["approved"] = False
            expanded.append(merged)
        else:
            expanded.append(fallback)
    return expanded


def render_transformation_builder(
    source_df: pd.DataFrame,
    mappings: list[dict[str, Any]],
    contract: ContractVersion,
) -> None:
    st.write("Transformation Rule Builder")
    targets_by_key = target_fields_by_key(contract.target_fields)
    labels = {
        f"{mapping.get('target_table')}.{mapping.get('target_field')}": index
        for index, mapping in enumerate(mappings)
        if mapping.get("source_column") or mapping.get("source_columns")
    }
    if not labels:
        st.info("Select source columns in the mapping review before configuring transformations.")
        return

    selected_label = st.selectbox("Target field", list(labels), key="transformation_target")
    mapping_index = labels[selected_label]
    mapping = dict(mappings[mapping_index])
    target_key = (str(mapping.get("target_table") or ""), str(mapping.get("target_field") or ""))
    target = targets_by_key[target_key]
    default_sources = mapping.get("source_columns") or [mapping.get("source_column")]
    selected_sources = st.multiselect(
        "Source columns",
        options=list(source_df.columns),
        default=[value for value in default_sources if value in source_df.columns],
        key=f"transform_sources_{mapping_index}",
    )

    step_rows = [
        {
            "operation": step.get("operation") or "trim",
            "parameters_json": json.dumps(step.get("parameters") or {}, ensure_ascii=True),
        }
        for step in mapping.get("transformation_steps") or []
    ]
    edited_steps = st.data_editor(
        pd.DataFrame(step_rows, columns=["operation", "parameters_json"]),
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        key=f"transform_steps_{mapping_index}",
        column_config={
            "operation": st.column_config.SelectboxColumn(
                "operation",
                options=sorted(APPROVED_OPERATIONS),
                required=True,
            ),
            "parameters_json": st.column_config.TextColumn("parameters", width="large"),
        },
    )
    failure_policy = st.selectbox(
        "Failure policy",
        options=sorted(FAILURE_POLICIES),
        index=sorted(FAILURE_POLICIES).index(str(mapping.get("failure_policy") or "error")),
        key=f"transform_failure_{mapping_index}",
    )
    failure_default_text = ""
    if failure_policy == "use_default":
        failure_default_text = st.text_input(
            "Failure default",
            value=(
                json.dumps(mapping.get("failure_default"), ensure_ascii=True)
                if "failure_default" in mapping
                else "null"
            ),
            key=f"transform_default_{mapping_index}",
        )

    pipeline_errors: list[str] = []
    parsed_steps: list[dict[str, Any]] = []
    for row_number, step_row in enumerate(edited_steps.to_dict("records"), start=1):
        operation = str(step_row.get("operation") or "").strip()
        if not operation:
            continue
        try:
            parameters = json.loads(str(step_row.get("parameters_json") or "{}"))
        except json.JSONDecodeError as exc:
            pipeline_errors.append(f"Step {row_number} parameters are not valid JSON: {exc.msg}.")
            continue
        parsed_steps.append({"operation": operation, "parameters": parameters})

    candidate = {
        **mapping,
        "source_column": selected_sources[0] if selected_sources else "",
        "source_columns": selected_sources,
        "transformation_steps": parsed_steps,
        "failure_policy": failure_policy,
    }
    if failure_policy == "use_default":
        try:
            candidate["failure_default"] = json.loads(failure_default_text)
        except json.JSONDecodeError:
            candidate["failure_default"] = failure_default_text
    pipeline_errors.extend(
        validate_transformation_pipeline(candidate, target, set(str(column) for column in source_df.columns))
    )
    if pipeline_errors:
        st.error(" ".join(pipeline_errors))

    recommended_col, preview_col, save_col = st.columns(3)
    if recommended_col.button("Use recommended rules", use_container_width=True, key=f"recommend_{mapping_index}"):
        mappings[mapping_index]["transformation_steps"] = recommended_steps(target)
        mappings[mapping_index]["transformation_approved"] = False
        st.session_state.mappings = mappings
        invalidate_after_mapping_change()
        st.rerun()
    if preview_col.button(
        "Preview transformation",
        use_container_width=True,
        disabled=bool(pipeline_errors),
        key=f"preview_{mapping_index}",
    ):
        st.session_state[f"transform_preview_{mapping_index}"] = preview_transformation_pipeline(
            source_df,
            candidate,
            target_field=target,
            limit=10,
        )
    if save_col.button(
        "Approve transformation",
        use_container_width=True,
        disabled=bool(pipeline_errors),
        key=f"save_transform_{mapping_index}",
    ):
        candidate["transformation_approved"] = True
        mappings[mapping_index] = candidate
        st.session_state.mappings = mappings
        invalidate_after_mapping_change()
        st.success(f"Approved transformation for {selected_label}.")

    preview = st.session_state.get(f"transform_preview_{mapping_index}")
    if preview is not None:
        st.dataframe(preview, use_container_width=True, hide_index=True)


def unapproved_transformation_targets(mappings: list[dict[str, Any]]) -> list[str]:
    return [
        f"{mapping.get('target_table')}.{mapping.get('target_field')}"
        for mapping in mappings
        if mapping.get("approved")
        and (mapping.get("source_column") or mapping.get("source_columns"))
        and not bool(mapping.get("transformation_approved", False))
    ]


def render_reconciliation_result(result: Any) -> None:
    if result is None:
        return
    status_message = f"Reconciliation status: {result.status}"
    if result.status == "FAIL":
        st.error(status_message)
    elif result.status == "WARNING":
        st.warning(status_message)
    else:
        st.success(status_message)
    show_metric_row(
        {
            "Source": result.source_metrics.get("source_rows", 0),
            "Accepted": result.source_metrics.get("accepted_rows", 0),
            "Rejected": result.source_metrics.get("rejected_rows", 0),
            "Reject rate": f"{float(result.source_metrics.get('reject_rate', 0)) * 100:.1f}%",
        }
    )
    table_names = list(result.table_metrics)
    if table_names:
        selected_table = st.selectbox("Reconciliation target table", table_names, key=f"recon_{result.stage}")
        st.dataframe(
            pd.DataFrame([{"target_table": selected_table, **result.table_metrics[selected_table]}]),
            use_container_width=True,
            hide_index=True,
        )
    st.dataframe(
        pd.DataFrame([check.__dict__ for check in result.checks]),
        use_container_width=True,
        hide_index=True,
        height=280,
    )


def process_correction_overlays(overlays: list[dict[str, Any]], corrected_by: str) -> None:
    source_df = st.session_state.source_df
    mappings = st.session_state.mappings or []
    if source_df is None or not mappings:
        st.error("Load and map the original source before applying corrections.")
        return
    contract = current_contract()
    source_hash = source_dataframe_fingerprint(source_df)
    corrected_source = apply_correction_overlays(source_df, source_hash, overlays)
    flat = build_canonical_flat(
        corrected_source,
        mappings,
        contract.target_fields,
        source_file_hash=source_hash,
    )
    full_result = validate_canonical_frame(
        flat,
        approved_source_columns_by_target_field(mappings),
        contract.target_fields,
    )
    selected_rows = {int(overlay["source_row_number"]) for overlay in overlays}
    selected_normalized = full_result.normalized_df[
        full_result.normalized_df["source_row_number"].isin(selected_rows)
    ].copy()
    selected_normalized.attrs.update(full_result.normalized_df.attrs)
    selected_issues = [issue for issue in full_result.issues if issue.source_row_number in selected_rows]
    recovery_result = ValidationResult(normalized_df=selected_normalized, issues=selected_issues)
    audit_rows = correction_audit_rows(overlays, source_df, corrected_by=corrected_by)
    rejected_after_correction = recovery_result.error_row_numbers
    for audit_row in audit_rows:
        audit_row["correction_status"] = (
            "still_rejected" if int(audit_row["source_row_number"]) in rejected_after_correction else "recovered"
        )
    recovery_outputs = transform_outputs(
        recovery_result.normalized_df,
        corrected_source,
        recovery_result.issues_df,
        mappings,
        target_schema=contract.target_fields,
        contract=contract,
        source_file_hash=source_hash,
        original_source_df=source_df,
        correction_audit=audit_rows,
        mapping_template_version=str(st.session_state.mapping_template_version),
        parent_import_run_id=st.session_state.import_run_id,
    )
    st.session_state.correction_overlays = overlays
    st.session_state.correction_audit = audit_rows
    st.session_state.corrected_source_df = corrected_source
    st.session_state.recovery_validation_result = recovery_result
    st.session_state.recovery_outputs = recovery_outputs
    st.session_state.parent_import_run_id = st.session_state.import_run_id
    st.session_state.run_kind = "row_correction"
    st.session_state.pre_reconciliation = build_transform_reconciliation(
        recovery_result,
        recovery_outputs,
        contract=contract,
        acknowledged_reject_count=len(st.session_state.acknowledged_rejects),
    )
    st.session_state.post_reconciliation = None
    st.session_state.published = False
    st.success(
        f"Correction run prepared: {recovery_result.accepted_row_count} recovered, "
        f"{recovery_result.rejected_row_count} still rejected."
    )


def render_correction_workspace(result: ValidationResult, outputs: Any) -> None:
    if result.rejected_row_count == 0 or outputs.correction_work_queue.empty:
        st.success("No rejected rows require correction.")
        return
    queue = outputs.correction_work_queue
    st.write("Rejected-Row Resolution")
    mode = st.segmented_control(
        "Resolution mode",
        ["Inline correction", "Correction file", "Acknowledge reject"],
        default="Inline correction",
        key="correction_mode",
    )
    if mode == "Inline correction":
        issue_codes = sorted(
            {
                code.strip()
                for value in queue["error_codes"].fillna("").astype(str)
                for code in value.split(";")
                if code.strip()
            }
        )
        selected_issue = st.selectbox("Issue filter", ["All issues"] + issue_codes)
        filtered = queue
        if selected_issue != "All issues":
            filtered = queue[queue["error_codes"].astype(str).str.contains(selected_issue, regex=False)]
        selected_ids = st.multiselect(
            "Rejected records",
            options=filtered["source_record_id"].astype(str).tolist(),
            format_func=lambda value: f"{value} (row {int(queue.loc[queue['source_record_id'].eq(value), 'source_row_number'].iloc[0])})",
        )
        if selected_ids:
            selected = queue[queue["source_record_id"].isin(selected_ids)].copy()
            error_source_columns = sorted(
                {
                    column.strip()
                    for value in selected["error_source_columns"].fillna("").astype(str)
                    for column in value.split(";")
                    if column.strip()
                }
            )
            edit_columns = [
                "source_record_id",
                "source_row_number",
                "original_row_fingerprint",
                "error_codes",
                *[f"corrected__{column}" for column in error_source_columns],
                "correction_comment",
            ]
            edited = st.data_editor(
                selected[edit_columns],
                use_container_width=True,
                hide_index=True,
                disabled=[
                    "source_record_id",
                    "source_row_number",
                    "original_row_fingerprint",
                    "error_codes",
                ],
                key="inline_correction_editor",
            )
            corrected_by = st.text_input("Corrected by", key="inline_corrected_by")
            if st.button("Revalidate selected corrections", use_container_width=True):
                upload_result = validate_correction_upload(edited, queue)
                if upload_result.errors:
                    st.error(" ".join(upload_result.errors))
                elif not corrected_by.strip():
                    st.error("Corrected by is required.")
                else:
                    process_correction_overlays(upload_result.overlays, corrected_by.strip())
    elif mode == "Correction file":
        st.download_button(
            "Download correction work queue",
            data=dataframe_to_csv_bytes(queue),
            file_name="rejected_rows_for_correction.csv",
            mime="text/csv",
            use_container_width=True,
        )
        uploaded_corrections = st.file_uploader("Upload corrected rejected rows", type=["csv"])
        corrected_by = st.text_input("Corrected by", key="file_corrected_by")
        if uploaded_corrections is not None:
            uploaded_df = pd.read_csv(uploaded_corrections)
            upload_result = validate_correction_upload(uploaded_df, queue)
            if upload_result.errors:
                st.error(" ".join(upload_result.errors))
            else:
                st.success(f"Validated {len(upload_result.overlays)} corrected records.")
                if st.button("Create correction run", use_container_width=True):
                    if not corrected_by.strip():
                        st.error("Corrected by is required.")
                    else:
                        process_correction_overlays(upload_result.overlays, corrected_by.strip())
    else:
        selected_ids = st.multiselect(
            "Rejected records to acknowledge",
            options=queue["source_record_id"].astype(str).tolist(),
            key="acknowledged_reject_ids",
        )
        comment = st.text_area("Acknowledgement comment", key="acknowledged_reject_comment")
        if st.button("Acknowledge unresolved rejects", use_container_width=True):
            if not selected_ids or not comment.strip():
                st.error("Select at least one rejected record and provide a comment.")
            else:
                acknowledged = dict(st.session_state.acknowledged_rejects)
                for record_id in selected_ids:
                    acknowledged[record_id] = comment.strip()
                st.session_state.acknowledged_rejects = acknowledged
                st.success(f"Acknowledged {len(selected_ids)} rejected records. They remain unpublished.")

    recovery_result = st.session_state.recovery_validation_result
    if recovery_result is not None:
        show_metric_row(
            {
                "Recovered": recovery_result.accepted_row_count,
                "Still rejected": recovery_result.rejected_row_count,
                "Corrected fields": len(st.session_state.correction_audit),
            }
        )


def show_metric_row(values: dict[str, Any]) -> None:
    columns = st.columns(len(values))
    for column, (label, value) in zip(columns, values.items(), strict=False):
        column.metric(label, value)


def main() -> None:
    st.set_page_config(page_title="Customer Data Onboarding Copilot", layout="wide")
    init_state()
    if not st.session_state.contract_registry_loaded:
        refresh_contract_registry()

    st.title("Customer Data Onboarding Copilot")
    selected_contract = current_contract()
    st.caption(
        f"Target: {selected_contract.name} {selected_contract.version} | "
        f"Contract: {selected_contract.contract_key} | {selected_contract.status}"
    )
    st.markdown(
        """
        <style>
          [data-testid="stMetricLabel"] { font-size: 0.78rem; }
          [data-testid="stMetricValue"] { font-size: 1.35rem; }
          div[data-testid="stDataFrame"] { font-size: 0.86rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    active_step = st.radio(
        "Workflow step",
        WORKFLOW_STEPS,
        horizontal=True,
        label_visibility="collapsed",
        key="active_step",
    )
    st.divider()

    if active_step == "Target":
        st.subheader("Target Schema")
        registry = st.session_state.contract_registry or [FALLBACK_CONTRACT]
        published_contracts = [contract for contract in registry if contract.status == "published"]
        if not published_contracts:
            published_contracts = [FALLBACK_CONTRACT]
        contract_labels = {f"{contract.name} | {contract.version}": contract for contract in published_contracts}
        current_label = next(
            (
                label
                for label, contract in contract_labels.items()
                if contract.contract_key == selected_contract.contract_key
                and contract.version == selected_contract.version
            ),
            next(iter(contract_labels)),
        )
        selector_col, refresh_col = st.columns([2, 1])
        selected_label = selector_col.selectbox(
            "Target contract",
            options=list(contract_labels),
            index=list(contract_labels).index(current_label),
        )
        set_selected_contract(contract_labels[selected_label])
        selected_contract = current_contract()
        selected_schema = selected_contract.target_fields
        if refresh_col.button("Refresh contracts", use_container_width=True):
            refresh_contract_registry()
            st.rerun()
        if st.session_state.contract_registry_error:
            st.info("PostgreSQL contract registry is unavailable; the built-in contract remains available.")

        schema_df = target_schema_rows(selected_contract)
        table_names = list(dict.fromkeys(schema_df["target_table"].tolist()))
        table_filter_col, _ = st.columns([1, 2])
        selected_target_table = table_filter_col.selectbox(
            "Output table",
            options=table_names + ["All output tables"],
            key="target_table_filter",
        )
        if selected_target_table == "All output tables":
            displayed_fields = selected_schema
            displayed_schema_df = schema_df
        else:
            displayed_fields = [field for field in selected_schema if field.table == selected_target_table]
            displayed_schema_df = schema_df[schema_df["target_table"].eq(selected_target_table)].reset_index(drop=True)

        required_count = sum(1 for field in displayed_fields if field.required and not field.generated)
        generated_count = sum(1 for field in displayed_fields if field.generated)
        show_metric_row(
            {
                "Tables": displayed_schema_df["target_table"].nunique(),
                "Fields": len(displayed_schema_df),
                "Required fields": required_count,
                "Generated fields": generated_count,
            }
        )
        st.dataframe(
            displayed_schema_df,
            use_container_width=True,
            hide_index=True,
            height=430,
            row_height=30,
            column_config={
                "target_table": st.column_config.TextColumn("table", width="medium"),
                "target_field": st.column_config.TextColumn("field", width="medium"),
                "required": st.column_config.TextColumn("required", width="small"),
                "target_data_type": st.column_config.TextColumn("type", width="small"),
                "nullable": st.column_config.CheckboxColumn("nullable", width="small"),
                "validation_kind": st.column_config.TextColumn("validation", width="medium"),
                "allowed_values": st.column_config.TextColumn("allowed values", width="medium"),
                "description": st.column_config.TextColumn("description", width="large"),
            },
        )

        with st.expander("Contract Registry", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "contract_key": contract.contract_key,
                            "name": contract.name,
                            "domain": contract.domain,
                            "version": contract.version,
                            "status": contract.status,
                            "checksum": contract.checksum[:12],
                        }
                        for contract in registry
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            st.download_button(
                "Export selected contract JSON",
                data=contract_json_bytes(selected_contract),
                file_name=f"{selected_contract.contract_key}__{selected_contract.version}.json",
                mime="application/json",
                use_container_width=True,
            )
            contract_upload = st.file_uploader("Import target contract JSON", type=["json"])
            contract_actor = st.text_input("Contract owner", key="contract_actor")
            contract_comment = st.text_area("Lifecycle comment", key="contract_comment")
            if st.button("Validate and import draft", use_container_width=True, disabled=contract_upload is None):
                try:
                    definition = json.loads(contract_upload.getvalue().decode("utf-8"))
                    engine = get_engine()
                    imported = save_contract_version(
                        engine,
                        definition,
                        actor=contract_actor.strip() or "schema owner",
                        comment=contract_comment.strip(),
                        status="draft",
                    )
                    refresh_contract_registry()
                    st.success(f"Imported draft {imported.contract_key} {imported.version}.")
                except (
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    ContractValidationError,
                    ContractRegistryError,
                ) as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.error(f"Contract import failed: {exc}")

            lifecycle_labels = {
                f"{contract.name} | {contract.version} | {contract.status}": contract for contract in registry
            }
            lifecycle_label = st.selectbox("Manage contract version", list(lifecycle_labels))
            lifecycle_contract = lifecycle_labels[lifecycle_label]
            publish_col, retire_col = st.columns(2)
            if publish_col.button(
                "Publish draft",
                use_container_width=True,
                disabled=lifecycle_contract.status != "draft",
            ):
                try:
                    transition_contract_status(
                        get_engine(),
                        lifecycle_contract,
                        new_status="published",
                        actor=contract_actor.strip() or "schema owner",
                        comment=contract_comment.strip(),
                    )
                    refresh_contract_registry()
                    st.rerun()
                except Exception as exc:
                    st.error(f"Contract publish failed: {exc}")
            if retire_col.button(
                "Retire version",
                use_container_width=True,
                disabled=lifecycle_contract.status != "published",
            ):
                try:
                    transition_contract_status(
                        get_engine(),
                        lifecycle_contract,
                        new_status="retired",
                        actor=contract_actor.strip() or "schema owner",
                        comment=contract_comment.strip(),
                    )
                    refresh_contract_registry()
                    if (
                        lifecycle_contract.contract_key == selected_contract.contract_key
                        and lifecycle_contract.version == selected_contract.version
                    ):
                        st.session_state.contract = FALLBACK_CONTRACT
                    st.rerun()
                except Exception as exc:
                    st.error(f"Contract retirement failed: {exc}")

    if active_step == "Upload":
        st.subheader("Upload Source File")
        st.caption(f"Source columns will be mapped into {current_contract().name} {current_contract().version}.")
        uploaded = st.file_uploader("Upload a CSV eligibility file", type=["csv"])
        col_a, col_b = st.columns([1, 3])
        if col_a.button("Load demo file", use_container_width=True):
            if DEMO_FILE.exists():
                load_source_df(pd.read_csv(DEMO_FILE), DEMO_FILE.name)
            else:
                st.error("Demo file is missing. Run scripts/generate_demo_eligibility_file.py first.")
        if uploaded is not None:
            load_source_df(pd.read_csv(uploaded), uploaded.name)

        source_df = st.session_state.source_df
        if source_df is not None:
            show_metric_row(
                {"Rows": len(source_df), "Columns": len(source_df.columns), "File": st.session_state.file_name}
            )
            st.dataframe(source_df.head(25), use_container_width=True)

    if active_step == "Profile":
        st.subheader("Source Profile")
        source_df = st.session_state.source_df
        if source_df is None:
            st.info("Load a CSV first.")
        else:
            if st.button("Profile source columns", use_container_width=True):
                st.session_state.profiles = profile_dataframe(source_df)
            if st.session_state.profiles is None:
                st.session_state.profiles = profile_dataframe(source_df)
            profiles = st.session_state.profiles
            table_view = st.selectbox("Profile table view", list(PROFILE_TABLE_VIEWS.keys()))
            st.dataframe(
                profile_display_rows(profiles, PROFILE_TABLE_VIEWS[table_view]),
                use_container_width=True,
                hide_index=True,
                height=410,
                row_height=30,
                column_config=PROFILE_COLUMN_CONFIG,
            )
            selected_column = st.selectbox("Inspect column", [profile["column_name"] for profile in profiles])
            selected_profile = next(profile for profile in profiles if profile["column_name"] == selected_column)
            with st.expander("Column samples and diagnostics", expanded=True):
                st.json(
                    {
                        "sample_values": selected_profile["sample_values"],
                        "top_values": selected_profile["top_values"],
                        "known_enum_matches": selected_profile["known_enum_matches"],
                        "min_date": selected_profile["min_date"],
                        "max_date": selected_profile["max_date"],
                    }
                )

    if active_step == "Map":
        st.subheader("Mapping Review")
        contract = current_contract()
        target_schema = contract.target_fields
        st.caption(f"Target contract: {contract.name} {contract.version}")
        source_df = st.session_state.source_df
        if source_df is None:
            st.info("Load a CSV first.")
        else:
            if st.session_state.profiles is None:
                st.session_state.profiles = profile_dataframe(source_df)
            st.session_state.mapping_mode = st.radio(
                "Mapping mode",
                ["Rules-Based", "AI-Assisted"],
                horizontal=True,
                index=0 if st.session_state.mapping_mode == "Rules-Based" else 1,
            )

            with st.expander("Mapping Templates", expanded=False):
                templates = [
                    template
                    for template in list_mapping_templates()
                    if template.get("schema_version") == contract.version
                    and template.get("contract_key") in {"", contract.contract_key}
                    and template.get("contract_checksum") in {"", contract.checksum}
                ]
                if templates:
                    labels = {
                        f"{template['template_name']} v{template['template_version']} ({template['saved_at']})": template
                        for template in templates
                    }
                    selected_label = st.selectbox("Load saved template", list(labels.keys()))
                    if st.button("Load mapping template", use_container_width=True):
                        try:
                            template = load_mapping_template(labels[selected_label]["file_name"])
                            loaded_mappings = apply_mapping_template(
                                template,
                                list(source_df.columns),
                                contract_key=contract.contract_key,
                                schema_version=contract.version,
                                contract_checksum=contract.checksum,
                            )
                            st.session_state.mappings = apply_mapping_type_alignment(
                                loaded_mappings,
                                st.session_state.profiles or [],
                                target_schema,
                            )
                            st.session_state.mapping_template_name = str(template.get("template_name") or "")
                            st.session_state.mapping_template_version = int(template.get("template_version") or 1)
                            st.session_state.source_coverage_reviewed = False
                            invalidate_after_mapping_change()
                            st.success(f"Loaded template: {st.session_state.mapping_template_name}")
                        except MappingTemplateCompatibilityError as exc:
                            st.error(str(exc))
                else:
                    st.info("No saved templates for this target schema version yet.")

            if st.button("Generate mapping suggestions", use_container_width=True):
                profiles = st.session_state.profiles
                rules_mappings = apply_mapping_type_alignment(
                    generate_rules_based_mappings(profiles, target_schema),
                    profiles,
                    target_schema,
                )
                invalidate_after_mapping_change()
                st.session_state.source_coverage_reviewed = False
                st.session_state.mapping_template_name = ""
                st.session_state.mapping_template_version = 1
                if st.session_state.mapping_mode == "Rules-Based":
                    st.session_state.mappings = rules_mappings
                else:
                    try:
                        ai_mappings = suggest_mappings_with_ai(
                            profiles,
                            rules_mappings,
                            target_schema=target_schema,
                        )
                        st.session_state.mappings = apply_mapping_type_alignment(
                            expand_ai_mappings(ai_mappings, rules_mappings),
                            profiles,
                            target_schema,
                        )
                    except OpenAIConfigurationError as exc:
                        st.error(str(exc))
                        st.session_state.mappings = rules_mappings
                    except AIMapperValidationError as exc:
                        st.error(f"AI mapping response failed validation: {exc}")
                        st.session_state.mappings = rules_mappings
                    except Exception as exc:
                        st.error(f"AI mapping failed: {exc}")
                        st.session_state.mappings = rules_mappings

            if st.session_state.mappings is not None:
                if st.button("Approve all suggested mappings with source columns", use_container_width=True):
                    for mapping in st.session_state.mappings:
                        if mapping.get("source_column"):
                            mapping["approved"] = True
                            mapping["transformation_approved"] = True

                editor_df = mapping_editor_rows(st.session_state.mappings)
                edited = st.data_editor(
                    editor_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "source_column": st.column_config.SelectboxColumn(
                            "source_column",
                            options=[""] + list(source_df.columns),
                        ),
                        "approved": st.column_config.CheckboxColumn("approved"),
                        "confidence": st.column_config.ProgressColumn(
                            "confidence",
                            min_value=0,
                            max_value=100,
                            format="%d",
                        ),
                    },
                    column_order=[
                        "target_table",
                        "target_field",
                        "target_data_type",
                        "source_column",
                        "source_inferred_type",
                        "type_alignment",
                        "confidence",
                        "approved",
                        "transformation_approved",
                        "needs_review",
                        "reason",
                    ],
                    disabled=[
                        "target_table",
                        "target_field",
                        "required",
                        "target_data_type",
                        "target_validation_kind",
                        "source_inferred_type",
                        "type_alignment",
                        "type_alignment_score",
                        "type_alignment_reason",
                        "confidence",
                        "mapping_status",
                        "needs_review",
                        "review_flags",
                        "reason",
                        "review_reason",
                        "score_breakdown",
                        "transformation_approved",
                    ],
                )
                previous_checksum = mapping_configuration_checksum(st.session_state.mappings)
                updated_mappings = apply_mapping_type_alignment(
                    rows_to_mappings(edited),
                    st.session_state.profiles or [],
                    target_schema,
                )
                st.session_state.mappings = updated_mappings
                if (
                    mapping_configuration_checksum(updated_mappings) != previous_checksum
                    and st.session_state.validation_result
                ):
                    invalidate_after_mapping_change()

                gaps = required_mapping_gaps(st.session_state.mappings, target_schema)
                alignment_issues = blocking_mapping_alignment_issues(st.session_state.mappings)
                transformation_gaps = unapproved_transformation_targets(st.session_state.mappings)
                if gaps:
                    st.warning("Required fields still need approved mappings: " + ", ".join(gaps))
                elif alignment_issues:
                    st.error("Approved mappings have target/source type mismatches: " + "; ".join(alignment_issues))
                elif transformation_gaps:
                    st.warning(
                        "Approved mappings still need transformation approval: " + ", ".join(transformation_gaps)
                    )
                else:
                    st.success("Required mappings and transformation pipelines are approved.")

                with st.expander("Transformation Rules", expanded=True):
                    render_transformation_builder(source_df, st.session_state.mappings, contract)

                st.write("Source Coverage")
                render_source_coverage_review(source_df, st.session_state.mappings)

                with st.expander("Save Mapping Template", expanded=False):
                    default_template_name = (
                        st.session_state.mapping_template_name or f"{st.session_state.file_name or 'source'} template"
                    )
                    template_name = st.text_input("Template name", value=default_template_name)
                    if st.button("Save current mappings as template", use_container_width=True):
                        if not template_name.strip():
                            st.error("Template name is required.")
                        else:
                            saved_template = save_mapping_template(
                                template_name=template_name,
                                schema_name=contract.name,
                                schema_version=contract.version,
                                source_columns=list(source_df.columns),
                                mappings=st.session_state.mappings,
                                contract_key=contract.contract_key,
                                contract_checksum=contract.checksum,
                            )
                            st.session_state.mapping_template_name = saved_template["template_name"]
                            st.session_state.mapping_template_version = int(saved_template["template_version"])
                            st.success(
                                f"Saved template: {saved_template['template_name']} "
                                f"v{saved_template['template_version']}"
                            )

    if active_step == "Validate":
        st.subheader("Validation")
        contract = current_contract()
        target_schema = contract.target_fields
        source_df = st.session_state.source_df
        mappings = st.session_state.mappings
        if source_df is None or mappings is None:
            st.info("Load a file and approve mappings first.")
        else:
            mappings = apply_mapping_type_alignment(mappings, st.session_state.profiles or [], target_schema)
            st.session_state.mappings = mappings
            gaps = required_mapping_gaps(mappings, target_schema)
            alignment_issues = blocking_mapping_alignment_issues(mappings)
            transformation_gaps = unapproved_transformation_targets(mappings)
            coverage_rows = current_source_coverage(source_df, mappings)
            unused_columns = unused_source_columns(coverage_rows)
            if gaps:
                st.warning("Approve required mappings before validation: " + ", ".join(gaps))
            elif alignment_issues:
                st.warning("Fix approved mapping type mismatches before validation: " + "; ".join(alignment_issues))
            elif transformation_gaps:
                st.warning("Approve transformations before validation: " + ", ".join(transformation_gaps))
            elif unused_columns and not st.session_state.source_coverage_reviewed:
                st.warning("Review and accept unused source columns before validation: " + ", ".join(unused_columns))
            elif st.button("Run validation", use_container_width=True):
                source_hash = source_dataframe_fingerprint(source_df)
                flat = build_canonical_flat(
                    source_df,
                    mappings,
                    target_schema,
                    source_file_hash=source_hash,
                )
                result = validate_canonical_frame(
                    flat,
                    approved_source_columns_by_target_field(mappings),
                    target_schema,
                )
                outputs = transform_outputs(
                    result.normalized_df,
                    source_df,
                    result.issues_df,
                    mappings,
                    target_schema=target_schema,
                    contract=contract,
                    source_file_hash=source_hash,
                    mapping_template_version=str(st.session_state.mapping_template_version),
                    parent_import_run_id=st.session_state.parent_import_run_id,
                )
                st.session_state.validation_result = result
                st.session_state.outputs = outputs
                st.session_state.validated_mapping_checksum = mapping_configuration_checksum(mappings)
                st.session_state.pre_reconciliation = build_transform_reconciliation(
                    result,
                    outputs,
                    contract=contract,
                    acknowledged_reject_count=len(st.session_state.acknowledged_rejects),
                )
                st.session_state.post_reconciliation = None
                st.session_state.corrected_source_df = None
                st.session_state.recovery_validation_result = None
                st.session_state.recovery_outputs = None
                st.session_state.correction_overlays = []
                st.session_state.correction_audit = []
                st.session_state.published = False

            result = st.session_state.validation_result
            if result is not None:
                show_metric_row(
                    {
                        "Accepted rows": result.accepted_row_count,
                        "Rejected rows": result.rejected_row_count,
                        "Warnings": result.warning_count,
                    }
                )
                issues_df = result.issues_df
                st.dataframe(issue_summary(issues_df), use_container_width=True)
                if not issues_df.empty:
                    st.dataframe(issues_df, use_container_width=True)
                if st.session_state.outputs is not None:
                    render_correction_workspace(result, st.session_state.outputs)

    if active_step == "Transform":
        st.subheader("Transform")
        contract = current_contract()
        source_df = active_source_df()
        result = active_validation_result()
        if source_df is None or result is None:
            st.info("Run validation first.")
        else:
            if st.button("Rebuild transformed outputs", use_container_width=True):
                st.session_state.outputs = transform_outputs(
                    result.normalized_df,
                    source_df,
                    result.issues_df,
                    st.session_state.mappings or [],
                    target_schema=contract.target_fields,
                    contract=contract,
                    source_file_hash=source_dataframe_fingerprint(st.session_state.source_df),
                    original_source_df=(
                        st.session_state.source_df if st.session_state.corrected_source_df is not None else None
                    ),
                    correction_audit=st.session_state.correction_audit,
                    mapping_template_version=str(st.session_state.mapping_template_version),
                    parent_import_run_id=st.session_state.parent_import_run_id,
                )
                if st.session_state.recovery_validation_result is not None:
                    st.session_state.recovery_outputs = st.session_state.outputs
            outputs = active_outputs()
            if outputs is not None:
                output_tables = {
                    **outputs.tables,
                    "rejected_rows_with_original_values": outputs.rejected_rows,
                    "rejected_rows_for_correction": outputs.correction_work_queue,
                    "field_lineage": outputs.field_lineage,
                }
                show_metric_row(
                    {
                        "Output tables": len(outputs.tables),
                        "Canonical records": sum(len(table) for table in outputs.tables.values()),
                        "Rejected rows": len(outputs.rejected_rows),
                        "Lineage rows": len(outputs.field_lineage),
                    }
                )
                selected_output_table = st.selectbox(
                    "Output table",
                    options=list(output_tables.keys()),
                    key="transform_output_table",
                )
                selected_output_df = output_tables[selected_output_table]
                st.dataframe(selected_output_df.head(50), use_container_width=True, hide_index=True)

    if active_step == "Publish":
        st.subheader("Publish To PostgreSQL")
        contract = current_contract()
        outputs = active_outputs()
        result = active_validation_result()
        ok, message = connection_status()
        st.info(message if ok else f"Not connected: {message}")

        if outputs is None or result is None:
            st.info("Transform accepted rows before publishing.")
        else:
            mapping_is_current = st.session_state.validated_mapping_checksum == mapping_configuration_checksum(
                st.session_state.mappings or []
            )
            if not mapping_is_current:
                st.error("Mappings or transformations changed after validation. Run the complete validation again.")
            if ok:
                try:
                    engine = get_engine()
                    init_db(engine)
                    ensure_contract_target_tables(engine, contract)
                    pre_reconciliation = build_pre_publish_reconciliation(
                        engine,
                        result,
                        outputs,
                        contract=contract,
                        acknowledged_reject_count=len(st.session_state.acknowledged_rejects),
                    )
                except Exception as exc:
                    st.warning(f"Database reconciliation could not run: {exc}")
                    pre_reconciliation = build_transform_reconciliation(
                        result,
                        outputs,
                        contract=contract,
                        acknowledged_reject_count=len(st.session_state.acknowledged_rejects),
                    )
            else:
                pre_reconciliation = build_transform_reconciliation(
                    result,
                    outputs,
                    contract=contract,
                    acknowledged_reject_count=len(st.session_state.acknowledged_rejects),
                )
            st.session_state.pre_reconciliation = pre_reconciliation
            render_reconciliation_result(pre_reconciliation)

            if st.session_state.run_kind == "row_correction":
                source_hash = source_dataframe_fingerprint(st.session_state.source_df)
                replay_check = {
                    "source_file_hash": source_hash,
                    "source_file_hash_short": source_hash[:12],
                    "is_replay": False,
                    "previous_import_run_id": st.session_state.parent_import_run_id,
                }
            else:
                replay_check = render_import_replay_check(st.session_state.source_df, ok)
            st.session_state.import_replay_check = replay_check
            render_reviewer_signoff()
            signoff = st.session_state.signoff
            publish_blocked = (
                not ok
                or not mapping_is_current
                or pre_reconciliation.status == "FAIL"
                or signoff is None
                or bool(replay_check.get("is_replay") and not st.session_state.import_replay_acknowledged)
                or bool(signoff and signoff.get("decision") == "Needs customer correction")
            )
            if pre_reconciliation.status == "FAIL":
                st.info("Resolve hard reconciliation failures before publishing.")
            elif replay_check.get("is_replay") and not st.session_state.import_replay_acknowledged:
                st.info("Acknowledge the import replay before publishing.")
            elif signoff is None:
                st.info("Save reviewer signoff before publishing.")
            elif signoff.get("decision") == "Needs customer correction":
                st.warning("This signoff decision blocks publish until customer corrections are complete.")
            if st.button(
                "Publish accepted records and audit trail",
                use_container_width=True,
                disabled=publish_blocked,
            ):
                try:
                    engine = get_engine()
                    outcome = publish_import(
                        engine=engine,
                        file_name=st.session_state.file_name or "uploaded.csv",
                        mapping_mode=st.session_state.mapping_mode,
                        mappings=st.session_state.mappings or [],
                        validation_result=result,
                        outputs=outputs,
                        target_schema_name=contract.name,
                        target_schema_version=contract.version,
                        mapping_template_name=st.session_state.mapping_template_name,
                        source_file_hash=replay_check.get("source_file_hash", ""),
                        import_replay_check=replay_check,
                        replay_acknowledged=bool(st.session_state.import_replay_acknowledged),
                        source_coverage=current_source_coverage(st.session_state.source_df, st.session_state.mappings),
                        source_coverage_reviewed=bool(st.session_state.source_coverage_reviewed),
                        signoff=signoff,
                        contract=contract,
                        mapping_template_version=int(st.session_state.mapping_template_version),
                        parent_import_run_id=st.session_state.parent_import_run_id,
                        run_kind=st.session_state.run_kind,
                        correction_attempt_number=(1 if st.session_state.run_kind == "row_correction" else None),
                        corrections=st.session_state.correction_audit,
                        acknowledged_reject_count=len(st.session_state.acknowledged_rejects),
                        return_outcome=True,
                    )
                    if not isinstance(outcome, PublishOutcome):
                        raise RuntimeError("Publish did not return reconciliation evidence.")
                    st.session_state.published = True
                    st.session_state.import_run_id = outcome.import_run_id
                    st.session_state.pre_reconciliation = outcome.pre_reconciliation
                    st.session_state.post_reconciliation = outcome.post_reconciliation
                    st.success(f"Published import run {outcome.import_run_id}.")
                except ReconciliationError as exc:
                    failed_run_id = getattr(exc, "failed_import_run_id", None)
                    suffix = f" Failed import run {failed_run_id} was retained for audit." if failed_run_id else ""
                    st.error(f"Publish reconciliation failed: {exc}.{suffix}")
                except Exception as exc:
                    st.error(f"Publish failed: {exc}")

    if active_step == "Report":
        st.subheader("Reports And Exports")
        contract = current_contract()
        outputs = active_outputs()
        result = active_validation_result()
        if outputs is None or result is None:
            st.info("Transform accepted rows first.")
        else:
            source_file_hash = source_dataframe_fingerprint(st.session_state.source_df)
            report_replay_check = st.session_state.import_replay_check or {
                "source_file_hash": source_file_hash,
                "is_replay": False,
            }
            report_data = build_report_data(
                file_name=st.session_state.file_name or "uploaded.csv",
                mapping_mode=st.session_state.mapping_mode,
                mappings=st.session_state.mappings or [],
                validation_result=result,
                outputs=outputs,
                published=st.session_state.published,
                import_run_id=st.session_state.import_run_id,
                target_schema_name=contract.name,
                target_schema_version=contract.version,
                mapping_template_name=st.session_state.mapping_template_name,
                source_file_hash=source_file_hash,
                import_replay_check=report_replay_check,
                source_coverage=current_source_coverage(st.session_state.source_df, st.session_state.mappings),
                source_coverage_reviewed=bool(st.session_state.source_coverage_reviewed),
                signoff=st.session_state.signoff,
                contract=contract,
                mapping_template_version=st.session_state.mapping_template_version,
                pre_reconciliation=st.session_state.pre_reconciliation,
                post_reconciliation=st.session_state.post_reconciliation,
                correction_audit=st.session_state.correction_audit,
                parent_import_run_id=st.session_state.parent_import_run_id,
                run_kind=st.session_state.run_kind,
            )
            html_report = render_html_report(report_data)
            pdf_report = render_pdf_report(report_data)
            st.components.v1.html(html_report, height=600, scrolling=True)

            reconciliation_result = st.session_state.post_reconciliation or st.session_state.pre_reconciliation
            col1, col2, col3, col4 = st.columns(4)
            col1.download_button(
                "Download HTML report",
                data=html_report.encode("utf-8"),
                file_name="validation_report.html",
                mime="text/html",
                use_container_width=True,
            )
            col2.download_button(
                "Download PDF report",
                data=pdf_report,
                file_name="validation_report.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
            col3.download_button(
                "Download reconciliation.json",
                data=(reconciliation_json_bytes(reconciliation_result) if reconciliation_result is not None else b"{}"),
                file_name="reconciliation.json",
                mime="application/json",
                use_container_width=True,
            )
            col4.download_button(
                "Download target_contract.json",
                data=contract_json_bytes(contract),
                file_name=f"{contract.contract_key}__{contract.version}.json",
                mime="application/json",
                use_container_width=True,
            )

            canonical_tables = outputs.tables or {
                "members": outputs.members,
                "plans": outputs.plans,
                "member_coverage": outputs.member_coverage,
            }
            selected_canonical_table = st.selectbox("Canonical output", list(canonical_tables))
            st.download_button(
                f"Download {selected_canonical_table}.csv",
                data=dataframe_to_csv_bytes(canonical_tables[selected_canonical_table]),
                file_name=f"{selected_canonical_table}.csv",
                mime="text/csv",
                use_container_width=True,
            )

            col5, col6, col7, col8 = st.columns(4)
            col5.download_button(
                "Download rejected rows",
                data=dataframe_to_csv_bytes(outputs.rejected_rows),
                file_name="rejected_rows_with_original_values.csv",
                mime="text/csv",
                use_container_width=True,
            )
            col6.download_button(
                "Download correction queue",
                data=dataframe_to_csv_bytes(outputs.correction_work_queue),
                file_name="rejected_rows_for_correction.csv",
                mime="text/csv",
                use_container_width=True,
            )
            col7.download_button(
                "Download correction audit",
                data=dataframe_to_csv_bytes(pd.DataFrame(st.session_state.correction_audit)),
                file_name="correction_audit.csv",
                mime="text/csv",
                use_container_width=True,
            )
            col8.download_button(
                "Download field_lineage.csv",
                data=dataframe_to_csv_bytes(outputs.field_lineage),
                file_name="field_lineage.csv",
                mime="text/csv",
                use_container_width=True,
            )

            mapping_template_payload = {
                "template_name": st.session_state.mapping_template_name or "Ad hoc mapping",
                "template_version": st.session_state.mapping_template_version,
                "contract_key": contract.contract_key,
                "contract_version": contract.version,
                "contract_checksum": contract.checksum,
                "mappings": st.session_state.mappings or [],
            }
            st.download_button(
                "Download mapping template JSON",
                data=json.dumps(mapping_template_payload, indent=2, default=str).encode("utf-8"),
                file_name="mapping_template.json",
                mime="application/json",
                use_container_width=True,
            )


if __name__ == "__main__":
    main()
