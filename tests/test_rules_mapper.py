from __future__ import annotations

import unittest

import pandas as pd

from onboarding.profiler import profile_dataframe
from onboarding.rules_mapper import generate_rules_based_mappings


class RulesMapperTests(unittest.TestCase):
    def test_rules_mapper_scores_obvious_columns(self) -> None:
        df = pd.DataFrame(
            {
                "Member Number": ["MEM001", "MEM002", "MEM003"],
                "First": ["Ana", "Sam", "Priya"],
                "Last": ["Patel", "Lee", "Khan"],
                "DOB": ["1988-01-01", "1977-02-03", "1990-04-05"],
                "Plan Code": ["PPO-100", "HMO-200", "PPO-100"],
                "Plan Name": ["Silver PPO", "Basic HMO", "Silver PPO"],
                "Effective Date": ["2024-01-01", "2024-01-01", "2024-01-01"],
                "Status": ["Active", "Termed", "Pending"],
                "Relation": ["Self", "Self", "Self"],
            }
        )
        mappings = generate_rules_based_mappings(profile_dataframe(df))
        by_field = {(mapping["target_table"], mapping["target_field"]): mapping for mapping in mappings}
        self.assertEqual(by_field[("members", "date_of_birth")]["source_column"], "DOB")
        self.assertGreaterEqual(by_field[("members", "date_of_birth")]["confidence"], 85)
        self.assertEqual(by_field[("members", "date_of_birth")]["target_data_type"], "date")
        self.assertEqual(by_field[("members", "email")]["target_data_type"], "email")
        self.assertEqual(by_field[("member_coverage", "coverage_status")]["source_column"], "Status")
        self.assertTrue(by_field[("member_coverage", "coverage_status")]["needs_review"])


if __name__ == "__main__":
    unittest.main()
