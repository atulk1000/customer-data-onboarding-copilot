from __future__ import annotations

import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


class AppSmokeTests(unittest.TestCase):
    def test_initial_target_screen_renders_without_exception(self) -> None:
        app_path = Path(__file__).resolve().parents[1] / "app.py"

        app = AppTest.from_file(str(app_path)).run(timeout=20)

        self.assertFalse(app.exception)
        self.assertEqual(app.title[0].value, "Customer Data Onboarding Copilot")


if __name__ == "__main__":
    unittest.main()
