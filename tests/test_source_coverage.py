from __future__ import annotations

import unittest

import pandas as pd

from onboarding.profiler import profile_dataframe
from onboarding.source_coverage import build_source_coverage, source_coverage_summary, unused_source_columns


class SourceCoverageTests(unittest.TestCase):
    def test_builds_unused_source_column_audit(self) -> None:
        source = pd.DataFrame(
            {
                "Member Number": ["MEM001", "MEM002"],
                "Legacy ID": ["L001", "L002"],
            }
        )
        mappings = [
            {
                "target_table": "members",
                "target_field": "member_id",
                "source_column": "Member Number",
                "approved": True,
            }
        ]

        rows = build_source_coverage(list(source.columns), profile_dataframe(source), mappings)
        by_column = {row["source_column"]: row for row in rows}

        self.assertEqual(by_column["Member Number"]["coverage_status"], "approved_mapped")
        self.assertEqual(by_column["Legacy ID"]["coverage_status"], "unused")
        self.assertIn("Review before ignoring", by_column["Legacy ID"]["review_recommendation"])
        self.assertEqual(unused_source_columns(rows), ["Legacy ID"])
        self.assertEqual(source_coverage_summary(rows)["unused_columns"], 1)


if __name__ == "__main__":
    unittest.main()
