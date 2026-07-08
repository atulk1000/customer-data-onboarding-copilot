from __future__ import annotations

import unittest

import pandas as pd

from onboarding.profiler import normalize_column_name, profile_dataframe


class ProfilerTests(unittest.TestCase):
    def test_normalize_column_name(self) -> None:
        self.assertEqual(normalize_column_name("Member/Number"), "member number")
        self.assertEqual(normalize_column_name("MEMBER_NO"), "member number")
        self.assertEqual(normalize_column_name("Effective-Date"), "effective date")

    def test_profile_detects_patterns(self) -> None:
        df = pd.DataFrame(
            {
                "DOB": ["1988-01-01", "1977-02-03"],
                "Email Address": ["a@example.com", "b@example.com"],
                "Status": ["Active", "Termed"],
            }
        )
        profiles = {profile["column_name"]: profile for profile in profile_dataframe(df)}
        self.assertEqual(profiles["DOB"]["inferred_type"], "date")
        self.assertGreaterEqual(profiles["Email Address"]["email_pattern_rate"], 0.9)
        self.assertIn("coverage_status", profiles["Status"]["known_enum_matches"])


if __name__ == "__main__":
    unittest.main()
