from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from onboarding.mapping_templates import (
    apply_mapping_template,
    list_mapping_templates,
    load_mapping_template,
    save_mapping_template,
)


class MappingTemplateTests(unittest.TestCase):
    def test_saves_and_loads_template_without_auto_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            template_dir = Path(tmp)
            saved = save_mapping_template(
                template_name="Acme eligibility",
                schema_name="Healthcare Eligibility Canonical v1",
                schema_version="1.0.0",
                source_columns=["Member Number"],
                mappings=[
                    {
                        "target_table": "members",
                        "target_field": "member_id",
                        "source_column": "Member Number",
                        "approved": True,
                        "review_flags": [],
                    }
                ],
                template_dir=template_dir,
            )

            templates = list_mapping_templates(template_dir)
            loaded = load_mapping_template(saved["file_name"], template_dir)
            applied = apply_mapping_template(loaded, ["Member Number"])

            self.assertEqual(templates[0]["template_name"], "Acme eligibility")
            self.assertEqual(applied[0]["source_column"], "Member Number")
            self.assertFalse(applied[0]["approved"])
            self.assertTrue(applied[0]["needs_review"])

    def test_missing_template_source_column_is_unmapped(self) -> None:
        template = {
            "mappings": [
                {
                    "target_table": "members",
                    "target_field": "member_id",
                    "source_column": "Member Number",
                    "review_flags": [],
                }
            ]
        }

        applied = apply_mapping_template(template, ["Other ID"])

        self.assertEqual(applied[0]["source_column"], "")
        self.assertIn("template_source_missing", applied[0]["review_flags"])


if __name__ == "__main__":
    unittest.main()
