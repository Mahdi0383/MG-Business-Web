import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NGINX_DIR = ROOT / "deploy" / "nginx"
LIMITS = NGINX_DIR / "mg-business-limits.conf.example"
VHOST = NGINX_DIR / "mg-business.conf.example"
RUNBOOK = ROOT / "deploy" / "PRODUCTION_RUNBOOK.md"


class NginxDeploymentConfigTests(unittest.TestCase):
    def test_shared_limit_zones_are_declared_outside_server_template(self):
        zones = LIMITS.read_text(encoding="utf-8")
        self.assertIn("limit_conn_zone $binary_remote_addr zone=mg_per_ip:10m;", zones)
        self.assertIn("limit_req_zone $binary_remote_addr zone=mg_api_per_ip:10m rate=20r/s;", zones)
        self.assertNotIn("server {", zones)

    def test_virtual_host_uses_declared_limits_and_loopback_backend(self):
        config = VHOST.read_text(encoding="utf-8")
        self.assertIn("limit_conn mg_per_ip 30;", config)
        self.assertIn("limit_req zone=mg_api_per_ip burst=60 nodelay;", config)
        self.assertIn("limit_req_status 429;", config)
        self.assertIn("proxy_pass http://127.0.0.1:8080;", config)
        self.assertIn("client_max_body_size 50m;", config)
        self.assertIn("return 301 https://business.example.com$request_uri;", config)
        self.assertIn("if ($host != business.example.com) { return 444; }", config)
        self.assertNotIn("return 301 https://$host$request_uri;", config)
        self.assertNotIn("proxy_pass http://0.0.0.0", config)

    def test_runbook_installs_shared_zones_before_enabling_virtual_host(self):
        runbook = RUNBOOK.read_text(encoding="utf-8")
        zone_install = runbook.index("mg-business-limits.conf.example")
        vhost_install = runbook.index("mg-business.conf.example")
        validation = runbook.index("sudo nginx -t")
        self.assertLess(zone_install, vhost_install)
        self.assertLess(vhost_install, validation)


if __name__ == "__main__":
    unittest.main()
