import unittest
from pathlib import Path

from deployment_preflight import validate_config


ROOT = Path(__file__).resolve().parents[1]


class DeploymentPreflightTests(unittest.TestCase):
    def test_safe_local_defaults_pass(self):
        self.assertEqual(validate_config({}, ROOT), [])

    def test_production_requires_secret_and_https(self):
        errors = validate_config({"MG_ENV": "production"}, ROOT)
        self.assertTrue(any("MG_LICENSE_SECRET" in error for error in errors))
        self.assertTrue(any("https://" in error for error in errors))

    def test_public_bind_requires_https_url(self):
        errors = validate_config({"MG_HOST": "0.0.0.0"}, ROOT)
        self.assertTrue(any("non-loopback" in error for error in errors))

    def test_public_bind_with_https_and_safe_paths_passes(self):
        env = {
            "MG_HOST": "0.0.0.0",
            "MG_PUBLIC_URL": "https://business.example.com",
            "MG_DATA_DIR": "/var/lib/mg-business",
            "MG_DB_PATH": "/var/lib/mg-business/data.sqlite3",
            "MG_BACKUP_DIR": "/var/backups/mg-business",
        }
        self.assertEqual(validate_config(env, ROOT), [])

    def test_web_root_database_is_rejected(self):
        errors = validate_config({"MG_DB_PATH": str(ROOT / "database.sqlite3")}, ROOT)
        self.assertTrue(any("MG_DB_PATH" in error for error in errors))

    def test_invalid_port_and_backup_policy_are_rejected(self):
        errors = validate_config({
            "MG_PORT": "70000",
            "MG_BACKUP_INTERVAL_HOURS": "0",
            "MG_BACKUP_RETENTION": "0",
        }, ROOT)
        self.assertTrue(any("MG_PORT" in error for error in errors))
        self.assertTrue(any("MG_BACKUP_INTERVAL_HOURS" in error for error in errors))
        self.assertTrue(any("MG_BACKUP_RETENTION" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
