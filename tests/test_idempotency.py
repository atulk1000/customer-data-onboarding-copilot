from __future__ import annotations

import unittest
from datetime import date

import pandas as pd
from sqlalchemy import create_engine, text

from onboarding.idempotency import build_import_replay_check, source_dataframe_fingerprint
from onboarding.transform import transform_outputs


class IdempotencyTests(unittest.TestCase):
    def test_source_fingerprint_is_stable_and_sensitive_to_changes(self) -> None:
        source = pd.DataFrame({"Member Number": ["MEM001"], "Plan Code": ["PPO-100"]})
        same_source = pd.DataFrame({"Member Number": ["MEM001"], "Plan Code": ["PPO-100"]})
        changed_source = pd.DataFrame({"Member Number": ["MEM002"], "Plan Code": ["PPO-100"]})

        self.assertEqual(source_dataframe_fingerprint(source), source_dataframe_fingerprint(same_source))
        self.assertNotEqual(source_dataframe_fingerprint(source), source_dataframe_fingerprint(changed_source))

    def test_replay_check_detects_previous_import_run(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(text("""
                    CREATE TABLE import_runs (
                        id INTEGER PRIMARY KEY,
                        file_name TEXT,
                        completed_at TEXT,
                        status TEXT,
                        source_file_hash TEXT
                    )
                    """))
            conn.execute(text("""
                    INSERT INTO import_runs (id, file_name, completed_at, status, source_file_hash)
                    VALUES (42, 'demo.csv', '2026-07-07 10:00:00', 'published', 'abc123')
                    """))

        check = build_import_replay_check(engine, "abc123")
        fresh_check = build_import_replay_check(engine, "newhash")

        self.assertTrue(check["is_replay"])
        self.assertEqual(check["previous_import_run_id"], 42)
        self.assertFalse(fresh_check["is_replay"])

    def test_coverage_ids_are_stable_for_same_business_keys(self) -> None:
        base_rows = [
            {
                "source_row_number": 2,
                "member_id": "MEM001",
                "first_name": "Ana",
                "last_name": "Patel",
                "date_of_birth": date(1988, 1, 1),
                "gender": "female",
                "email": "ana@example.com",
                "phone": "555-111-2222",
                "plan_id": "PPO-100",
                "plan_name": "Silver PPO",
                "plan_type": "PPO",
                "carrier_name": "Acme Health",
                "coverage_start_date": date(2024, 1, 1),
                "coverage_end_date": None,
                "coverage_status": "active",
                "relationship_to_subscriber": "self",
                "subscriber_id": "MEM001",
            },
            {
                "source_row_number": 3,
                "member_id": "MEM002",
                "first_name": "Sam",
                "last_name": "Lee",
                "date_of_birth": date(1980, 2, 2),
                "gender": "male",
                "email": "sam@example.com",
                "phone": "555-222-3333",
                "plan_id": "HMO-200",
                "plan_name": "Basic HMO",
                "plan_type": "HMO",
                "carrier_name": "Acme Health",
                "coverage_start_date": date(2024, 1, 1),
                "coverage_end_date": None,
                "coverage_status": "active",
                "relationship_to_subscriber": "self",
                "subscriber_id": "MEM002",
            },
        ]
        source = pd.DataFrame({"raw": ["a", "b"]})
        issues = pd.DataFrame()
        outputs = transform_outputs(pd.DataFrame(base_rows), source, issues)
        reversed_outputs = transform_outputs(pd.DataFrame(list(reversed(base_rows))), source, issues)

        self.assertEqual(
            set(outputs.member_coverage["coverage_id"]),
            set(reversed_outputs.member_coverage["coverage_id"]),
        )


if __name__ == "__main__":
    unittest.main()
