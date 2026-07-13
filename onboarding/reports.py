from __future__ import annotations

import html
from datetime import datetime
from io import BytesIO
from typing import Any

import pandas as pd

from onboarding.contracts import ContractVersion
from onboarding.reconciliation import ReconciliationResult
from onboarding.source_coverage import source_coverage_summary
from onboarding.transform import TransformOutputs
from onboarding.validation import ValidationResult


def _issues_by_type(issues_df: pd.DataFrame, severity: str) -> list[dict[str, Any]]:
    if issues_df.empty:
        return []
    filtered = issues_df[issues_df["severity"].eq(severity)]
    if filtered.empty:
        return []
    grouped = (
        filtered.groupby("issue_code", as_index=False)["source_row_number"]
        .nunique()
        .rename(columns={"source_row_number": "row_count"})
        .sort_values("row_count", ascending=False)
    )
    return grouped.to_dict("records")


def build_report_data(
    *,
    file_name: str,
    mapping_mode: str,
    mappings: list[dict[str, Any]],
    validation_result: ValidationResult,
    outputs: TransformOutputs,
    published: bool = False,
    import_run_id: int | None = None,
    target_schema_name: str = "",
    target_schema_version: str = "",
    mapping_template_name: str = "",
    source_file_hash: str = "",
    import_replay_check: dict[str, Any] | None = None,
    source_coverage: list[dict[str, Any]] | None = None,
    source_coverage_reviewed: bool = False,
    signoff: dict[str, Any] | None = None,
    contract: ContractVersion | None = None,
    mapping_template_version: int | str = "",
    pre_reconciliation: ReconciliationResult | None = None,
    post_reconciliation: ReconciliationResult | None = None,
    correction_audit: list[dict[str, Any]] | None = None,
    parent_import_run_id: int | None = None,
    run_kind: str = "original",
) -> dict[str, Any]:
    issues_df = validation_result.issues_df
    rejected_rows = outputs.rejected_rows
    source_coverage_rows = source_coverage or []
    coverage_summary = source_coverage_summary(source_coverage_rows)
    replay_check = import_replay_check or {}
    signoff = signoff or {}
    correction_audit = correction_audit or []
    field_lineage = outputs.field_lineage
    if published:
        signoff_status = "Published to PostgreSQL"
    elif validation_result.rejected_row_count > 0:
        signoff_status = "Needs customer correction"
    else:
        signoff_status = "Ready for publish"

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "import_summary": {
            "file_name": file_name,
            "mapping_mode": mapping_mode,
            "source_rows": len(validation_result.normalized_df),
            "accepted_rows": validation_result.accepted_row_count,
            "rejected_rows": validation_result.rejected_row_count,
            "warning_count": validation_result.warning_count,
            "published": published,
            "import_run_id": import_run_id,
            "target_schema_name": target_schema_name,
            "target_schema_version": target_schema_version,
            "mapping_template_name": mapping_template_name,
            "mapping_template_version": mapping_template_version,
            "parent_import_run_id": parent_import_run_id,
            "run_kind": run_kind,
            "source_file_hash_short": source_file_hash[:12] if source_file_hash else "",
            "is_replay": bool(replay_check.get("is_replay")),
            "previous_import_run_id": replay_check.get("previous_import_run_id"),
        },
        "mapping_summary": [
            {
                "target_table": mapping.get("target_table"),
                "target_field": mapping.get("target_field"),
                "target_data_type": mapping.get("target_data_type"),
                "target_validation_kind": mapping.get("target_validation_kind"),
                "source_column": mapping.get("source_column"),
                "source_inferred_type": mapping.get("source_inferred_type"),
                "type_alignment": mapping.get("type_alignment"),
                "type_alignment_reason": mapping.get("type_alignment_reason"),
                "confidence": mapping.get("confidence"),
                "mapping_mode": mapping_mode,
                "needs_review": mapping.get("needs_review"),
                "approved": mapping.get("approved"),
                "reason": mapping.get("reason") or mapping.get("rationale"),
                "source_columns": "; ".join(
                    str(value) for value in (mapping.get("source_columns") or [mapping.get("source_column")]) if value
                ),
                "transformation_operations": ", ".join(
                    str(step.get("operation") or "") for step in mapping.get("transformation_steps") or []
                ),
                "failure_policy": mapping.get("failure_policy") or "error",
                "transformation_approved": bool(mapping.get("transformation_approved", False)),
            }
            for mapping in mappings
        ],
        "validation_summary": {
            "blocking_errors": _issues_by_type(issues_df, "error"),
            "warnings": _issues_by_type(issues_df, "warning"),
        },
        "reconciliation_summary": {
            "source_rows": len(validation_result.normalized_df),
            "accepted_rows": validation_result.accepted_row_count,
            "rejected_rows": validation_result.rejected_row_count,
            "members_created": len(outputs.members),
            "plans_created": len(outputs.plans),
            "coverage_records_created": len(outputs.member_coverage),
            "status": (
                post_reconciliation.status
                if post_reconciliation
                else pre_reconciliation.status if pre_reconciliation else "Not run"
            ),
        },
        "contract_summary": {
            "contract_key": contract.contract_key if contract else "",
            "contract_name": contract.name if contract else target_schema_name,
            "contract_version": contract.version if contract else target_schema_version,
            "contract_status": contract.status if contract else "",
            "contract_checksum_short": contract.checksum[:12] if contract else "",
            "domain": contract.domain if contract else "",
        },
        "transformation_summary": [
            {
                "target": f"{mapping.get('target_table')}.{mapping.get('target_field')}",
                "sources": "; ".join(
                    str(value) for value in (mapping.get("source_columns") or [mapping.get("source_column")]) if value
                ),
                "operations": ", ".join(
                    str(step.get("operation") or "") for step in mapping.get("transformation_steps") or []
                )
                or "canonical normalization only",
                "failure_policy": mapping.get("failure_policy") or "error",
                "approved": bool(mapping.get("transformation_approved", False)),
            }
            for mapping in mappings
            if mapping.get("approved")
        ],
        "reconciliation_forecast": pre_reconciliation.to_dict() if pre_reconciliation else {},
        "reconciliation_actual": post_reconciliation.to_dict() if post_reconciliation else {},
        "reconciliation_checks": (
            [check.__dict__ for check in post_reconciliation.checks]
            if post_reconciliation
            else [check.__dict__ for check in pre_reconciliation.checks] if pre_reconciliation else []
        ),
        "correction_summary": {
            "corrected_records": len({row.get("source_record_id") for row in correction_audit}),
            "corrected_fields": len(correction_audit),
            "parent_import_run_id": parent_import_run_id,
            "run_kind": run_kind,
        },
        "correction_audit_preview": correction_audit[:25],
        "source_coverage_summary": {
            **coverage_summary,
            "source_coverage_reviewed": source_coverage_reviewed,
        },
        "source_coverage_detail": source_coverage_rows,
        "reviewer_signoff": {
            "reviewer_name": signoff.get("reviewer_name", ""),
            "reviewer_role": signoff.get("reviewer_role", ""),
            "decision": signoff.get("decision", ""),
            "comment": signoff.get("comment", ""),
            "signed_off_at": signoff.get("signed_off_at", ""),
        },
        "rejected_rows_preview": rejected_rows.head(25).to_dict("records") if not rejected_rows.empty else [],
        "field_lineage_preview": field_lineage.head(50).to_dict("records") if not field_lineage.empty else [],
        "signoff_status": signoff_status,
    }


def _table_html(rows: list[dict[str, Any]], empty_text: str) -> str:
    if not rows:
        return f"<p>{html.escape(empty_text)}</p>"
    columns = list(rows[0].keys())
    header = "".join(f"<th>{html.escape(str(column))}</th>" for column in columns)
    body = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(row.get(column, '')))}</td>" for column in columns)
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _select_columns(rows: list[dict[str, Any]], columns: list[str]) -> list[dict[str, Any]]:
    return [{column: row.get(column, "") for column in columns} for row in rows]


def render_html_report(report_data: dict[str, Any]) -> str:
    import_summary = report_data["import_summary"]
    validation = report_data["validation_summary"]
    reconciliation = report_data["reconciliation_summary"]
    summary_rows = [{"metric": key, "value": value} for key, value in import_summary.items()]
    reconciliation_rows = [{"metric": key, "value": value} for key, value in reconciliation.items()]
    coverage_rows = [{"metric": key, "value": value} for key, value in report_data["source_coverage_summary"].items()]
    signoff_rows = [{"field": key, "value": value} for key, value in report_data["reviewer_signoff"].items()]
    contract_rows = [{"field": key, "value": value} for key, value in report_data["contract_summary"].items()]
    active_reconciliation = report_data["reconciliation_actual"] or report_data["reconciliation_forecast"]
    reconciliation_table_rows = [
        {"target_table": table_name, **metrics}
        for table_name, metrics in (active_reconciliation.get("table_metrics") or {}).items()
    ]
    correction_rows = [{"metric": key, "value": value} for key, value in report_data["correction_summary"].items()]

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Customer Data Onboarding Validation Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; color: #17202a; margin: 32px; }}
    h1 {{ font-size: 24px; margin-bottom: 4px; }}
    h2 {{ font-size: 18px; margin-top: 28px; border-bottom: 1px solid #d9e2ec; padding-bottom: 6px; }}
    .status {{ display: inline-block; padding: 6px 10px; border-radius: 4px; background: #e8f3ff; font-weight: 700; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 13px; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 7px; text-align: left; vertical-align: top; }}
    th {{ background: #f4f7fb; }}
    .muted {{ color: #5d6d7e; }}
  </style>
</head>
<body>
  <h1>Customer Data Onboarding Validation Report</h1>
  <p class="muted">Generated at {html.escape(report_data["generated_at"])}</p>
  <p class="status">{html.escape(report_data["signoff_status"])}</p>

  <h2>Import Summary</h2>
  {_table_html(summary_rows, "No import summary available.")}

  <h2>Target Contract</h2>
  {_table_html(contract_rows, "No target contract metadata available.")}

  <h2>Mapping Summary</h2>
  {_table_html(report_data["mapping_summary"], "No mapping decisions available.")}

  <h2>Transformation Summary</h2>
  {_table_html(report_data["transformation_summary"], "No approved transformation pipelines available.")}

  <h2>Source Coverage</h2>
  {_table_html(coverage_rows, "No source coverage available.")}
  {_table_html(report_data["source_coverage_detail"], "No source column details available.")}

  <h2>Validation Results: Blocking Errors</h2>
  {_table_html(validation["blocking_errors"], "No blocking errors found.")}

  <h2>Validation Results: Warnings</h2>
  {_table_html(validation["warnings"], "No warnings found.")}

  <h2>Reconciliation</h2>
  {_table_html(reconciliation_rows, "No reconciliation summary available.")}
  {_table_html(reconciliation_table_rows, "No table-level reconciliation metrics available.")}
  {_table_html(report_data["reconciliation_checks"], "No detailed reconciliation checks available.")}

  <h2>Correction And Reprocessing</h2>
  {_table_html(correction_rows, "No correction summary available.")}
  {_table_html(report_data["correction_audit_preview"], "No corrected fields in this run.")}

  <h2>Reviewer Signoff</h2>
  {_table_html(signoff_rows, "No reviewer signoff captured.")}

  <h2>Rejected Rows Preview</h2>
  {_table_html(report_data["rejected_rows_preview"], "No rejected rows.")}

  <h2>Field-Level Lineage Preview</h2>
  {_table_html(_select_columns(report_data["field_lineage_preview"], ["source_row_number", "source_record_id", "row_status", "lineage_status", "target_table", "target_field", "source_column", "original_value", "corrected_values_json", "final_value", "transformation_applied", "issue_codes"]), "No field lineage available.")}
</body>
</html>
"""


def render_pdf_report(report_data: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(letter), rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36
    )
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle(
        "ReportCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7,
        leading=8.5,
        wordWrap="CJK",
    )
    header_style = ParagraphStyle(
        "ReportHeaderCell",
        parent=cell_style,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#17202a"),
    )
    story: list[Any] = []

    def add_heading(text: str) -> None:
        story.append(Spacer(1, 10))
        story.append(Paragraph(text, styles["Heading2"]))

    def add_table(rows: list[dict[str, Any]], empty_text: str) -> None:
        if not rows:
            story.append(Paragraph(empty_text, styles["BodyText"]))
            return
        columns = list(rows[0].keys())
        available_width = doc.width
        column_width = available_width / len(columns)
        data = [[Paragraph(html.escape(str(column)), header_style) for column in columns]]
        for row in rows:
            data.append(
                [
                    Paragraph(html.escape(str(row.get(column, ""))).replace("\n", "<br/>"), cell_style)
                    for column in columns
                ]
            )
        table = Table(data, colWidths=[column_width] * len(columns), repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef3f8")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bac7d5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(table)

    story.append(Paragraph("Customer Data Onboarding Validation Report", styles["Title"]))
    story.append(Paragraph(f"Generated at {report_data['generated_at']}", styles["BodyText"]))
    story.append(Paragraph(f"Sign-off status: {report_data['signoff_status']}", styles["Heading3"]))

    add_heading("Import Summary")
    add_table(
        [{"metric": key, "value": value} for key, value in report_data["import_summary"].items()],
        "No import summary available.",
    )

    add_heading("Target Contract")
    add_table(
        [{"field": key, "value": value} for key, value in report_data["contract_summary"].items()],
        "No target contract metadata available.",
    )

    add_heading("Mapping Summary")
    add_table(
        _select_columns(
            report_data["mapping_summary"],
            [
                "target_table",
                "target_field",
                "source_column",
                "confidence",
                "type_alignment",
                "approved",
                "needs_review",
                "reason",
            ],
        ),
        "No mapping decisions available.",
    )

    add_heading("Transformation Summary")
    add_table(
        report_data["transformation_summary"],
        "No approved transformation pipelines available.",
    )

    add_heading("Source Coverage")
    add_table(
        [{"metric": key, "value": value} for key, value in report_data["source_coverage_summary"].items()],
        "No source coverage available.",
    )
    add_table(
        _select_columns(
            report_data["source_coverage_detail"],
            [
                "source_column",
                "inferred_type",
                "coverage_status",
                "approved_targets",
                "review_recommendation",
            ],
        ),
        "No source column details available.",
    )

    add_heading("Validation Results: Blocking Errors")
    add_table(report_data["validation_summary"]["blocking_errors"], "No blocking errors found.")

    add_heading("Validation Results: Warnings")
    add_table(report_data["validation_summary"]["warnings"], "No warnings found.")

    add_heading("Reconciliation")
    add_table(
        [{"metric": key, "value": value} for key, value in report_data["reconciliation_summary"].items()],
        "No reconciliation summary available.",
    )
    active_reconciliation = report_data["reconciliation_actual"] or report_data["reconciliation_forecast"]
    add_table(
        [
            {"target_table": table_name, **metrics}
            for table_name, metrics in (active_reconciliation.get("table_metrics") or {}).items()
        ],
        "No table-level reconciliation metrics available.",
    )
    add_table(
        _select_columns(
            report_data["reconciliation_checks"],
            ["check_code", "severity", "status", "expected_value", "actual_value", "message"],
        ),
        "No detailed reconciliation checks available.",
    )

    add_heading("Correction And Reprocessing")
    add_table(
        [{"metric": key, "value": value} for key, value in report_data["correction_summary"].items()],
        "No correction summary available.",
    )
    add_table(
        _select_columns(
            report_data["correction_audit_preview"],
            [
                "source_record_id",
                "source_row_number",
                "source_column",
                "original_value",
                "corrected_value",
                "correction_reason",
                "correction_status",
            ],
        ),
        "No corrected fields in this run.",
    )

    add_heading("Reviewer Signoff")
    add_table(
        [{"field": key, "value": value} for key, value in report_data["reviewer_signoff"].items()],
        "No reviewer signoff captured.",
    )

    add_heading("Rejected Rows Preview")
    add_table(
        _select_columns(
            report_data["rejected_rows_preview"],
            [
                "source_row_number",
                "source_record_id",
                "error_count",
                "error_codes",
                "error_target_fields",
                "errors",
                "warning_count",
                "warnings",
            ],
        ),
        "No rejected rows.",
    )

    add_heading("Field-Level Lineage Preview")
    add_table(
        _select_columns(
            report_data["field_lineage_preview"],
            [
                "source_row_number",
                "row_status",
                "lineage_status",
                "target_table",
                "target_field",
                "source_column",
                "original_value",
                "corrected_values_json",
                "final_value",
                "issue_codes",
            ],
        ),
        "No field lineage available.",
    )

    doc.build(story)
    return buffer.getvalue()
