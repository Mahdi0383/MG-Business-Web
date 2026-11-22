import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProductionDeploymentAssetTests(unittest.TestCase):
    def test_nginx_proxy_is_tls_only_and_overwrites_real_ip(self):
        config = (ROOT / "deploy/nginx/mg-business.conf.example").read_text(encoding="utf-8")
        self.assertIn("return 301 https://business.example.com$request_uri;", config)
        self.assertIn("if ($host != business.example.com) { return 444; }", config)
        self.assertNotIn("return 301 https://$host$request_uri;", config)
        self.assertIn("listen 443 ssl;", config)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", config)
        self.assertIn("proxy_pass http://127.0.0.1:8080;", config)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", config)
        self.assertNotIn("http2 on;", config)
        self.assertNotIn("proxy_pass http://0.0.0.0", config)

    def test_nginx_limits_large_bodies_to_backup_restore_route(self):
        config = (ROOT / "deploy/nginx/mg-business.conf.example").read_text(encoding="utf-8")
        self.assertIn("client_max_body_size 2m;", config)
        self.assertIn("location = /api/backup/restore {\n        client_max_body_size 50m;", config)
        self.assertEqual(config.count("client_max_body_size 50m;"), 1)

    def test_nginx_buffers_client_bodies_before_forwarding_to_app_workers(self):
        config = (ROOT / "deploy/nginx/mg-business.conf.example").read_text(encoding="utf-8")
        self.assertEqual(config.count("proxy_request_buffering on;"), 3)

    def test_systemd_service_has_restrictive_runtime_sandbox(self):
        unit = (ROOT / "deploy/systemd/mg-business.service").read_text(encoding="utf-8")
        for directive in (
            "User=mg-business",
            "Group=mg-business",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "RestrictSUIDSGID=true",
            "UMask=0077",
            "ReadWritePaths=/var/lib/mg-business /var/backups/mg-business",
        ):
            self.assertIn(directive, unit)

    def test_systemd_startup_runs_preflight_before_server(self):
        unit = (ROOT / "deploy/systemd/mg-business.service").read_text(encoding="utf-8")
        startup = (ROOT / "deploy/systemd/start.sh").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/bin/sh /opt/mg-business/deploy/systemd/start.sh", unit)
        self.assertLess(startup.index("deployment_preflight.py --production"),
                        startup.index("exec /usr/bin/python3 server.py"))

    def test_production_runbook_covers_tls_backup_restore_and_release_gates(self):
        runbook = (ROOT / "deploy/PRODUCTION_RUNBOOK.md").read_text(encoding="utf-8")
        for required in (
            "MG_TRUSTED_PROXIES",
            "MG_BACKUP_ENCRYPTION_KEY",
            "certbot renew --dry-run",
            "/api/ready",
            "backup_manager.py --verify",
            "restore drills",
            "independent security review",
            "penetration test",
        ):
            self.assertIn(required, runbook)

    def test_production_environment_template_keeps_backend_private(self):
        env = (ROOT / "deploy/systemd/mg-business.env.example").read_text(encoding="utf-8")
        self.assertIn("MG_ENV=production", env)
        self.assertIn("MG_HOST=127.0.0.1", env)
        self.assertIn("MG_PUBLIC_URL=https://business.example.com", env)
        self.assertIn("MG_TRUSTED_PROXIES=127.0.0.1/32,::1/128", env)
        self.assertIn("REPLACE_WITH_A_RANDOM_SECRET", env)


if __name__ == "__main__":
    unittest.main()
