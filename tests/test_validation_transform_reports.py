from __future__ import annotations

import unittest

import pandas as pd

from onboarding.profiler import profile_dataframe
from onboarding.reports import build_report_data, render_html_report, render_pdf_report
from onboarding.schema import TARGET_SCHEMA
from onboarding.source_coverage import build_source_coverage
from onboarding.transform import approved_source_columns_by_target_field, build_canonical_flat, transform_outputs
from onboarding.validation import validate_canonical_frame


def approved_mapping(target_table: str, target_field: str, source_column: str) -> dict:
    return {
        "target_table": target_table,
        "target_field": target_field,
        "source_column": source_column,
        "approved": True,
        "confidence": 95,
        "needs_review": False,
        "reason": "test mapping",
    }


class ValidationTransformReportTests(unittest.TestCase):
    def test_validate_transform_and_report(self) -> None:
        source = pd.DataFrame(
            {
                "Member Number": ["MEM001", "MEM002", "MEM003"],
                "First": ["Ana", "Sam", "Priya"],
                "Last": ["Patel", "Lee", "Khan"],
                "DOB": ["1988-01-01", "1977-02-03", "2035-04-05"],
                "Sex": ["F", "M", "F"],
                "Email Address": ["ana@example.com", "bad-email", "priya@example.com"],
                "Phone": ["555-111-2222", "abc", "555-333-4444"],
                "Plan Code": ["PPO-100", "HMO-200", "PPO-100"],
                "Plan Name": ["Silver PPO", "Basic HMO", "Silver PPO"],
                "Plan Type": ["PPO", "HMO", "PPO"],
                "Carrier": ["Acme Health", "Acme Health", "Acme Health"],
                "Effective Date": ["2024-01-01", "2024-01-01", "2024-01-01"],
                "Term Date": ["", "2023-01-01", ""],
                "Status": ["Active", "Termed", "Active"],
                "Relation": ["Self", "Self", "Self"],
                "Subscriber ID": ["MEM001", "MEM002", "MEM003"],
            }
        )
        mappings = [
            approved_mapping("members", "member_id", "Member Number"),
            approved_mapping("members", "first_name", "First"),
            approved_mapping("members", "last_name", "Last"),
            approved_mapping("members", "date_of_birth", "DOB"),
            approved_mapping("members", "gender", "Sex"),
            approved_mapping("members", "email", "Email Address"),
            approved_mapping("members", "phone", "Phone"),
            approved_mapping("plans", "plan_id", "Plan Code"),
            approved_mapping("plans", "plan_name", "Plan Name"),
            approved_mapping("plans", "plan_type", "Plan Type"),
            approved_mapping("plans", "carrier_name", "Carrier"),
            approved_mapping("member_coverage", "member_id", "Member Number"),
            approved_mapping("member_coverage", "plan_id", "Plan Code"),
            approved_mapping("member_coverage", "coverage_start_date", "Effective Date"),
            approved_mapping("member_coverage", "coverage_end_date", "Term Date"),
            approved_mapping("member_coverage", "coverage_status", "Status"),
            approved_mapping("member_coverage", "relationship_to_subscriber", "Relation"),
            approved_mapping("member_coverage", "subscriber_id", "Subscriber ID"),
        ]
        flat = build_canonical_flat(source, mappings)
        result = validate_canonical_frame(flat, approved_source_columns_by_target_field(mappings))
        outputs = transform_outputs(result.normalized_df, source, result.issues_df, mappings)

        self.assertEqual(result.rejected_row_count, 2)
        self.assertEqual(len(outputs.members), 1)
        self.assertEqual(len(outputs.plans), 1)
        self.assertEqual(len(outputs.member_coverage), 1)
        self.assertEqual(len(outputs.rejected_rows), 2)
        source_column_by_issue = {
            (row["source_row_number"], row["issue_code"]): row["source_column"]
            for row in result.issues_df.to_dict("records")
        }
        self.assertEqual(source_column_by_issue[(4, "date_of_birth_future")], "DOB")
        self.assertEqual(source_column_by_issue[(3, "email_invalid")], "Email Address")
        self.assertIn("error_codes", outputs.rejected_rows.columns)
        self.assertIn("error_target_fields", outputs.rejected_rows.columns)
        self.assertIn("original__DOB", outputs.rejected_rows.columns)
        future_dob_reject = outputs.rejected_rows[outputs.rejected_rows["source_row_number"] == 4].iloc[0]
        self.assertIn("date_of_birth", future_dob_reject["error_target_fields"])
        self.assertEqual(len(outputs.field_lineage), len(source) * len(TARGET_SCHEMA))

        dob_lineage = outputs.field_lineage[
            (outputs.field_lineage["source_row_number"] == 2)
            & (outputs.field_lineage["target_field"] == "date_of_birth")
        ].iloc[0]
        self.assertEqual(dob_lineage["source_column"], "DOB")
        self.assertEqual(dob_lineage["original_value"], "1988-01-01")
        self.assertEqual(dob_lineage["normalized_value"], "1988-01-01")
        self.assertEqual(dob_lineage["lineage_status"], "accepted")

        rejected_dob_lineage = outputs.field_lineage[
            (outputs.field_lineage["source_row_number"] == 4)
            & (outputs.field_lineage["target_field"] == "date_of_birth")
        ].iloc[0]
        self.assertEqual(rejected_dob_lineage["lineage_status"], "error")
        self.assertIn("date_of_birth_future", rejected_dob_lineage["issue_codes"])

        report_data = build_report_data(
            file_name="demo.csv",
            mapping_mode="Rules-Based",
            mappings=mappings,
            validation_result=result,
            outputs=outputs,
            target_schema_name="Healthcare Eligibility Canonical v1",
            target_schema_version="1.0.0",
            mapping_template_name="Demo template",
            source_coverage=build_source_coverage(list(source.columns), profile_dataframe(source), mappings),
            source_coverage_reviewed=True,
            signoff={
                "reviewer_name": "Priya S.",
                "reviewer_role": "Implementation",
                "decision": "Approved to publish accepted records",
                "comment": "Accepted rows can be published.",
                "signed_off_at": "2026-07-07 10:42:00",
            },
        )
        html = render_html_report(report_data)
        pdf = render_pdf_report(report_data)
        self.assertIn("Customer Data Onboarding Validation Report", html)
        self.assertIn("Source Coverage", html)
        self.assertIn("Reviewer Signoff", html)
        self.assertIn("Field-Level Lineage Preview", html)
        self.assertEqual(report_data["reviewer_signoff"]["reviewer_name"], "Priya S.")
        self.assertTrue(report_data["field_lineage_preview"])
        self.assertGreater(len(pdf), 1000)


if __name__ == "__main__":
    unittest.main()
