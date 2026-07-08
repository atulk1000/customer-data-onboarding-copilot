from __future__ import annotations

import unittest

from scripts.generate_demo_eligibility_file import ROW_COUNT, generate_rows


class DemoGeneratorTests(unittest.TestCase):
    def test_generates_exact_row_count(self) -> None:
        rows = generate_rows()
        self.assertEqual(len(rows), ROW_COUNT)
        self.assertEqual(len(rows[0]), 16)


if __name__ == "__main__":
    unittest.main()
