from __future__ import annotations

import unittest

import pandas as pd

from onboarding.mapping_quality import apply_mapping_type_alignment, blocking_mapping_alignment_issues
from onboarding.profiler import profile_dataframe


class MappingQualityTests(unittest.TestCase):
    def test_flags_approved_target_type_mismatch(self) -> None:
        profiles = profile_dataframe(
            pd.DataFrame(
                {
                    "Email Address": ["ana@example.com", "sam@example.com", "priya@example.com"],
                }
            )
        )
        mappings = [
            {
                "target_table": "members",
                "target_field": "date_of_birth",
                "source_column": "Email Address",
                "approved": True,
                "needs_review": False,
                "review_flags": [],
            }
        ]

        annotated = apply_mapping_type_alignment(mappings, profiles)

        self.assertEqual(annotated[0]["target_data_type"], "date")
        self.assertEqual(annotated[0]["source_inferred_type"], "email")
        self.assertEqual(annotated[0]["type_alignment"], "mismatch")
        self.assertTrue(annotated[0]["needs_review"])
        self.assertIn("target_type_mismatch", annotated[0]["review_flags"])
        self.assertEqual(len(blocking_mapping_alignment_issues(annotated)), 1)


if __name__ == "__main__":
    unittest.main()
