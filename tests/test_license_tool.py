import base64
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LicenseToolTests(unittest.TestCase):
    def run_tool(self, modules, license_id=None):
        env = dict(os.environ, MG_LICENSE_SECRET="test-secret-with-at-least-thirty-two-characters")
        command = [sys.executable, str(ROOT / "license_tool.py"), "--modules", modules]
        if license_id is not None:
            command.extend(["--license-id", license_id])
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_issuer_expands_declared_module_dependencies(self):
        result = self.run_tool("reports")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload_part = result.stdout.strip().split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4)))
        self.assertEqual(payload["modules"], ["accounting", "reports"])

    def test_issuer_adds_accounting_for_cafe_license(self):
        result = self.run_tool("cafe")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload_part = result.stdout.strip().split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4)))
        self.assertEqual(payload["modules"], ["accounting", "cafe"])

    def test_issuer_rejects_empty_or_overlong_license_identifier(self):
        for license_id in ("", "x" * 101):
            with self.subTest(length=len(license_id)):
                result = self.run_tool("cafe", license_id)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("License ID must contain between 1 and 100 characters", result.stderr)

    def test_issuer_rejects_planned_unimplemented_vertical(self):
        result = self.run_tool("hotel")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unknown module keys: hotel", result.stderr)

    def test_issuer_rejects_duplicate_module_keys_instead_of_silently_deduplicating(self):
        result = self.run_tool("cafe,cafe")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Duplicate module keys: cafe", result.stderr)

    def test_issuer_rejects_empty_module_list_entries(self):
        for value in ("cafe,,accounting", "cafe,"):
            with self.subTest(value=value):
                result = self.run_tool(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Module list must not contain empty entries", result.stderr)


if __name__ == "__main__":
    unittest.main()
