from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from onboarding.ai_mapper import AIMapperValidationError, OpenAIConfigurationError, suggest_mappings_with_ai
from onboarding.database import connection_status, get_engine, init_db, publish_import
from onboarding.exports import dataframe_to_csv_bytes
from onboarding.idempotency import build_import_replay_check, source_dataframe_fingerprint
from onboarding.mapping_quality import apply_mapping_type_alignment, blocking_mapping_alignment_issues
from onboarding.mapping_templates import (
    apply_mapping_template,
    list_mapping_templates,
    load_mapping_template,
    save_mapping_template,
)
from onboarding.profiler import profile_dataframe
from onboarding.reports import build_report_data, render_html_report, render_pdf_report
from onboarding.rules_mapper import generate_rules_based_mappings
from onboarding.schema import TARGET_SCHEMA
from onboarding.source_coverage import build_source_coverage, source_coverage_summary, unused_source_columns
from onboarding.transform import approved_source_columns_by_target_field, build_canonical_flat, transform_outputs
from onboarding.validation import issue_summary, validate_canonical_frame

ROOT = Path(__file__).resolve().parent
DEMO_FILE = ROOT / "data" / "demo" / "messy_eligibility_file.csv"
TARGET_SCHEMA_NAME = "Healthcare Eligibility Canonical v1"
TARGET_SCHEMA_VERSION = "1.0.0"
TARGET_SCHEMA_REGISTRY = {
    TARGET_SCHEMA_NAME: TARGET_SCHEMA,
}
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
        "import_replay_acknowledged": False,
        "import_replay_check": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def reset_downstream() -> None:
    for key in ["profiles", "mappings", "validation_result", "outputs", "published", "import_run_id", "signoff"]:
        st.session_state[key] = None if key not in {"published"} else False
    st.session_state.source_coverage_reviewed = False
    st.session_state.mapping_template_name = ""
    st.session_state.import_replay_acknowledged = False
    st.session_state.import_replay_check = None


def load_source_df(df: pd.DataFrame, file_name: str) -> None:
    st.session_state.source_df = df
    st.session_state.file_name = file_name
    reset_downstream()


def mapping_editor_rows(mappings: list[dict[str, Any]]) -> pd.DataFrame:
    display_rows = []
    for mapping in mappings:
        row = dict(mapping)
        row["review_flags"] = ", ".join(mapping.get("review_flags") or [])
        row["score_breakdown"] = str(mapping.get("score_breakdown") or "")
        display_rows.append(row)
    return pd.DataFrame(display_rows)


def rows_to_mappings(rows: pd.DataFrame) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    for row in rows.to_dict("records"):
        flags = row.get("review_flags", "")
        row["review_flags"] = [flag.strip() for flag in str(flags).split(",") if flag.strip()]
        if pd.isna(row.get("source_column")):
            row["source_column"] = ""
        row["approved"] = bool(row.get("approved", False))
        mappings.append(row)
    return mappings


def required_mapping_gaps(mappings: list[dict[str, Any]]) -> list[str]:
    approved_by_key = {
        (mapping.get("target_table"), mapping.get("target_field"))
        for mapping in mappings
        if mapping.get("approved") and mapping.get("source_column")
    }
    gaps = []
    for field in TARGET_SCHEMA:
        if field.field == "coverage_id":
            continue
        if field.required and (field.table, field.field) not in approved_by_key:
            gaps.append(f"{field.table}.{field.field}")
    return gaps


def target_schema_rows() -> pd.DataFrame:
    rows = []
    selected_schema = st.session_state.get("target_schema_name", TARGET_SCHEMA_NAME)
    for field in TARGET_SCHEMA_REGISTRY[selected_schema]:
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
    st.session_state.signoff = {
        "reviewer_name": reviewer_name,
        "reviewer_role": str(st.session_state.get("reviewer_role") or "").strip(),
        "decision": st.session_state.get("signoff_decision"),
        "comment": str(st.session_state.get("signoff_comment") or "").strip(),
        "signed_off_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_schema_name": TARGET_SCHEMA_NAME,
        "target_schema_version": TARGET_SCHEMA_VERSION,
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


def show_metric_row(values: dict[str, Any]) -> None:
    columns = st.columns(len(values))
    for column, (label, value) in zip(columns, values.items(), strict=False):
        column.metric(label, value)


def main() -> None:
    st.set_page_config(page_title="Customer Data Onboarding Copilot", layout="wide")
    init_state()

    st.title("Customer Data Onboarding Copilot")
    st.caption(f"Target: {TARGET_SCHEMA_NAME} ({TARGET_SCHEMA_VERSION})")
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
        selector_col, _ = st.columns([1, 2])
        selector_col.selectbox(
            "Target schema",
            options=list(TARGET_SCHEMA_REGISTRY.keys()),
            key="target_schema_name",
        )
        schema_df = target_schema_rows()
        selected_schema = TARGET_SCHEMA_REGISTRY[st.session_state.target_schema_name]
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

    if active_step == "Upload":
        st.subheader("Upload Source File")
        st.caption(f"Source columns will be mapped into {TARGET_SCHEMA_NAME}.")
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
        st.caption(f"Target schema: {TARGET_SCHEMA_NAME}")
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
                    if template.get("schema_name") == TARGET_SCHEMA_NAME
                    and template.get("schema_version") == TARGET_SCHEMA_VERSION
                ]
                if templates:
                    labels = {
                        f"{template['template_name']} ({template['saved_at']})": template for template in templates
                    }
                    selected_label = st.selectbox("Load saved template", list(labels.keys()))
                    if st.button("Load mapping template", use_container_width=True):
                        template = load_mapping_template(labels[selected_label]["file_name"])
                        loaded_mappings = apply_mapping_template(template, list(source_df.columns))
                        st.session_state.mappings = apply_mapping_type_alignment(
                            loaded_mappings,
                            st.session_state.profiles or [],
                        )
                        st.session_state.mapping_template_name = str(template.get("template_name") or "")
                        st.session_state.source_coverage_reviewed = False
                        st.success(f"Loaded template: {st.session_state.mapping_template_name}")
                else:
                    st.info("No saved templates for this target schema version yet.")

            if st.button("Generate mapping suggestions", use_container_width=True):
                profiles = st.session_state.profiles
                rules_mappings = apply_mapping_type_alignment(
                    generate_rules_based_mappings(profiles),
                    profiles,
                )
                st.session_state.source_coverage_reviewed = False
                st.session_state.mapping_template_name = ""
                if st.session_state.mapping_mode == "Rules-Based":
                    st.session_state.mappings = rules_mappings
                else:
                    try:
                        ai_mappings = suggest_mappings_with_ai(profiles, rules_mappings)
                        st.session_state.mappings = apply_mapping_type_alignment(
                            expand_ai_mappings(ai_mappings, rules_mappings),
                            profiles,
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
                    ],
                )
                st.session_state.mappings = apply_mapping_type_alignment(
                    rows_to_mappings(edited),
                    st.session_state.profiles or [],
                )

                gaps = required_mapping_gaps(st.session_state.mappings)
                alignment_issues = blocking_mapping_alignment_issues(st.session_state.mappings)
                if gaps:
                    st.warning("Required fields still need approved mappings: " + ", ".join(gaps))
                elif alignment_issues:
                    st.error("Approved mappings have target/source type mismatches: " + "; ".join(alignment_issues))
                else:
                    st.success("Required mappings are approved.")

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
                                schema_name=TARGET_SCHEMA_NAME,
                                schema_version=TARGET_SCHEMA_VERSION,
                                source_columns=list(source_df.columns),
                                mappings=st.session_state.mappings,
                            )
                            st.session_state.mapping_template_name = saved_template["template_name"]
                            st.success(f"Saved template: {saved_template['template_name']}")

    if active_step == "Validate":
        st.subheader("Validation")
        source_df = st.session_state.source_df
        mappings = st.session_state.mappings
        if source_df is None or mappings is None:
            st.info("Load a file and approve mappings first.")
        else:
            mappings = apply_mapping_type_alignment(mappings, st.session_state.profiles or [])
            st.session_state.mappings = mappings
            gaps = required_mapping_gaps(mappings)
            alignment_issues = blocking_mapping_alignment_issues(mappings)
            coverage_rows = current_source_coverage(source_df, mappings)
            unused_columns = unused_source_columns(coverage_rows)
            if gaps:
                st.warning("Approve required mappings before validation: " + ", ".join(gaps))
            elif alignment_issues:
                st.warning("Fix approved mapping type mismatches before validation: " + "; ".join(alignment_issues))
            elif unused_columns and not st.session_state.source_coverage_reviewed:
                st.warning("Review and accept unused source columns before validation: " + ", ".join(unused_columns))
            elif st.button("Run validation", use_container_width=True):
                flat = build_canonical_flat(source_df, mappings)
                st.session_state.validation_result = validate_canonical_frame(
                    flat,
                    approved_source_columns_by_target_field(mappings),
                )
                st.session_state.outputs = None
                st.session_state.published = False
                st.session_state.import_run_id = None

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

    if active_step == "Transform":
        st.subheader("Transform")
        source_df = st.session_state.source_df
        result = st.session_state.validation_result
        if source_df is None or result is None:
            st.info("Run validation first.")
        else:
            if st.button("Transform accepted rows", use_container_width=True):
                st.session_state.outputs = transform_outputs(
                    result.normalized_df,
                    source_df,
                    result.issues_df,
                    st.session_state.mappings or [],
                )
            outputs = st.session_state.outputs
            if outputs is not None:
                output_tables = {
                    "members": outputs.members,
                    "plans": outputs.plans,
                    "member_coverage": outputs.member_coverage,
                    "rejected_rows_with_original_values": outputs.rejected_rows,
                    "field_lineage": outputs.field_lineage,
                }
                show_metric_row(
                    {
                        "Members": len(outputs.members),
                        "Plans": len(outputs.plans),
                        "Coverage records": len(outputs.member_coverage),
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
        outputs = st.session_state.outputs
        result = st.session_state.validation_result
        ok, message = connection_status()
        st.info(message if ok else f"Not connected: {message}")

        if outputs is None or result is None:
            st.info("Transform accepted rows before publishing.")
        else:
            replay_check = render_import_replay_check(st.session_state.source_df, ok)
            st.session_state.import_replay_check = replay_check
            render_reviewer_signoff()
            signoff = st.session_state.signoff
            if replay_check.get("is_replay") and not st.session_state.import_replay_acknowledged:
                st.info("Acknowledge the import replay before publishing.")
            elif signoff is None:
                st.info("Save reviewer signoff before publishing.")
            elif signoff.get("decision") == "Needs customer correction":
                st.warning("This signoff decision blocks publish until customer corrections are complete.")
            elif st.button("Publish accepted records and audit trail", use_container_width=True, disabled=not ok):
                try:
                    engine = get_engine()
                    import_run_id = publish_import(
                        engine=engine,
                        file_name=st.session_state.file_name or "uploaded.csv",
                        mapping_mode=st.session_state.mapping_mode,
                        mappings=st.session_state.mappings or [],
                        validation_result=result,
                        outputs=outputs,
                        target_schema_name=TARGET_SCHEMA_NAME,
                        target_schema_version=TARGET_SCHEMA_VERSION,
                        mapping_template_name=st.session_state.mapping_template_name,
                        source_file_hash=replay_check.get("source_file_hash", ""),
                        import_replay_check=replay_check,
                        replay_acknowledged=bool(st.session_state.import_replay_acknowledged),
                        source_coverage=current_source_coverage(st.session_state.source_df, st.session_state.mappings),
                        source_coverage_reviewed=bool(st.session_state.source_coverage_reviewed),
                        signoff=signoff,
                    )
                    st.session_state.published = True
                    st.session_state.import_run_id = import_run_id
                    st.success(f"Published import run {import_run_id}.")
                except Exception as exc:
                    st.error(f"Publish failed: {exc}")

    if active_step == "Report":
        st.subheader("Reports And Exports")
        outputs = st.session_state.outputs
        result = st.session_state.validation_result
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
                target_schema_name=TARGET_SCHEMA_NAME,
                target_schema_version=TARGET_SCHEMA_VERSION,
                mapping_template_name=st.session_state.mapping_template_name,
                source_file_hash=source_file_hash,
                import_replay_check=report_replay_check,
                source_coverage=current_source_coverage(st.session_state.source_df, st.session_state.mappings),
                source_coverage_reviewed=bool(st.session_state.source_coverage_reviewed),
                signoff=st.session_state.signoff,
            )
            html_report = render_html_report(report_data)
            pdf_report = render_pdf_report(report_data)
            st.components.v1.html(html_report, height=600, scrolling=True)

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
                "Download members.csv",
                data=dataframe_to_csv_bytes(outputs.members),
                file_name="members.csv",
                mime="text/csv",
                use_container_width=True,
            )
            col4.download_button(
                "Download rejected_rows_with_original_values.csv",
                data=dataframe_to_csv_bytes(outputs.rejected_rows),
                file_name="rejected_rows_with_original_values.csv",
                mime="text/csv",
                use_container_width=True,
            )

            col5, col6, col7 = st.columns(3)
            col5.download_button(
                "Download plans.csv",
                data=dataframe_to_csv_bytes(outputs.plans),
                file_name="plans.csv",
                mime="text/csv",
                use_container_width=True,
            )
            col7.download_button(
                "Download field_lineage.csv",
                data=dataframe_to_csv_bytes(outputs.field_lineage),
                file_name="field_lineage.csv",
                mime="text/csv",
                use_container_width=True,
            )
            col6.download_button(
                "Download member_coverage.csv",
                data=dataframe_to_csv_bytes(outputs.member_coverage),
                file_name="member_coverage.csv",
                mime="text/csv",
                use_container_width=True,
            )


if __name__ == "__main__":
    main()
