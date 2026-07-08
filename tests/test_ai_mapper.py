from __future__ import annotations

import unittest

from onboarding.ai_mapper import (
    AIMapperValidationError,
    OpenAIConfigurationError,
    _response_create_kwargs,
    validate_ai_mapping_response,
)


class AIMapperValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = [
            {
                "column_name": "Member Number",
                "inferred_type": "identifier",
                "null_rate": 0.0,
                "unique_rate": 1.0,
                "sample_values": ["MEM001", "MEM002"],
                "top_values": {},
                "date_parse_rate": 0.0,
                "email_pattern_rate": 0.0,
                "phone_pattern_rate": 0.0,
                "known_enum_matches": {},
            },
            {
                "column_name": "DOB",
                "inferred_type": "date",
                "null_rate": 0.0,
                "unique_rate": 1.0,
                "sample_values": ["1988-01-01", "1977-02-03"],
                "top_values": {},
                "date_parse_rate": 1.0,
                "email_pattern_rate": 0.0,
                "phone_pattern_rate": 0.0,
                "known_enum_matches": {},
            },
            {
                "column_name": "Status",
                "inferred_type": "enum",
                "null_rate": 0.0,
                "unique_rate": 0.1,
                "sample_values": ["Active", "Termed"],
                "top_values": {"Active": 8, "Termed": 2},
                "date_parse_rate": 0.0,
                "email_pattern_rate": 0.0,
                "phone_pattern_rate": 0.0,
                "known_enum_matches": {"coverage_status": ["active", "terminated"]},
            },
        ]

    def test_validates_ai_mapping_response(self) -> None:
        parsed = {
            "mappings": [
                {
                    "target_table": "members",
                    "target_field": "date_of_birth",
                    "source_column": "DOB",
                    "confidence": 97,
                    "needs_review": False,
                    "review_flags": [],
                    "rationale": "DOB maps to date_of_birth.",
                    "transformation_hint": "Parse as date.",
                }
            ],
            "unmapped_required_fields": [],
            "ambiguous_mappings": [],
        }
        mappings = validate_ai_mapping_response(parsed, self.profiles)
        self.assertEqual(mappings[0]["source_column"], "DOB")
        self.assertTrue(mappings[0]["required"])
        self.assertEqual(mappings[0]["target_data_type"], "date")
        self.assertEqual(mappings[0]["target_validation_kind"], "date_of_birth")
        self.assertEqual(mappings[0]["source_inferred_type"], "date")
        self.assertEqual(mappings[0]["type_alignment"], "aligned")

    def test_rejects_unknown_source_column(self) -> None:
        parsed = {
            "mappings": [
                {
                    "target_table": "members",
                    "target_field": "date_of_birth",
                    "source_column": "Birthdate From Nowhere",
                    "confidence": 97,
                    "needs_review": False,
                    "review_flags": [],
                    "rationale": "Bad source.",
                    "transformation_hint": "Parse as date.",
                }
            ],
            "unmapped_required_fields": [],
            "ambiguous_mappings": [],
        }
        with self.assertRaises(AIMapperValidationError):
            validate_ai_mapping_response(parsed, self.profiles)

    def test_rejects_unknown_target_field(self) -> None:
        parsed = {
            "mappings": [
                {
                    "target_table": "members",
                    "target_field": "favorite_color",
                    "source_column": "DOB",
                    "confidence": 80,
                    "needs_review": True,
                    "review_flags": ["unknown"],
                    "rationale": "Bad target.",
                    "transformation_hint": "",
                }
            ],
            "unmapped_required_fields": [],
            "ambiguous_mappings": [],
        }
        with self.assertRaises(AIMapperValidationError):
            validate_ai_mapping_response(parsed, self.profiles)

    def test_gpt5_request_uses_reasoning_effort(self) -> None:
        kwargs = _response_create_kwargs("gpt-5-mini")
        self.assertEqual(kwargs["reasoning"]["effort"], "low")
        self.assertNotIn("temperature", kwargs)

    def test_legacy_request_uses_temperature(self) -> None:
        kwargs = _response_create_kwargs("gpt-4.1-mini")
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertNotIn("reasoning", kwargs)

    def test_invalid_reasoning_effort_is_rejected(self) -> None:
        import os

        previous = os.environ.get("OPENAI_REASONING_EFFORT")
        os.environ["OPENAI_REASONING_EFFORT"] = "extreme"
        try:
            with self.assertRaises(OpenAIConfigurationError):
                _response_create_kwargs("gpt-5-mini")
        finally:
            if previous is None:
                os.environ.pop("OPENAI_REASONING_EFFORT", None)
            else:
                os.environ["OPENAI_REASONING_EFFORT"] = previous


if __name__ == "__main__":
    unittest.main()
