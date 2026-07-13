from __future__ import annotations

import unittest

import pandas as pd

from onboarding.contracts import default_contract
from onboarding.schema import TargetField
from onboarding.transform import _table_candidate_stats, build_canonical_flat
from onboarding.transformations import execute_transformation_pipeline
from onboarding.validation import validate_canonical_frame


class TransformationTests(unittest.TestCase):
    def test_executes_ordered_crosswalk_pipeline(self) -> None:
        target = next(
            field for field in default_contract().target_fields if field.table == "plans" and field.field == "plan_type"
        )
        mapping = {
            "target_field": "plan_type",
            "source_column": "Plan Type",
            "source_columns": ["Plan Type"],
            "transformation_steps": [
                {"operation": "trim", "parameters": {}},
                {"operation": "uppercase", "parameters": {}},
                {
                    "operation": "map_values",
                    "parameters": {"mapping": {"P.P.O.": "PPO"}},
                },
            ],
            "failure_policy": "error",
        }

        execution = execute_transformation_pipeline({"Plan Type": " p.p.o. "}, mapping, target_field=target)

        self.assertEqual(execution.value, "PPO")
        self.assertEqual([step["operation"] for step in execution.trace], ["trim", "uppercase", "map_values"])

    def test_transformation_failure_becomes_validation_issue(self) -> None:
        contract = default_contract()
        source = pd.DataFrame({"DOB": ["not-a-date"]})
        mapping = {
            "target_table": "members",
            "target_field": "date_of_birth",
            "source_column": "DOB",
            "source_columns": ["DOB"],
            "approved": True,
            "transformation_steps": [{"operation": "parse_date", "parameters": {}}],
            "failure_policy": "error",
        }

        flat = build_canonical_flat(source, [mapping], contract.target_fields, source_file_hash="abc")
        result = validate_canonical_frame(flat, {"date_of_birth": "DOB"}, contract.target_fields)

        self.assertIn("transformation_parse_date_failed", set(result.issues_df["issue_code"]))
        trace = flat.attrs["transformation_traces"][(2, "members", "date_of_birth")]
        self.assertEqual(trace[-1]["status"], "failed")

    def test_sparse_duplicates_coalesce_without_hiding_real_conflicts(self) -> None:
        fields = [
            TargetField("plans", "plan_id", True, "identifier", False, "plan_identifier", "Plan identifier."),
            TargetField("plans", "plan_name", True, "text", False, "plan_name", "Plan name."),
            TargetField("plans", "plan_type", False, "enum", True, "plan_type", "Plan type."),
        ]
        sparse_candidates = pd.DataFrame(
            [
                {"plan_id": "P1", "plan_name": "Silver", "plan_type": None},
                {"plan_id": "P1", "plan_name": "Silver", "plan_type": "PPO"},
                {"plan_id": "P1", "plan_name": "Silver", "plan_type": "PPO"},
            ]
        )

        merged, sparse_stats = _table_candidate_stats(sparse_candidates, fields, ["plan_id"])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged.iloc[0]["plan_type"], "PPO")
        self.assertEqual(sparse_stats["conflicting_duplicate_count"], 0)
        self.assertEqual(sparse_stats["exact_duplicate_count"], 2)

        conflicting_candidates = sparse_candidates.iloc[:2].copy()
        conflicting_candidates.loc[0, "plan_type"] = "HMO"
        _, conflict_stats = _table_candidate_stats(conflicting_candidates, fields, ["plan_id"])
        self.assertEqual(conflict_stats["conflicting_duplicate_count"], 2)


if __name__ == "__main__":
    unittest.main()
