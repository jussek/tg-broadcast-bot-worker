"""Tests for Vercel's FastAPI routing configuration."""
import json
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VercelConfigurationTests(unittest.TestCase):
    def test_fastapi_entrypoint_routes_requests_without_a_function_rewrite(self):
        with (ROOT / "pyproject.toml").open("rb") as project_file:
            project = tomllib.load(project_file)
        config = json.loads((ROOT / "vercel.json").read_text())

        self.assertEqual(project["tool"]["vercel"]["entrypoint"], "api.index:app")
        self.assertNotIn("rewrites", config)


if __name__ == "__main__":
    unittest.main()
