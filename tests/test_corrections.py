from __future__ import annotations

import unittest

import pandas as pd

from onboarding.corrections import (
    add_correction_columns,
    apply_correction_overlays,
    correction_audit_rows,
    validate_correction_upload,
)
from onboarding.idempotency import source_dataframe_fingerprint


class CorrectionTests(unittest.TestCase):
    def test_correction_csv_round_trip_preserves_original(self) -> None:
        source = pd.DataFrame({"DOB": ["bad-date"], "Status": ["Active"]})
        source_hash = source_dataframe_fingerprint(source)
        rejected = pd.DataFrame(
            [
                {
                    "source_row_number": 2,
                    "row_status": "rejected",
                    "error_count": 1,
                    "error_codes": "date_of_birth_missing_or_invalid",
                    "error_target_fields": "date_of_birth",
                    "error_source_columns": "DOB",
                    "errors": "DOB is invalid.",
                    "warning_count": 0,
                    "warning_codes": "",
                    "warning_target_fields": "",
                    "warning_source_columns": "",
                    "warnings": "",
                    "original__DOB": "bad-date",
                    "original__Status": "Active",
                }
            ]
        )
        queue = add_correction_columns(rejected, source, source_hash)
        edited = queue.copy()
        edited.loc[0, "corrected__DOB"] = "1988-01-01"
        edited.loc[0, "correction_comment"] = "Confirmed with source owner."

        result = validate_correction_upload(edited, queue)
        corrected = apply_correction_overlays(source, source_hash, result.overlays)
        audit = correction_audit_rows(result.overlays, source, corrected_by="Reviewer")

        self.assertTrue(result.is_valid)
        self.assertEqual(source.loc[0, "DOB"], "bad-date")
        self.assertEqual(corrected.loc[0, "DOB"], "1988-01-01")
        self.assertEqual(audit[0]["original_value"], "bad-date")

    def test_fingerprint_mismatch_is_rejected(self) -> None:
        source = pd.DataFrame({"DOB": ["bad-date"]})
        queue = add_correction_columns(
            pd.DataFrame(
                [
                    {
                        "source_row_number": 2,
                        "error_codes": "bad_date",
                        "error_source_columns": "DOB",
                        "original__DOB": "bad-date",
                    }
                ]
            ),
            source,
            source_dataframe_fingerprint(source),
        )
        edited = queue.copy()
        edited.loc[0, "original_row_fingerprint"] = "tampered"
        edited.loc[0, "corrected__DOB"] = "1988-01-01"
        edited.loc[0, "correction_comment"] = "Fix."

        result = validate_correction_upload(edited, queue)

        self.assertFalse(result.is_valid)
        self.assertIn("fingerprint mismatch", " ".join(result.errors))


if __name__ == "__main__":
    unittest.main()
