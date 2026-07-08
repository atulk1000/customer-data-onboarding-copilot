from __future__ import annotations

import unittest

from onboarding.schema import TARGET_FIELDS_BY_KEY, target_schema_payload


class SchemaMetadataTests(unittest.TestCase):
    def test_target_fields_define_data_type_contract(self) -> None:
        for field in target_schema_payload():
            self.assertIn("data_type", field)
            self.assertIn("nullable", field)
            self.assertIn("validation_kind", field)
            self.assertTrue(field["data_type"])
            self.assertTrue(field["validation_kind"])

    def test_enum_fields_define_allowed_values(self) -> None:
        coverage_status = TARGET_FIELDS_BY_KEY[("member_coverage", "coverage_status")]
        self.assertEqual(coverage_status.data_type, "enum")
        self.assertIn("active", coverage_status.allowed_values)


if __name__ == "__main__":
    unittest.main()
