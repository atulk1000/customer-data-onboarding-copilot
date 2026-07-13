from __future__ import annotations

import unittest

import pandas as pd

from onboarding.contracts import contract_from_definition
from onboarding.mapping_quality import apply_mapping_type_alignment
from onboarding.profiler import profile_dataframe
from onboarding.rules_mapper import generate_rules_based_mappings
from onboarding.transform import approved_source_columns_by_target_field, build_canonical_flat, transform_outputs
from onboarding.validation import validate_canonical_frame


class DynamicContractTests(unittest.TestCase):
    def test_non_healthcare_contract_drives_mapping_validation_and_output(self) -> None:
        contract = contract_from_definition(
            {
                "contract_key": "customer_master",
                "name": "Customer Master",
                "domain": "customer",
                "version": "1.0.0",
                "tables": [
                    {
                        "name": "customers",
                        "primary_key": ["customer_id"],
                        "business_key": ["customer_id"],
                        "foreign_keys": [],
                        "fields": [
                            {
                                "name": "customer_id",
                                "data_type": "identifier",
                                "required": True,
                                "nullable": False,
                                "aliases": ["customer number"],
                                "validation_kind": "required",
                            },
                            {
                                "name": "full_name",
                                "data_type": "text",
                                "required": True,
                                "nullable": False,
                                "aliases": ["customer name"],
                                "validation_kind": "required",
                            },
                            {
                                "name": "signup_date",
                                "data_type": "date",
                                "required": True,
                                "nullable": False,
                                "aliases": ["joined date"],
                                "validation_kind": "required",
                            },
                        ],
                    }
                ],
                "reconciliation_policy": {"max_reject_rate": 0.1},
            }
        )
        source = pd.DataFrame(
            {
                "Customer Number": ["C-100", "C-200"],
                "Customer Name": ["Ada Lovelace", "Grace Hopper"],
                "Joined Date": ["2025-01-10", "2025-02-20"],
            }
        )
        profiles = profile_dataframe(source)
        mappings = apply_mapping_type_alignment(
            generate_rules_based_mappings(profiles, contract.target_fields),
            profiles,
            contract.target_fields,
        )
        for mapping in mappings:
            if mapping.get("source_column"):
                mapping["approved"] = True
                mapping["transformation_approved"] = True

        flat = build_canonical_flat(source, mappings, contract.target_fields, source_file_hash="dynamic")
        validation = validate_canonical_frame(
            flat,
            approved_source_columns_by_target_field(mappings),
            contract.target_fields,
        )
        outputs = transform_outputs(
            validation.normalized_df,
            source,
            validation.issues_df,
            mappings,
            target_schema=contract.target_fields,
            contract=contract,
            source_file_hash="dynamic",
        )

        self.assertEqual(validation.accepted_row_count, 2)
        self.assertIn("customers", outputs.tables)
        self.assertEqual(list(outputs.tables["customers"]["customer_id"]), ["C-100", "C-200"])


if __name__ == "__main__":
    unittest.main()
