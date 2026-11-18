import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_deployment_file(relative_path: str) -> str:
    path = ROOT / relative_path
    if not path.is_file():
        raise AssertionError(f"Required deployment file is missing: {relative_path}")
    return path.read_text(encoding="utf-8")


class DeploymentContractTests(unittest.TestCase):
    def test_systemd_service_runs_unprivileged_and_sandboxed(self):
        service = read_deployment_file("deploy/systemd/mg-business.service")
        required = (
            "User=mg-business",
            "Group=mg-business",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
            "RestrictSUIDSGID=true",
            "CapabilityBoundingSet=",
            "UMask=0077",
            "Restart=on-failure",
        )
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, service)
        self.assertIn(
            "ReadWritePaths=/var/lib/mg-business /var/backups/mg-business",
            service,
        )
        self.assertNotIn("AmbientCapabilities=", service)

    def test_service_startup_must_pass_production_preflight_first(self):
        startup = read_deployment_file("deploy/systemd/start.sh")
        self.assertIn("set -eu", startup)
        preflight = startup.find("deployment_preflight.py --production")
        server = startup.find("exec /usr/bin/python3 server.py")
        self.assertGreaterEqual(preflight, 0)
        self.assertGreater(server, preflight)

    def test_production_environment_template_keeps_backend_on_loopback(self):
        template = read_deployment_file("deploy/systemd/mg-business.env.example")
        self.assertIn("MG_ENV=production", template)
        self.assertIn("MG_HOST=127.0.0.1", template)
        self.assertIn("MG_PORT=8080", template)
        self.assertIn("MG_TRUSTED_PROXIES=127.0.0.1/32,::1/128", template)
        self.assertIn("MG_BACKUP_ENCRYPTION_KEY=", template)
        self.assertIn("MG_MAX_HTTP_WORKERS=64", template)
        self.assertIn("MG_REQUEST_TIMEOUT_SECONDS=15", template)

    def test_nginx_template_terminates_tls_and_proxies_only_to_loopback(self):
        nginx = read_deployment_file("deploy/nginx/mg-business.conf.example")
        self.assertIn("listen 443 ssl;", nginx)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", nginx)
        self.assertIn("return 301 https://business.example.com$request_uri;", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:8080;", nginx)
        self.assertIn("limit_conn mg_per_ip 30;", nginx)
        self.assertIn("limit_req zone=mg_api_per_ip", nginx)
        self.assertIn("client_max_body_size 50m;", nginx)
        self.assertNotIn("proxy_pass http://0.0.0.0", nginx)


    def test_restore_runbook_requires_verification_and_staging_drill(self):
        runbook = read_deployment_file("deploy/PRODUCTION_RUNBOOK.md")
        self.assertIn("backup_manager.py --verify", runbook)
        self.assertIn("separate staging host", runbook)
        self.assertIn("Never experiment with restore commands against the only production database.", runbook)
        self.assertIn("stop the service", runbook)
        self.assertIn("MG_BACKUP_ENCRYPTION_KEY", runbook)

    def test_upgrade_runbook_requires_backup_before_fast_forward(self):
        runbook = read_deployment_file("deploy/PRODUCTION_RUNBOOK.md")
        verify_backup = runbook.index("Verify that the latest encrypted backup exists")
        stop_service = runbook.index("Stop the service", verify_backup)
        fetch_release = runbook.index("Fetch the release and fast-forward only", stop_service)
        self.assertLess(verify_backup, stop_service)
        self.assertLess(stop_service, fetch_release)
        self.assertIn("Do not deploy an unreviewed working tree", runbook)
        self.assertIn("Do not roll application code back across a database migration", runbook)

    def test_release_gate_does_not_equate_green_ci_with_production_approval(self):
        runbook = read_deployment_file("deploy/PRODUCTION_RUNBOOK.md")
        self.assertIn("necessary, not sufficient", runbook)
        self.assertIn("independent security review and penetration test", runbook)
        self.assertIn("restore drills and monitoring/alerting", runbook)
        self.assertIn("load and browser workflows", runbook)


if __name__ == "__main__":
    unittest.main()
