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

    def test_production_rejects_template_license_secret(self):
        errors = validate_config({
            "MG_ENV": "production",
            "MG_LICENSE_SECRET": "REPLACE_WITH_A_RANDOM_SECRET_OF_AT_LEAST_32_CHARACTERS",
            "MG_PUBLIC_URL": "https://business.example.com",
            "MG_HOST": "127.0.0.1",
        }, ROOT)
        self.assertTrue(any("real MG_LICENSE_SECRET" in error for error in errors))

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

    def test_production_rejects_public_bind_even_with_https_url(self):
        env = {
            "MG_ENV": "production",
            "MG_LICENSE_SECRET": "production-secret-with-more-than-32-characters",
            "MG_HOST": "0.0.0.0",
            "MG_PUBLIC_URL": "https://business.example.com",
            "MG_DATA_DIR": "/var/lib/mg-business",
            "MG_DB_PATH": "/var/lib/mg-business/data.sqlite3",
            "MG_BACKUP_DIR": "/var/backups/mg-business",
        }
        errors = validate_config(env, ROOT)
        self.assertTrue(any("must bind to loopback" in error for error in errors))

    def test_production_loopback_behind_https_proxy_passes(self):
        env = {
            "MG_ENV": "production",
            "MG_LICENSE_SECRET": "production-secret-with-more-than-32-characters",
            "MG_HOST": "127.0.0.1",
            "MG_PORT": "8080",
            "MG_PUBLIC_URL": "https://business.example.com",
            "MG_DATA_DIR": "/var/lib/mg-business",
            "MG_DB_PATH": "/var/lib/mg-business/data.sqlite3",
            "MG_BACKUP_DIR": "/var/backups/mg-business",
            "MG_TRUSTED_PROXIES": "127.0.0.1/32,::1/128",
        }
        self.assertEqual(validate_config(env, ROOT), [])

    def test_production_requires_trusted_reverse_proxy_for_client_ip_limits(self):
        env = {
            "MG_ENV": "production",
            "MG_LICENSE_SECRET": "production-secret-with-more-than-32-characters",
            "MG_HOST": "127.0.0.1",
            "MG_PUBLIC_URL": "https://business.example.com",
            "MG_DATA_DIR": "/var/lib/mg-business",
            "MG_DB_PATH": "/var/lib/mg-business/data.sqlite3",
            "MG_BACKUP_DIR": "/var/backups/mg-business",
        }
        errors = validate_config(env, ROOT)
        self.assertTrue(any("MG_TRUSTED_PROXIES" in error for error in errors))
        env["MG_TRUSTED_PROXIES"] = "127.0.0.1/32,::1/128"
        self.assertFalse(any("MG_TRUSTED_PROXIES" in error for error in validate_config(env, ROOT)))

    def test_unknown_environment_cannot_bypass_production_checks(self):
        errors = validate_config({
            "MG_ENV": "prod",
            "MG_HOST": "127.0.0.1",
        }, ROOT)
        self.assertTrue(any("MG_ENV must be one of" in error for error in errors))

    def test_public_https_url_rejects_credentials_and_invalid_ports(self):
        for url in ("https://user:password@business.example.com",
                    "https://business.example.com:99999",
                    "https://business.example.com#fragment"):
            errors = validate_config({
                "MG_HOST": "0.0.0.0",
                "MG_PUBLIC_URL": url,
                "MG_DATA_DIR": "/var/lib/mg-business",
                "MG_DB_PATH": "/var/lib/mg-business/data.sqlite3",
                "MG_BACKUP_DIR": "/var/backups/mg-business",
            }, ROOT)
            self.assertTrue(any("valid https://" in error for error in errors), url)

    def test_trusted_proxy_networks_are_validated(self):
        valid = validate_config({
            "MG_TRUSTED_PROXIES": "127.0.0.1,10.0.0.0/8,::1/128",
        }, ROOT)
        self.assertEqual(valid, [])
        invalid = validate_config({
            "MG_TRUSTED_PROXIES": "127.0.0.1,not-a-network",
        }, ROOT)
        self.assertTrue(any("MG_TRUSTED_PROXIES" in error for error in invalid))
        public_proxy = validate_config({
            "MG_TRUSTED_PROXIES": "0.0.0.0/0",
        }, ROOT)
        self.assertTrue(any("only private, loopback, or link-local" in error for error in public_proxy))

    def test_web_root_database_is_rejected(self):
        errors = validate_config({"MG_DB_PATH": str(ROOT / "database.sqlite3")}, ROOT)
        self.assertTrue(any("MG_DB_PATH" in error for error in errors))

    def test_backup_directory_cannot_contain_live_database(self):
        errors = validate_config({
            "MG_DATA_DIR": "/var/lib/mg-business",
            "MG_DB_PATH": "/var/lib/mg-business/data.sqlite3",
            "MG_BACKUP_DIR": "/var/lib/mg-business",
        }, ROOT)
        self.assertTrue(any("must not contain" in error for error in errors))

    def test_malformed_ipv6_public_url_is_reported_not_raised(self):
        errors = validate_config({
            "MG_HOST": "0.0.0.0",
            "MG_PUBLIC_URL": "https://[invalid",
            "MG_DATA_DIR": "/var/lib/mg-business",
            "MG_DB_PATH": "/var/lib/mg-business/data.sqlite3",
            "MG_BACKUP_DIR": "/var/backups/mg-business",
        }, ROOT)
        self.assertTrue(any("valid https://" in error for error in errors))

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
