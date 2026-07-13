from __future__ import annotations

import unittest

from sqlalchemy import create_engine

from onboarding.contracts import (
    ContractRegistryError,
    ContractValidationError,
    contract_checksum,
    default_contract_definition,
    list_contract_versions,
    save_contract_version,
    transition_contract_status,
)


class ContractTests(unittest.TestCase):
    def test_default_contract_is_stable_and_complete(self) -> None:
        definition = default_contract_definition()

        self.assertEqual(definition["contract_key"], "healthcare_eligibility")
        self.assertEqual(len(definition["tables"]), 3)
        self.assertEqual(contract_checksum(definition), contract_checksum(default_contract_definition()))

    def test_contract_registry_enforces_lifecycle_and_versions(self) -> None:
        engine = create_engine("sqlite:///:memory:", future=True)
        definition = default_contract_definition()
        definition["version"] = "1.3.0"

        draft = save_contract_version(engine, definition, actor="Schema Owner")
        published = transition_contract_status(
            engine,
            draft,
            new_status="published",
            actor="Schema Owner",
            comment="Approved for onboarding.",
        )
        retired = transition_contract_status(
            engine,
            published,
            new_status="retired",
            actor="Schema Owner",
            comment="Replaced by a later version.",
        )

        self.assertEqual(retired.status, "retired")
        self.assertEqual(list_contract_versions(engine)[0].version, "1.3.0")
        with self.assertRaises(ContractRegistryError):
            save_contract_version(engine, definition, actor="Schema Owner")

    def test_invalid_contract_is_rejected(self) -> None:
        definition = default_contract_definition()
        definition["tables"][0]["fields"][0]["data_type"] = "mystery"

        with self.assertRaises(ContractValidationError):
            save_contract_version(create_engine("sqlite:///:memory:", future=True), definition, actor="Owner")


if __name__ == "__main__":
    unittest.main()
