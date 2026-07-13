from __future__ import annotations

import unittest
from datetime import date

import pandas as pd
from sqlalchemy import create_engine, text

from onboarding.contracts import default_contract
from onboarding.reconciliation import build_pre_publish_reconciliation, build_transform_reconciliation
from onboarding.transform import TransformOutputs
from onboarding.validation import ValidationResult


class ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = default_contract()
        self.members = pd.DataFrame(
            [
                {
                    "member_id": "M1",
                    "first_name": "Ana",
                    "last_name": "Patel",
                    "date_of_birth": date(1988, 1, 1),
                    "gender": "female",
                    "email": "ana@example.com",
                    "phone": "555-111-2222",
                },
                {
                    "member_id": "M2",
                    "first_name": "Sam",
                    "last_name": "Lee",
                    "date_of_birth": date(1980, 2, 2),
                    "gender": "male",
                    "email": "sam@example.com",
                    "phone": "555-222-3333",
                },
            ]
        )
        self.plans = pd.DataFrame(
            [{"plan_id": "P1", "plan_name": "Silver", "plan_type": "PPO", "carrier_name": "Acme"}]
        )
        self.coverage = pd.DataFrame(
            [
                {
                    "coverage_id": "C1",
                    "member_id": "M1",
                    "plan_id": "P1",
                    "coverage_start_date": date(2024, 1, 1),
                    "coverage_end_date": None,
                    "coverage_status": "active",
                    "relationship_to_subscriber": "self",
                    "subscriber_id": "M1",
                },
                {
                    "coverage_id": "C2",
                    "member_id": "M2",
                    "plan_id": "P1",
                    "coverage_start_date": date(2024, 1, 1),
                    "coverage_end_date": None,
                    "coverage_status": "active",
                    "relationship_to_subscriber": "self",
                    "subscriber_id": "M2",
                },
            ]
        )
        self.outputs = TransformOutputs(
            members=self.members,
            plans=self.plans,
            member_coverage=self.coverage,
            rejected_rows=pd.DataFrame(),
            field_lineage=pd.DataFrame(),
            tables={"members": self.members, "plans": self.plans, "member_coverage": self.coverage},
        )
        self.validation = ValidationResult(
            normalized_df=pd.DataFrame({"source_row_number": [2, 3]}),
            issues=[],
        )

    def test_transform_reconciliation_accounts_for_rows_and_relationships(self) -> None:
        result = build_transform_reconciliation(self.validation, self.outputs, contract=self.contract)

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.source_metrics["source_rows"], 2)
        self.assertEqual(result.table_metrics["member_coverage"]["orphan_count"], 0)

    def test_database_forecast_classifies_insert_update_and_unchanged(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(text("""
                    CREATE TABLE members (
                        member_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT,
                        date_of_birth DATE, gender TEXT, email TEXT, phone TEXT
                    )
                    """))
            conn.execute(text("""
                    CREATE TABLE plans (
                        plan_id TEXT PRIMARY KEY, plan_name TEXT, plan_type TEXT, carrier_name TEXT
                    )
                    """))
            conn.execute(text("""
                    CREATE TABLE member_coverage (
                        coverage_id TEXT PRIMARY KEY, member_id TEXT, plan_id TEXT,
                        coverage_start_date DATE, coverage_end_date DATE, coverage_status TEXT,
                        relationship_to_subscriber TEXT, subscriber_id TEXT
                    )
                    """))
            conn.execute(text("""
                    INSERT INTO members VALUES
                    ('M1', 'Ana', 'Patel', '1988-01-01', 'female', 'ana@example.com', '555-111-2222')
                    """))
            conn.execute(text("INSERT INTO plans VALUES ('P1', 'Old Name', 'PPO', 'Acme')"))

        result = build_pre_publish_reconciliation(
            engine,
            self.validation,
            self.outputs,
            contract=self.contract,
        )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.table_metrics["members"]["expected_insert_count"], 1)
        self.assertEqual(result.table_metrics["members"]["expected_unchanged_count"], 1)
        self.assertEqual(result.table_metrics["plans"]["expected_update_count"], 1)
        self.assertEqual(result.table_metrics["member_coverage"]["expected_insert_count"], 2)


if __name__ == "__main__":
    unittest.main()
