import base64
import hashlib
import hmac
import json
import http.client
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import server


def make_license(modules, license_id):
    payload = {"license_id": license_id, "modules": modules, "plan": "integration",
               "expires_at": int(time.time()) + 86400}
    encoded = base64.urlsafe_b64encode(json.dumps(payload,separators=(",",":"),sort_keys=True).encode()).decode().rstrip("=")
    signed = ("MG1." + encoded).encode()
    secret = os.environ["MG_LICENSE_SECRET"]
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(),signed,hashlib.sha256).digest()).decode().rstrip("=")
    return "MG1." + encoded + "." + signature



class DatabaseSchemaCompatibilityTests(unittest.TestCase):
    def test_newer_database_schema_is_rejected_before_schema_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "future.sqlite3"
            connection = __import__("sqlite3").connect(db_path)
            connection.execute(
                "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (server.SCHEMA_VERSION + 1, int(time.time())),
            )
            connection.commit()
            connection.close()

            with mock.patch.object(server, "DATA_DIR", Path(directory)), \
                 mock.patch.object(server, "DB_PATH", db_path):
                with self.assertRaisesRegex(RuntimeError, "newer than this application supports"):
                    server.connect()

            check = __import__("sqlite3").connect(db_path)
            try:
                tables = {row[0] for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                self.assertEqual(tables, {"schema_migrations"})
                self.assertEqual(
                    check.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                    server.SCHEMA_VERSION + 1,
                )
            finally:
                check.close()


    def test_failed_migration_rolls_back_schema_and_version_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "migration.sqlite3"
            with mock.patch.object(server, "DATA_DIR", Path(directory)), mock.patch.object(server, "DB_PATH", db_path):
                initialized = server.connect()
                initialized.close()

                raw = sqlite3.connect(db_path)
                try:
                    raw.execute("DELETE FROM schema_migrations WHERE version=17")
                    raw.execute("DROP INDEX IF EXISTS idx_orders_fulfillment_queue")
                    columns = {row[1] for row in raw.execute("PRAGMA table_info(orders)")}
                    if "fulfillment_status" in columns:
                        raw.execute("ALTER TABLE orders DROP COLUMN fulfillment_status")
                    raw.execute("""
                        CREATE TRIGGER reject_migration_17
                        BEFORE INSERT ON schema_migrations
                        WHEN NEW.version = 17
                        BEGIN SELECT RAISE(ABORT, 'simulated migration failure'); END
                    """)
                    raw.commit()
                finally:
                    raw.close()

                with self.assertRaisesRegex(sqlite3.IntegrityError, "simulated migration failure"):
                    server.connect()

                check = sqlite3.connect(db_path)
                try:
                    versions = {row[0] for row in check.execute("SELECT version FROM schema_migrations")}
                    columns = {row[1] for row in check.execute("PRAGMA table_info(orders)")}
                    index = check.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_orders_fulfillment_queue'"
                    ).fetchone()
                    self.assertNotIn(17, versions)
                    self.assertNotIn("fulfillment_status", columns)
                    self.assertIsNone(index)
                finally:
                    check.close()


class BackupSchedulerTests(unittest.TestCase):
    def test_failed_scheduled_backups_retry_with_bounded_backoff(self):
        class StopAfterWaits:
            def __init__(self, count):
                self.count = count
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                return len(self.waits) >= self.count

        stop_event = StopAfterWaits(3)
        with mock.patch.object(
            server, "create_snapshot",
            side_effect=[OSError("disk temporarily unavailable"),
                         OSError("disk still unavailable"), Path("/tmp/recovered.sqlite3")],
        ) as create, mock.patch.object(server, "BACKUP_INTERVAL_SECONDS", 86400):
            server.backup_scheduler(stop_event)

        self.assertEqual(create.call_count, 3)
        self.assertEqual(stop_event.waits, [60, 120, 86400])

    def test_successful_backup_resets_retry_backoff(self):
        class StopAfterWaits:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return False

            def wait(self, delay):
                self.waits.append(delay)
                return len(self.waits) >= 3

        stop_event = StopAfterWaits()
        with mock.patch.object(
            server, "create_snapshot",
            side_effect=[OSError("temporary failure"), Path("/tmp/recovered.sqlite3"),
                         OSError("later failure")],
        ) as create, mock.patch.object(server, "BACKUP_INTERVAL_SECONDS", 7200):
            server.backup_scheduler(stop_event)

        self.assertEqual(create.call_count, 3)
        self.assertEqual(stop_event.waits, [60, 7200, 60])


class BoundedHttpServerTests(unittest.TestCase):
    def test_over_capacity_response_is_bounded_well_formed_and_closed(self):
        httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        client, peer = socket.socketpair()
        try:
            # A zero-capacity semaphore deterministically models a saturated worker pool.
            httpd._worker_slots = threading.BoundedSemaphore(0)
            with mock.patch.object(socket.socket, "settimeout", autospec=True) as settimeout, \
                 mock.patch.object(httpd, "shutdown_request", wraps=httpd.shutdown_request) as shutdown:
                httpd.process_request(client, ("127.0.0.1", 12345))

            settimeout.assert_called_once_with(server.REQUEST_TIMEOUT_SECONDS)
            shutdown.assert_called_once_with(client)
            response = peer.recv(1024)
            self.assertEqual(
                response,
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Connection: close\r\n"
                b"Content-Length: 0\r\n\r\n",
            )
            self.assertEqual(peer.recv(1), b"")
            # A rejected request never acquired a worker slot.
            self.assertEqual(httpd._worker_slots._value, 0)
        finally:
            peer.close()
            httpd.server_close()

    def test_overload_send_failure_still_closes_socket_without_consuming_slot(self):
        httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        client, peer = socket.socketpair()
        httpd._worker_slots = threading.BoundedSemaphore(0)
        peer.close()
        try:
            with mock.patch.object(httpd, "shutdown_request", wraps=httpd.shutdown_request) as shutdown:
                # A disconnected peer makes the 503 write fail; cleanup must
                # still close the accepted socket and leave capacity unchanged.
                httpd.process_request(client, ("127.0.0.1", 12345))
            shutdown.assert_called_once_with(client)
            self.assertEqual(httpd._worker_slots._value, 0)
            self.assertEqual(client.fileno(), -1)
        finally:
            client.close()
            httpd.server_close()

    def test_live_worker_limit_rejects_excess_connection_until_slot_releases(self):
        started = threading.Event()
        release = threading.Event()
        active_lock = threading.Lock()
        active_count = 0
        peak_active = 0

        class BlockingHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal active_count, peak_active
                with active_lock:
                    active_count += 1
                    peak_active = max(peak_active, active_count)
                started.set()
                try:
                    release.wait(3)
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                finally:
                    with active_lock:
                        active_count -= 1

            def log_message(self, *args):
                pass

        httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), BlockingHandler)
        httpd._worker_slots = threading.BoundedSemaphore(1)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        first = second = None
        try:
            first = socket.create_connection(httpd.server_address, timeout=2)
            first.sendall(b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n")
            self.assertTrue(started.wait(2), "first request did not occupy the worker slot")

            second = socket.create_connection(httpd.server_address, timeout=2)
            second.sendall(b"GET /second HTTP/1.1\r\nHost: localhost\r\n\r\n")
            rejected = bytearray()
            while True:
                chunk = second.recv(1024)
                if not chunk:
                    break
                rejected.extend(chunk)
            self.assertTrue(rejected.startswith(b"HTTP/1.1 503 Service Unavailable\r\n"))
            self.assertEqual(peak_active, 1, "the server must never exceed its worker ceiling")
            self.assertEqual(httpd._worker_slots._value, 0)

            release.set()
            accepted = bytearray()
            while True:
                chunk = first.recv(1024)
                if not chunk:
                    break
                accepted.extend(chunk)
            self.assertTrue(accepted.startswith(b"HTTP/1.0 200 OK\r\n"))
            self.assertTrue(accepted.endswith(b"ok"))
            deadline = time.time() + 2
            while httpd._worker_slots._value != 1 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(httpd._worker_slots._value, 1)
        finally:
            release.set()
            if first is not None:
                first.close()
            if second is not None:
                second.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)

    def test_overload_send_failure_still_closes_socket_without_consuming_capacity(self):
        class BrokenPeer:
            def settimeout(self, timeout):
                self.timeout = timeout

            def sendall(self, payload):
                raise BrokenPipeError("peer disconnected")

        httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        client = BrokenPeer()
        try:
            httpd._worker_slots = threading.BoundedSemaphore(0)
            with mock.patch.object(httpd, "shutdown_request") as shutdown:
                # A disconnected peer must not escape the overload cleanup path.
                httpd.process_request(client, ("127.0.0.1", 12345))

            self.assertEqual(client.timeout, server.REQUEST_TIMEOUT_SECONDS)
            shutdown.assert_called_once_with(client)
            self.assertEqual(httpd._worker_slots._value, 0)
        finally:
            httpd.server_close()

    def test_slow_incomplete_request_is_closed_after_socket_timeout(self):
        class NoopHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        with mock.patch.object(server, "REQUEST_TIMEOUT_SECONDS", 0.2):
            httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), NoopHandler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            client = socket.create_connection(httpd.server_address, timeout=2)
            try:
                client.sendall(b"GET / HTTP/1.1\r\nHost:")
                client.settimeout(2)
                self.assertEqual(client.recv(1), b"")
                deadline = time.time() + 2
                while httpd._worker_slots._value != server.MAX_HTTP_WORKERS and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(httpd._worker_slots._value, server.MAX_HTTP_WORKERS)
            finally:
                client.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)

    def test_accepted_connection_gets_timeout_and_releases_worker_slot(self):
        class TimeoutProbeHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = str(self.connection.gettimeout()).encode("ascii")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), TimeoutProbeHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:%d/" % httpd.server_address[1]
            with urllib.request.urlopen(url, timeout=5) as response:
                observed_timeout = float(response.read().decode("ascii"))
            self.assertEqual(observed_timeout, server.REQUEST_TIMEOUT_SECONDS)
            deadline = time.time() + 2
            while httpd._worker_slots._value != server.MAX_HTTP_WORKERS and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(httpd._worker_slots._value, server.MAX_HTTP_WORKERS)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


    def test_worker_slot_is_released_when_thread_start_fails(self):
        httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        client, peer = socket.socketpair()
        initial_slots = httpd._worker_slots._value
        try:
            with mock.patch("threading.Thread.start", side_effect=RuntimeError("simulated thread start failure")), \
                 mock.patch.object(httpd, "shutdown_request", wraps=httpd.shutdown_request) as shutdown:
                with self.assertRaisesRegex(RuntimeError, "simulated thread start failure"):
                    httpd.process_request(client, ("127.0.0.1", 12345))
            shutdown.assert_called_once_with(client)
            self.assertEqual(peer.recv(1), b"")
            self.assertEqual(httpd._worker_slots._value, initial_slots)
        finally:
            client.close()
            peer.close()
            httpd.server_close()


class HttpProtocolHardeningTests(unittest.TestCase):
    def test_backup_restore_rejects_expect_continue_without_interim_body_upload(self):
        # BaseHTTPRequestHandler only invokes handle_expect_100 when its
        # configured response protocol is HTTP/1.1 or newer.
        with mock.patch.object(server.Handler, "protocol_version", "HTTP/1.1"):
            httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            client = socket.create_connection(httpd.server_address, timeout=2)
            try:
                client.sendall(
                    b"POST /api/backup/restore HTTP/1.1\r\n"
                    b"Host: localhost\r\n"
                    b"Content-Length: 100\r\n"
                    b"Expect: 100-continue\r\n"
                    b"Connection: close\r\n\r\n"
                )
                response = bytearray()
                while True:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                self.assertTrue(response.startswith(b"HTTP/1.1 417 "), bytes(response[:200]))
                self.assertNotIn(b"100 Continue", response)
                self.assertNotIn(b"HTTP/1.1 100", response)
            finally:
                client.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)


class StaticPathSecurityTests(unittest.TestCase):
    def test_symlink_cannot_publish_files_outside_web_root(self):
        with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(root_dir)
            outside = Path(outside_dir) / "private.txt"
            outside.write_text("private", encoding="utf-8")
            (root / "assets").mkdir()
            (root / "assets" / "external").symlink_to(outside_dir, target_is_directory=True)
            handler = object.__new__(server.Handler)
            handler.directory = str(root)
            with mock.patch.object(server, "ROOT", root):
                translated = Path(handler.translate_path("/assets/external/private.txt"))
                self.assertEqual(translated, root / "__blocked_resource__")

    def test_hidden_and_operational_files_are_not_public_resources(self):
        with tempfile.TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            (root / "assets").mkdir()
            (root / "assets" / "app.js").write_text("ok", encoding="utf-8")
            (root / ".secrets").mkdir()
            (root / ".secrets" / "token.txt").write_text("secret", encoding="utf-8")
            (root / "settings.env").write_text("secret", encoding="utf-8")
            handler = object.__new__(server.Handler)
            handler.directory = str(root)
            with mock.patch.object(server, "ROOT", root):
                self.assertEqual(Path(handler.translate_path("/assets/app.js")), root / "assets" / "app.js")
                self.assertEqual(Path(handler.translate_path("/.secrets/token.txt")), root / "__blocked_resource__")
                self.assertEqual(Path(handler.translate_path("/settings.env")), root / "__blocked_resource__")


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ['MG_LICENSE_SECRET'] = 'integration-test-secret-which-is-long-enough-123456'
        cls.temp = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(cls.temp.name)
        server.DB_PATH = Path(cls.temp.name) / "test.sqlite3"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def request(self, method, path, payload=None, token=None):
        body = None if payload is None else json.dumps(payload).encode()
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_security_headers_are_applied_to_api_and_static_responses(self):
        for path in ("/api/health", "/"):
            req = urllib.request.Request(self.base + path, method="GET")
            with urllib.request.urlopen(req, timeout=5) as response:
                headers = response.headers
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(headers.get("X-Frame-Options"), "DENY")
                self.assertEqual(headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
                self.assertEqual(headers.get("Permissions-Policy"),
                                 "camera=(), microphone=(), geolocation=()")
                self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))
                self.assertEqual(headers.get("Cross-Origin-Opener-Policy"), "same-origin")
                self.assertEqual(headers.get("Cross-Origin-Resource-Policy"), "same-origin")

    def test_operational_and_developer_files_are_not_public(self):
        paths = (
            "/deployment_preflight.py",
            "/deploy/systemd/mg-business.env.example",
            "/deploy/nginx/mg-business.conf.example",
            "/tests/test_server.py",
            "/README.md",
            "/backup_manager.py",
        )
        for path in paths:
            with self.subTest(path=path):
                req = urllib.request.Request(self.base + path, method="GET")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(error.exception.code, 404)

    def test_request_body_limit_is_small_except_for_backup_restore(self):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=5)
        try:
            connection.putrequest("POST", "/api/login")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(server.MAX_BODY + 1))
            connection.endheaders()
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 400)
            self.assertIn("حجم درخواست", payload["error"])
        finally:
            connection.close()

    def test_post_rejects_ambiguous_http_body_framing_and_closes_socket(self):
        malformed_headers = (
            b"Content-Length: 2\r\nContent-Length: 2\r\n",
            b"Transfer-Encoding: chunked\r\nContent-Length: 2\r\n",
        )
        for framing in malformed_headers:
            with self.subTest(framing=framing):
                connection = socket.create_connection(self.httpd.server_address, timeout=3)
                try:
                    connection.sendall(
                        b"POST /api/login HTTP/1.1\r\n"
                        b"Host: localhost\r\n"
                        b"Content-Type: application/json\r\n"
                        + framing + b"\r\n{}"
                    )
                    response = bytearray()
                    while b"\r\n\r\n" not in response:
                        chunk = connection.recv(4096)
                        self.assertTrue(chunk, "server closed before returning a framing error")
                        response.extend(chunk)
                    header_end = response.index(b"\r\n\r\n") + 4
                    headers = response[:header_end].decode("iso-8859-1")
                    self.assertIn(" 400 ", headers.splitlines()[0])
                    body_length = next(
                        int(line.split(":", 1)[1].strip())
                        for line in headers.splitlines()
                        if line.lower().startswith("content-length:")
                    )
                    body = bytearray(response[header_end:])
                    while len(body) < body_length:
                        chunk = connection.recv(4096)
                        self.assertTrue(chunk, "server closed before completing the framing error")
                        body.extend(chunk)
                    self.assertIn("طول بدنه", json.loads(body[:body_length])["error"])
                    self.assertEqual(connection.recv(1), b"", "ambiguous framing must close the connection")
                finally:
                    connection.close()

    def test_post_rejects_extremely_long_content_length_without_uncaught_error(self):
        connection = socket.create_connection(self.httpd.server_address, timeout=3)
        try:
            connection.sendall(
                b"POST /api/login HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Type: application/json\r\n"
                + b"Content-Length: " + (b"9" * 5000) + b"\r\n\r\n"
            )
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = connection.recv(4096)
                self.assertTrue(chunk, "server closed before returning invalid-length response")
                response.extend(chunk)
            header_end = response.index(b"\r\n\r\n") + 4
            headers = response[:header_end].decode("iso-8859-1")
            self.assertIn(" 400 ", headers.splitlines()[0])
            body_length = next(
                int(line.split(":", 1)[1].strip())
                for line in headers.splitlines()
                if line.lower().startswith("content-length:")
            )
            body = bytearray(response[header_end:])
            while len(body) < body_length:
                chunk = connection.recv(4096)
                self.assertTrue(chunk, "server closed before completing invalid-length response")
                body.extend(chunk)
            self.assertIn("طول درخواست", json.loads(body[:body_length])["error"])
            self.assertEqual(connection.recv(1), b"", "invalid numeric length must close connection")
        finally:
            connection.close()

    def test_backup_restore_does_not_send_100_continue_before_authorization(self):
        connection = socket.create_connection(self.httpd.server_address, timeout=3)
        try:
            connection.sendall(
                b"POST /api/backup/restore HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Type: application/json\r\n"
                b"Expect: 100-continue\r\n"
                + ("Content-Length: %d\r\n" % server.MAX_BACKUP_BODY).encode("ascii")
                + b"\r\n"
            )
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = connection.recv(4096)
                self.assertTrue(chunk, "server closed before rejecting Expect header")
                response.extend(chunk)
            self.assertNotIn(b"100 Continue", response)
            self.assertIn(b"401", response.split(b"\r\n", 1)[0])
            header_end = response.index(b"\r\n\r\n") + 4
            headers = response[:header_end].decode("iso-8859-1")
            body_length = next(
                int(line.split(":", 1)[1].strip())
                for line in headers.splitlines()
                if line.lower().startswith("content-length:")
            )
            body = bytearray(response[header_end:])
            while len(body) < body_length:
                chunk = connection.recv(4096)
                self.assertTrue(chunk, "server closed before completing unauthorized restore response")
                body.extend(chunk)
            self.assertIn("وارد حساب شوید", json.loads(body[:body_length].decode("utf-8"))["error"])
            self.assertEqual(connection.recv(1), b"", "unauthorized restore must close without reading upload body")
        finally:
            connection.close()

    def test_backup_restore_rejects_concurrent_large_upload_before_reading_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(server, "DATA_DIR", root), mock.patch.object(server, "DB_PATH", root / "test.sqlite3"):
                status, setup = self.request("POST", "/api/setup", {
                    "businessName": "Restore capacity test",
                    "username": "restore-capacity-owner",
                    "password": "restore-capacity-owner-strong-password"
                })
                self.assertEqual(status, 200)
                with mock.patch.object(server, "BACKUP_RESTORE_SEMAPHORE", threading.BoundedSemaphore(0)):
                    connection = socket.create_connection(self.httpd.server_address, timeout=3)
                    try:
                        connection.sendall(
                            b"POST /api/backup/restore HTTP/1.1\r\n"
                            b"Host: localhost\r\n"
                            b"Content-Type: application/json\r\n"
                            + ("Content-Length: %d\r\n" % server.MAX_BACKUP_BODY).encode("ascii")
                            + ("Authorization: Bearer %s\r\n" % setup["token"]).encode("ascii")
                            + b"\r\n"
                        )
                        response = bytearray()
                        while b"\r\n\r\n" not in response:
                            chunk = connection.recv(4096)
                            self.assertTrue(chunk, "server closed before returning restore-capacity response")
                            response.extend(chunk)
                        header_end = response.index(b"\r\n\r\n") + 4
                        headers = response[:header_end].decode("iso-8859-1")
                        self.assertIn(" 503 ", headers.splitlines()[0])
                        body_length = next(
                            int(line.split(":", 1)[1].strip())
                            for line in headers.splitlines()
                            if line.lower().startswith("content-length:")
                        )
                        body = bytearray(response[header_end:])
                        while len(body) < body_length:
                            chunk = connection.recv(4096)
                            self.assertTrue(chunk, "server closed before completing restore-capacity response")
                            body.extend(chunk)
                        self.assertIn("در حال اجراست", json.loads(body[:body_length])["error"])
                        self.assertEqual(connection.recv(1), b"", "rejected restore upload must close connection")
                    finally:
                        connection.close()

    def test_backup_restore_authenticates_before_reading_large_body_and_closes_socket(self):
        connection = socket.create_connection(self.httpd.server_address, timeout=3)
        try:
            connection.sendall(
                b"POST /api/backup/restore HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Type: application/json\r\n"
                + ("Content-Length: %d\r\n" % server.MAX_BACKUP_BODY).encode("ascii")
                + b"\r\n"
            )
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = connection.recv(4096)
                self.assertTrue(chunk, "server closed before sending the authentication response")
                response.extend(chunk)
            header_end = response.index(b"\r\n\r\n") + 4
            headers = response[:header_end].decode("iso-8859-1")
            self.assertIn(" 401 ", headers.splitlines()[0])
            body_length = next(
                int(line.split(":", 1)[1].strip())
                for line in headers.splitlines()
                if line.lower().startswith("content-length:")
            )
            body = bytearray(response[header_end:])
            while len(body) < body_length:
                chunk = connection.recv(4096)
                self.assertTrue(chunk, "server closed before sending the complete response body")
                body.extend(chunk)
            self.assertIn("وارد حساب", json.loads(body[:body_length])["error"])
            self.assertEqual(connection.recv(1), b"", "unauthenticated large-body connection must close immediately")
        finally:
            connection.close()

    def test_backup_restore_rejects_non_owner_before_reading_large_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(server, "DATA_DIR", root), mock.patch.object(server, "DB_PATH", root / "test.sqlite3"):
                status, setup = self.request("POST", "/api/setup", {
                    "businessName": "Restore authorization test",
                    "username": "restore-owner",
                    "password": "restore-owner-strong-password"
                })
                self.assertEqual(status, 200)
                owner_token = setup["token"]
                status, cashier = self.request("POST", "/api/users", {
                    "username": "restore-cashier",
                    "password": "restore-cashier-strong-password",
                    "role": "cashier"
                }, owner_token)
                self.assertEqual(status, 201)
                status, login = self.request("POST", "/api/login", {
                    "username": "restore-cashier",
                    "password": "restore-cashier-strong-password"
                })
                self.assertEqual(status, 200)

                connection = socket.create_connection(self.httpd.server_address, timeout=3)
                try:
                    connection.sendall(
                        b"POST /api/backup/restore HTTP/1.1\r\n"
                        b"Host: localhost\r\n"
                        b"Content-Type: application/json\r\n"
                        + ("Content-Length: %d\r\n" % server.MAX_BACKUP_BODY).encode("ascii")
                        + ("Authorization: Bearer %s\r\n" % login["token"]).encode("ascii")
                        + b"\r\n"
                    )
                    response = bytearray()
                    while b"\r\n\r\n" not in response:
                        chunk = connection.recv(4096)
                        self.assertTrue(chunk, "server closed before sending the authorization response")
                        response.extend(chunk)
                    header_end = response.index(b"\r\n\r\n") + 4
                    headers = response[:header_end].decode("iso-8859-1")
                    self.assertIn(" 403 ", headers.splitlines()[0])
                    body_length = next(
                        int(line.split(":", 1)[1].strip())
                        for line in headers.splitlines()
                        if line.lower().startswith("content-length:")
                    )
                    body = bytearray(response[header_end:])
                    while len(body) < body_length:
                        chunk = connection.recv(4096)
                        self.assertTrue(chunk, "server closed before sending the complete authorization response")
                        body.extend(chunk)
                    self.assertIn("فقط مالک", json.loads(body[:body_length])["error"])
                    self.assertEqual(connection.recv(1), b"", "non-owner backup connection must close immediately")
                finally:
                    connection.close()

    def test_public_routes_fail_closed_when_multiple_tenants_exist(self):
        tenant_ids = ("tenant-public-a", "tenant-public-b")
        db = server.connect()
        try:
            now = int(time.time())
            db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)", (tenant_ids[0],"Public A",now))
            db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)", (tenant_ids[1],"Public B",now+1))
            db.commit()
        finally:
            db.close()

        try:
            status, result = self.request("GET", "/api/public/menu")
            self.assertEqual(status, 409)
            self.assertIn("دامنه اختصاصی", result["error"])

            status, result = self.request("POST", "/api/public/orders", {
                "customerName": "Test Customer",
                "phone": "+49123456789",
                "items": [{"productId": "unknown-product", "quantity": 1}]
            })
            self.assertEqual(status, 409)
            self.assertIn("دامنه اختصاصی", result["error"])
        finally:
            # The integration server shares one temporary database across tests.
            # Remove only fixtures created here so test order cannot affect setup.
            db = server.connect()
            try:
                db.execute("DELETE FROM tenants WHERE id IN (?,?)", tenant_ids)
                db.commit()
            finally:
                db.close()
            # Keep the process-wide public-order rate limiter isolated between tests.
            with server.LOGIN_LOCK:
                server.PUBLIC_ORDER_FAILURES.pop("127.0.0.1", None)

    def test_proportional_order_discount_allocation_is_exact(self):
        allocations=server.allocate_proportional_discount([10000,20000,30000],10001)
        self.assertEqual(sum(allocations),10001)
        self.assertEqual(allocations,[1667,3334,5000])
        self.assertEqual(server.allocate_proportional_discount([1,1,1],2),[1,1,0])
        with self.assertRaises(ValueError):
            server.allocate_proportional_discount([100,200],301)

    def test_setup_auth_transaction_audit_and_isolation(self):
        status, result = self.request("GET", "/api/setup-status")
        self.assertEqual((status, result["configured"]), (200, False))

        status, result = self.request("POST", "/api/setup", {
            "businessName": "MG Test Shop", "username": "owner",
            "password": "correct-horse-battery"
        })
        self.assertEqual(status, 200)
        token = result["token"]
        owner_id = result["user"]["id"]
        self.assertEqual(result["user"]["role"], "owner")

        status, result = self.request("POST", "/api/setup", {
            "businessName": "Second Shop", "username": "other",
            "password": "correct-horse-battery"
        })
        self.assertEqual(status, 409)

        status, result = self.request("GET", "/api/me", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["tenant"]["name"], "MG Test Shop")

        valid_code = make_license(["cafe"], "bad-license")
        parts = valid_code.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        bad_code = ".".join(parts)
        status, result = self.request("POST", "/api/licenses/activate", {"code": bad_code}, token)
        self.assertEqual(status, 400)
        limited_code = make_license(["cafe","accounting","inventory","customers","reports"], "license-limited")
        status, result = self.request("POST", "/api/licenses/activate", {"code": limited_code}, token)
        self.assertEqual(status, 200)
        self.assertNotIn("gamenet", result["modules"])
        status, result = self.request("GET", "/api/gamenet/devices", token=token)
        self.assertEqual(status, 403)
        full_code = make_license(["cafe","gamenet","accounting","customers","inventory","reports"], "license-full")
        status, result = self.request("POST", "/api/licenses/activate", {"code": full_code}, token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/modules", token=token)
        self.assertIn("gamenet", result["modules"])

        status, result = self.request("POST", "/api/users", {
            "username": "cashier", "password": "cashier-long-password", "role": "cashier"
        }, token)
        self.assertEqual(status, 201)
        cashier_id = result["id"]
        # Duplicate usernames must be a clean conflict, not an internal error.
        status, duplicate_user = self.request("POST", "/api/users", {
            "username": "  CASHIER  ", "password": "another-cashier-password-2026", "role": "viewer"
        }, token)
        self.assertEqual(status,409)
        status, users = self.request("GET", "/api/users", token=token)
        self.assertEqual(status,200)
        self.assertEqual(sum(1 for item in users["items"] if item["username"]=="cashier"),1)
        status, result = self.request("POST", "/api/login", {
            "username": "cashier", "password": "cashier-long-password"
        })
        self.assertEqual(status, 200)
        cashier_token = result["token"]
        status, denied_reset = self.request("POST", "/api/users/"+cashier_id+"/password", {
            "password":"cashier-new-long-password"
        }, cashier_token)
        self.assertEqual(status,403)
        status, reset = self.request("POST", "/api/users/"+cashier_id+"/password", {
            "password":"cashier-new-long-password"
        }, token)
        self.assertEqual(status,200)
        status, old_session = self.request("GET", "/api/me", token=cashier_token)
        self.assertEqual(status,401)
        status, login_new = self.request("POST", "/api/login", {
            "username":"cashier","password":"cashier-new-long-password"
        })
        self.assertEqual(status,200)
        cashier_token = login_new["token"]
        status, result = self.request("POST", "/api/products", {
            "name": "Forbidden", "sellPrice": 100, "costPrice": 20
        }, cashier_token)
        self.assertEqual(status, 403)
        # A non-owner cannot deactivate another account, and an owner cannot
        # accidentally deactivate their own only administrative account.
        status, denied_deactivation = self.request("DELETE", "/api/users/" + cashier_id, token=cashier_token)
        self.assertEqual(status, 403)
        status, self_deactivation = self.request("DELETE", "/api/users/" + owner_id, token=token)
        self.assertEqual(status, 400)
        status, result = self.request("DELETE", "/api/users/" + cashier_id, token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/me", token=cashier_token)
        self.assertEqual(status, 401)
        status, repeated_deactivation = self.request("DELETE", "/api/users/" + cashier_id, token=token)
        self.assertEqual(status, 404)
        status, deactivated_login = self.request("POST", "/api/login", {
            "username": "cashier", "password": "cashier-new-long-password"
        })
        self.assertEqual(status, 401)
        status, user_audit = self.request("GET", "/api/audit-log", token=token)
        self.assertEqual(status, 200)
        deactivation_events = [event for event in user_audit["items"]
                               if event["action"] == "user.deactivated" and event["entityId"] == cashier_id]
        self.assertEqual(len(deactivation_events), 1)

        status, result = self.request("POST", "/api/transactions", {
            "id": "tx-test-1", "title": "Cafe sale", "amount": 150000,
            "kind": "income", "category": "Cafe",
            "createdAt": "2026-11-16T12:00:00.000Z"
        }, token)
        self.assertEqual(status, 201)
        status, duplicate_tx = self.request("POST", "/api/transactions", {
            "id":"tx-test-1","title":"Cafe sale","amount":150000,
            "kind":"income","category":"Cafe","createdAt":"2026-11-16T12:00:00.000Z"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_tx["duplicate"])
        # Idempotent retries must verify the immutable accounting journal, not only
        # the transaction row, before claiming that the original write succeeded.
        db=server.connect()
        try:
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            tenant_id=db.execute("SELECT tenant_id FROM users WHERE username='owner'").fetchone()["tenant_id"]
            journal_id=db.execute("""SELECT id FROM journal_entries
                WHERE tenant_id=? AND source_type='transaction' AND source_id=?""",
                (tenant_id,"tx-test-1")).fetchone()["id"]
            saved_tx_lines=[dict(row) for row in db.execute(
                "SELECT * FROM journal_lines WHERE tenant_id=? AND entry_id=?",
                (tenant_id,journal_id)).fetchall()]
            db.execute("DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id))
            db.commit()
        finally:
            db.close()
        status, damaged_retry=self.request("POST","/api/transactions",{
            "id":"tx-test-1","title":"Cafe sale","amount":150000,
            "kind":"income","category":"Cafe","createdAt":"2026-11-16T12:00:00.000Z"
        },token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            columns=list(saved_tx_lines[0])
            placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(line[column] for column in columns) for line in saved_tx_lines])
            db.execute("""CREATE TRIGGER IF NOT EXISTS journal_lines_no_delete BEFORE DELETE ON journal_lines
                BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END""")
            db.commit()
        finally:
            db.close()
        status, conflict_tx = self.request("POST", "/api/transactions", {
            "id":"tx-test-1","title":"Different sale","amount":150000,
            "kind":"income","category":"Cafe","createdAt":"2026-11-16T12:00:00.000Z"
        }, token)
        self.assertEqual(status,409)

        status, audit = self.request("GET", "/api/audit-log", token=token)
        self.assertEqual(status,200)
        self.assertTrue(any(x["action"]=="transaction.created" for x in audit["items"]))
        status, result = self.request("GET", "/api/transactions", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["amount"], 150000)

        status, result = self.request("GET", "/api/accounting/profit-loss", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["revenue"], 150000)
        status, ranged_profit = self.request("GET", "/api/accounting/profit-loss?fromDate=2026-11-17&toDate=2026-11-17", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(ranged_profit["revenue"], 0)
        self.assertEqual(ranged_profit["expenses"], 0)
        self.assertEqual(ranged_profit["fromDate"], "2026-11-17")
        status, ranged_journal = self.request("GET", "/api/accounting/journal?fromDate=2026-11-16&toDate=2026-11-16", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(ranged_journal["fromDate"], "2026-11-16")
        self.assertEqual(ranged_journal["toDate"], "2026-11-16")
        self.assertTrue(any(item["sourceId"]=="tx-test-1" for item in ranged_journal["items"]))
        status, account_list = self.request("GET", "/api/accounting/accounts", token=token)
        self.assertEqual(status, 200)
        cash_account = next(item for item in account_list["items"] if item["code"]=="1000")
        status, account_ledger = self.request("GET",
            "/api/accounting/account-ledger?accountId="+cash_account["id"]+
            "&fromDate=2026-11-16&toDate=2026-11-16", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(account_ledger["account"]["code"], "1000")
        self.assertEqual(account_ledger["openingBalance"], 0)
        self.assertEqual(account_ledger["periodDebit"], 150000)
        self.assertEqual(account_ledger["periodCredit"], 0)
        self.assertEqual(account_ledger["closingBalance"], 150000)
        self.assertEqual(len(account_ledger["items"]), 1)
        self.assertEqual(account_ledger["items"][0]["runningBalance"], 150000)
        status, unbounded_account_ledger = self.request("GET",
            "/api/accounting/account-ledger?accountId="+cash_account["id"], token=token)
        self.assertEqual(status, 200)
        self.assertEqual(unbounded_account_ledger["openingBalance"], 0)
        self.assertEqual(unbounded_account_ledger["periodDebit"], 150000)
        self.assertEqual(unbounded_account_ledger["closingBalance"], 150000)
        status, upper_bounded_account_ledger = self.request("GET",
            "/api/accounting/account-ledger?accountId="+cash_account["id"]+
            "&toDate=2026-11-16", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(upper_bounded_account_ledger["openingBalance"], 0)
        self.assertEqual(upper_bounded_account_ledger["closingBalance"], 150000)
        status, missing_account_ledger = self.request("GET", "/api/accounting/account-ledger", token=token)
        self.assertEqual(status, 400)
        status, unknown_account_ledger = self.request("GET",
            "/api/accounting/account-ledger?accountId=not-a-real-account", token=token)
        self.assertEqual(status, 404)
        status, duplicate_account_param = self.request("GET",
            "/api/accounting/account-ledger?accountId="+cash_account["id"]+
            "&accountId="+cash_account["id"], token=token)
        self.assertEqual(status, 400)
        status, invalid_account_ledger_range = self.request("GET",
            "/api/accounting/account-ledger?accountId="+cash_account["id"]+
            "&fromDate=2026-11-18&toDate=2026-11-17", token=token)
        self.assertEqual(status, 400)
        status, cash_flow = self.request("GET", "/api/accounting/cash-flow?fromDate=2026-11-16&toDate=2026-11-16", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(cash_flow["inflow"], 150000)
        self.assertEqual(cash_flow["outflow"], 0)
        self.assertEqual(cash_flow["netCashFlow"], 150000)
        self.assertEqual([item["sourceId"] for item in cash_flow["items"]], ["tx-test-1"])
        status, empty_cash_flow = self.request("GET", "/api/accounting/cash-flow?fromDate=2026-11-17&toDate=2026-11-17", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(empty_cash_flow["items"], [])
        self.assertEqual(empty_cash_flow["netCashFlow"], 0)
        status, invalid_cash_flow = self.request("GET", "/api/accounting/cash-flow?fromDate=2026-11-18&toDate=2026-11-17", token=token)
        self.assertEqual(status, 400)
        status, empty_journal = self.request("GET", "/api/accounting/journal?fromDate=2026-11-17&toDate=2026-11-17", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(empty_journal["items"], [])
        status, invalid_journal_range = self.request("GET", "/api/accounting/journal?fromDate=2026-11-18&toDate=2026-11-17", token=token)
        self.assertEqual(status, 400)
        status, invalid_period = self.request("GET", "/api/accounting/profit-loss?fromDate=2026-11-18&toDate=2026-11-17", token=token)
        self.assertEqual(status, 400)
        status, malformed_period = self.request("GET", "/api/accounting/trial-balance?toDate=17-11-2026", token=token)
        self.assertEqual(status, 400)
        status, prior_trial = self.request("GET", "/api/accounting/trial-balance?toDate=2026-11-15", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(prior_trial["totalDebit"], 0)
        self.assertEqual(prior_trial["totalCredit"], 0)
        self.assertTrue(any(x["code"]=="1000" and x["debit"]==0 and x["credit"]==0 for x in prior_trial["items"]))
        status, prior_sheet = self.request("GET", "/api/accounting/balance-sheet?toDate=2026-11-15", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(prior_sheet["assets"], 0)
        self.assertEqual(prior_sheet["liabilities"], 0)
        self.assertEqual(prior_sheet["equity"], 0)
        self.assertTrue(any(x["code"]=="1000" and x["debit"]==0 and x["credit"]==0 for x in prior_sheet["items"]))
        status, trial = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(trial["totalDebit"], trial["totalCredit"])
        status, balance_sheet = self.request("GET", "/api/accounting/balance-sheet", token=token)
        self.assertTrue(balance_sheet["balanced"])
        db = server.connect()
        try:
            totals = db.execute("""SELECT SUM(l.debit),SUM(l.credit) FROM journal_lines l
                JOIN journal_entries e ON e.id=l.entry_id
                WHERE e.tenant_id=(SELECT tenant_id FROM users WHERE username='owner')
                  AND e.source_type='transaction' AND e.source_id='tx-test-1'""").fetchone()
            self.assertEqual((totals[0], totals[1]), (150000, 150000))
            journal_id = db.execute("SELECT id FROM journal_entries WHERE source_type='transaction' AND source_id='tx-test-1'").fetchone()[0]
            with self.assertRaises(Exception):
                db.execute("UPDATE journal_entries SET memo='tampered' WHERE id=?", (journal_id,))
                db.commit()
            db.rollback()
        finally:
            db.close()

        # Exercise the expense-side idempotency mapping and damaged-journal rejection.
        expense_payload={
            "id":"tx-expense-retry","title":"Utilities","amount":25000,
            "kind":"expense","category":"Utilities","createdAt":"2026-11-16T13:00:00.000Z"
        }
        status, expense_created=self.request("POST","/api/transactions",expense_payload,token)
        self.assertEqual(status,201)
        status, updated_cash_flow=self.request("GET","/api/accounting/cash-flow?fromDate=2026-11-16&toDate=2026-11-16",token=token)
        self.assertEqual(status,200)
        self.assertEqual(updated_cash_flow["inflow"],150000)
        self.assertEqual(updated_cash_flow["outflow"],25000)
        self.assertEqual(updated_cash_flow["netCashFlow"],125000)
        status, expense_duplicate=self.request("POST","/api/transactions",expense_payload,token)
        self.assertEqual(status,200)
        self.assertTrue(expense_duplicate["duplicate"])
        db=server.connect()
        try:
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            tenant_id=db.execute("SELECT tenant_id FROM users WHERE username='owner'").fetchone()["tenant_id"]
            journal_id=db.execute("""SELECT id FROM journal_entries WHERE tenant_id=?
                AND source_type='transaction' AND source_id='tx-expense-retry'""",(tenant_id,)).fetchone()["id"]
            saved_expense_lines=[dict(row) for row in db.execute(
                "SELECT * FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id)
            ).fetchall()]
            db.execute("DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id))
            db.commit()
        finally:
            db.close()
        status, damaged_expense_retry=self.request("POST","/api/transactions",expense_payload,token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            columns=list(saved_expense_lines[0])
            placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(line[column] for column in columns) for line in saved_expense_lines])
            db.execute("""CREATE TRIGGER IF NOT EXISTS journal_lines_no_delete BEFORE DELETE ON journal_lines
                BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END""")
            db.commit()
        finally:
            db.close()
        status, expense_report=self.request("GET","/api/accounting/profit-loss",token=token)
        self.assertEqual(status,200)
        self.assertEqual(expense_report["expenses"],25000)
        status, expense_trial=self.request("GET","/api/accounting/trial-balance",token=token)
        self.assertEqual(status,200)
        self.assertEqual(expense_trial["totalDebit"],expense_trial["totalCredit"])
        status, expense_balance=self.request("GET","/api/accounting/balance-sheet",token=token)
        self.assertEqual(status,200)
        self.assertTrue(expense_balance["balanced"])

        status, result = self.request("POST", "/api/transactions", {
            "id": "tx-bad", "title": "Invalid amount", "amount": True,
            "kind": "income", "createdAt": "2026-11-16T12:00:00Z"
        }, token)
        self.assertEqual(status, 400)

        status, result = self.request("POST", "/api/products", {
            "id": "product-1", "name": "Espresso", "sku": "ESP-01",
            "category": "Coffee", "sellPrice": 85000, "costPrice": 30000
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("GET", "/api/products", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["sellPrice"], 85000)
        status, result = self.request("POST", "/api/products", {
            "id": "product-2", "name": "Duplicate SKU", "sku": "ESP-01",
            "sellPrice": 90000, "costPrice": 30000
        }, token)
        self.assertEqual(status, 409)
        status, duplicate_product_id = self.request("POST", "/api/products", {
            "id":"product-1","name":"Duplicate product ID","sku":"ESP-02",
            "sellPrice":100000,"costPrice":40000
        }, token)
        self.assertEqual(status,409)

        status, result = self.request("POST", "/api/inventory/movements", {
            "id": "move-in-1", "productId": "product-1", "movementType": "purchase",
            "quantityDelta": 20, "unitCost": 30000
        }, token)
        self.assertEqual(status, 201)
        status, duplicate_movement = self.request("POST", "/api/inventory/movements", {
            "id":"move-in-1","productId":"product-1","movementType":"purchase",
            "quantityDelta":20,"unitCost":30000
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_movement["duplicate"])
        db = server.connect()
        try:
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            journal_id = db.execute("""SELECT id FROM journal_entries
                WHERE tenant_id=? AND source_type='inventory_movement' AND source_id=?""",
                (db.execute("SELECT tenant_id FROM users WHERE username=?", ("owner",)).fetchone()["tenant_id"],"move-in-1")).fetchone()["id"]
            saved_journal_lines = [dict(row) for row in db.execute(
                "SELECT * FROM journal_lines WHERE entry_id=?", (journal_id,)
            ).fetchall()]
            db.execute("DELETE FROM journal_lines WHERE entry_id=?", (journal_id,))
            db.commit()
        finally:
            db.close()
        status, corrupted_retry = self.request("POST", "/api/inventory/movements", {
            "id":"move-in-1","productId":"product-1","movementType":"purchase",
            "quantityDelta":20,"unitCost":30000
        }, token)
        self.assertEqual(status,409)
        db = server.connect()
        try:
            columns = list(saved_journal_lines[0])
            placeholders = ",".join("?" for _ in columns)
            db.executemany(
                "INSERT INTO journal_lines(" + ",".join(columns) + ") VALUES(" + placeholders + ")",
                [tuple(line[column] for column in columns) for line in saved_journal_lines]
            )
            db.commit()
        finally:
            db.close()
        status, result = self.request("POST", "/api/inventory/movements", {
            "id": "move-out-1", "productId": "product-1", "movementType": "sale",
            "quantityDelta": -5, "unitCost": 30000
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/inventory/movements", {
            "id": "move-out-2", "productId": "product-1", "movementType": "sale",
            "quantityDelta": -16, "unitCost": 30000
        }, token)
        self.assertEqual(status, 409)
        status, result = self.request("POST", "/api/inventory/movements", {
            "id": "move-invalid-cost", "productId": "product-1", "movementType": "purchase",
            "quantityDelta": 1, "unitCost": 9007199254740992
        }, token)
        self.assertEqual(status, 400)
        status, oversized_value = self.request("POST", "/api/inventory/movements", {
            "id": "move-overflow-value", "productId": "product-1", "movementType": "purchase",
            "quantityDelta": 2, "unitCost": 9007199254740991
        }, token)
        self.assertEqual(status, 400)
        status, result = self.request("GET", "/api/inventory", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["quantity"], 15)

        status, result = self.request("POST", "/api/customers", {
            "id": "customer-1", "fullName": "Test Customer",
            "phone": "+491234567", "email": "test@example.com"
        }, token)
        self.assertEqual(status, 201)
        status, duplicate_customer = self.request("POST", "/api/customers", {
            "id":"customer-duplicate","fullName":"Duplicate Customer",
            "phone":"+491234567"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["fullName"], "Test Customer")

        status, invalid_loyalty = self.request("POST","/api/loyalty/program",{
            "enabled":True,"spendAmount":9999,"pointsAwarded":2
        },token)
        self.assertEqual(status,400)
        status, program = self.request("POST","/api/loyalty/program",{
            "enabled":True,"spendAmount":100000,"pointsAwarded":2
        },token)
        self.assertEqual(status,200)
        self.assertTrue(program["program"]["enabled"])
        status, program_read = self.request("GET","/api/loyalty/program",token=token)
        self.assertEqual(program_read["program"]["spendAmount"],100000)
        status, result = self.request("POST", "/api/orders", {
            "id": "order-test-1", "customerId": "customer-1", "discount": 5000,
            "paymentMethod": "card",
            "items": [{"productId": "product-1", "quantity": 2}]
        }, token)
        self.assertEqual(status, 201)
        self.assertEqual(result["total"], 165000)
        status, order_details = self.request("GET","/api/orders/order-test-1",token=token)
        self.assertEqual(status,200)
        self.assertEqual(sum(x["discountAmount"] for x in order_details["items"]),5000)
        self.assertEqual(sum(x["lineTotal"]-x["discountAmount"] for x in order_details["items"]),165000)
        db=server.connect()
        try:
            allocated=db.execute("""SELECT SUM(discount_amount) AS discount,
                SUM(line_total-discount_amount) AS net
                FROM order_items WHERE order_id=?""",
                ("order-test-1",)).fetchone()
            self.assertEqual(allocated["discount"],5000)
            self.assertEqual(allocated["net"],165000)
        finally:
            db.close()
        status, loyalty = self.request("GET","/api/customers/customer-1/loyalty",token=token)
        self.assertEqual(status,200)
        self.assertEqual(loyalty["balance"],2)
        self.assertEqual(len(loyalty["items"]),1)
        status, backup_with_loyalty = self.request("GET","/api/backup",token=token)
        self.assertEqual(status,200)
        self.assertIn("loyalty_programs",backup_with_loyalty["tables"])
        self.assertIn("customer_loyalty_ledger",backup_with_loyalty["tables"])
        self.assertEqual(backup_with_loyalty["tables"]["customer_loyalty_ledger"][0]["points_delta"],2)
        status, daily_best = self.request("GET","/api/reports/best-selling-today",token=token)
        self.assertEqual(status,200)
        self.assertEqual(len(daily_best["items"]),1)
        self.assertEqual(daily_best["items"][0]["productId"],"product-1")
        self.assertEqual(daily_best["items"][0]["quantity"],2)
        self.assertEqual(daily_best["items"][0]["revenue"],165000)  # Revenue is net of the proportionally allocated order discount.
        self.assertEqual(daily_best["items"][0]["orderCount"],1)
        status, duplicate_order = self.request("POST", "/api/orders", {
            "id":"order-test-1","customerId":"customer-1","discount":5000,
            "paymentMethod":"card","items":[{"productId":"product-1","quantity":2}]
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_order["duplicate"])
        self.assertEqual(duplicate_order["orderNumber"],result["orderNumber"])
        # A matching order retry must not hide missing sales or COGS postings.
        db=server.connect()
        try:
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            tenant_id=db.execute("SELECT tenant_id FROM users WHERE username='owner'").fetchone()["tenant_id"]
            saved_order_journals={}
            for source_type,source_id in (("transaction","order-order-test-1"),("order_cogs","order-test-1")):
                entry_id=db.execute("""SELECT id FROM journal_entries WHERE tenant_id=?
                    AND source_type=? AND source_id=?""",(tenant_id,source_type,source_id)).fetchone()["id"]
                saved_order_journals[source_type]=[dict(row) for row in db.execute(
                    "SELECT * FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,entry_id)
                ).fetchall()]
            db.execute("""DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=(
                SELECT id FROM journal_entries WHERE tenant_id=? AND source_type='transaction' AND source_id=?)""",
                (tenant_id,tenant_id,"order-order-test-1"))
            db.commit()
        finally:
            db.close()
        retry_payload={"id":"order-test-1","customerId":"customer-1","discount":5000,
            "paymentMethod":"card","items":[{"productId":"product-1","quantity":2}]}
        status, damaged_sales_retry=self.request("POST","/api/orders",retry_payload,token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            lines=saved_order_journals["transaction"]
            columns=list(lines[0]); placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(line[column] for column in columns) for line in lines])
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            db.execute("""DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=(
                SELECT id FROM journal_entries WHERE tenant_id=? AND source_type='order_cogs' AND source_id=?)""",
                (tenant_id,tenant_id,"order-test-1"))
            db.commit()
        finally:
            db.close()
        status, damaged_cogs_retry=self.request("POST","/api/orders",retry_payload,token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            lines=saved_order_journals["order_cogs"]
            columns=list(lines[0]); placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(line[column] for column in columns) for line in lines])
            db.execute("""CREATE TRIGGER IF NOT EXISTS journal_lines_no_delete BEFORE DELETE ON journal_lines
                BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END""")
            db.commit()
        finally:
            db.close()
        db=server.connect()
        try:
            movement_rows=[dict(row) for row in db.execute("""SELECT * FROM inventory_movements
                WHERE tenant_id=? AND movement_type='sale' AND note=?""",
                (tenant_id,"سفارش "+result["orderNumber"])).fetchall()]
            self.assertTrue(movement_rows)
            db.execute("DELETE FROM inventory_movements WHERE tenant_id=? AND movement_type='sale' AND note=?",
                (tenant_id,"سفارش "+result["orderNumber"]))
            db.commit()
        finally:
            db.close()
        status, damaged_stock_retry=self.request("POST","/api/orders",retry_payload,token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            columns=list(movement_rows[0]); placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO inventory_movements("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(row[column] for column in columns) for row in movement_rows])
            db.commit()
        finally:
            db.close()
        status, duplicate_loyalty = self.request("GET","/api/customers/customer-1/loyalty",token=token)
        self.assertEqual(duplicate_loyalty["balance"],2)
        self.assertEqual(len(duplicate_loyalty["items"]),1)
        status, conflicting_order = self.request("POST", "/api/orders", {
            "id":"order-test-1","customerId":"customer-1","discount":0,
            "paymentMethod":"card","items":[{"productId":"product-1","quantity":2}]
        }, token)
        self.assertEqual(status,409)
        db = server.connect()
        try:
            card_receivable = db.execute("""SELECT SUM(l.debit) FROM journal_lines l
                JOIN accounts a ON a.id=l.account_id
                JOIN journal_entries e ON e.id=l.entry_id
                WHERE e.source_type='transaction' AND e.source_id='order-order-test-1' AND a.code='1010'""").fetchone()[0]
            self.assertEqual(card_receivable, 165000)
        finally:
            db.close()
        order_number = result["orderNumber"]
        status, result = self.request("GET", "/api/orders", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["orderNumber"], order_number)
        status, result = self.request("GET", "/api/inventory", token=token)
        self.assertEqual(result["items"][0]["quantity"], 13)

        status, result = self.request("POST", "/api/orders", {
            "id": "order-credit-1", "customerId": "customer-1", "discount": 0,
            "paymentMethod": "credit",
            "items": [{"productId": "product-1", "quantity": 1}]
        }, token)
        self.assertEqual(status, 201)
        self.assertEqual(result["total"], 85000)
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(result["items"][0]["balance"], 85000)
        status, result = self.request("POST", "/api/customers/customer-1/payments", {
            "id":"customer-payment-01","amount":50000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["remainingBalance"],35000)
        status, duplicate_payment = self.request("POST", "/api/customers/customer-1/payments", {
            "id":"customer-payment-01","amount":50000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_payment["duplicate"])
        self.assertEqual(duplicate_payment["remainingBalance"],35000)
        # A customer-payment retry must not succeed if its accounting journal was damaged.
        db=server.connect()
        try:
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            tenant_id=db.execute("SELECT tenant_id FROM users WHERE username=?",("owner",)).fetchone()["tenant_id"]
            journal_id=db.execute("""SELECT id FROM journal_entries
                WHERE tenant_id=? AND source_type='customer_payment' AND source_id=?""",
                (tenant_id,"customer-payment-01")).fetchone()["id"]
            saved_customer_payment_lines=[dict(row) for row in db.execute(
                "SELECT * FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id)
            ).fetchall()]
            db.execute("DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id))
            db.commit()
        finally:
            db.close()
        status, corrupted_customer_retry=self.request("POST", "/api/customers/customer-1/payments", {
            "id":"customer-payment-01","amount":50000,"paymentMethod":"card"
        },token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            columns=list(saved_customer_payment_lines[0])
            placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(line[column] for column in columns) for line in saved_customer_payment_lines])
            db.commit()
        finally:
            db.close()
        status, changed_method_retry = self.request("POST", "/api/customers/customer-1/payments", {
            "id":"customer-payment-01","amount":50000,"paymentMethod":"cash"
        },token)
        self.assertEqual(status,409)
        status, second_customer = self.request("POST", "/api/customers", {
            "id":"customer-2","fullName":"Second Customer","phone":"+491234568"
        }, token)
        self.assertEqual(status,201)
        status, reused_payment_id = self.request("POST", "/api/customers/customer-2/payments", {
            "id":"customer-payment-01","amount":50000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("POST", "/api/customers/customer-1/payments", {
            "amount": 40000, "paymentMethod": "cash"
        }, token)
        self.assertEqual(status, 409)
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(next(x for x in result["items"] if x["id"]=="customer-1")["balance"], 35000)
        status, trial = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(trial["totalDebit"], trial["totalCredit"])
        status, balance_sheet = self.request("GET", "/api/accounting/balance-sheet", token=token)
        self.assertTrue(balance_sheet["balanced"])

        status, result = self.request("GET", "/api/public/menu")
        self.assertEqual(status, 200)
        self.assertTrue(any(x["id"] == "product-1" for x in result["items"]))
        status, result = self.request("POST", "/api/public/orders", {
            "clientOrderId": "public-order-01", "customerName": "Online Guest", "phone": "+491111111",
            "items": [{"productId": "product-1", "quantity": 1}]
        })
        self.assertEqual(status, 201)
        self.assertTrue(result["orderNumber"].startswith("MG-ON-"))
        today = time.strftime("%Y-%m-%d", time.localtime())
        status, dated_orders = self.request("GET", "/api/orders?fromDate="+today+"&toDate="+today, token=token)
        self.assertEqual(status, 200)
        self.assertTrue(any(item["id"]=="public-public-order-01" for item in dated_orders["items"]))
        status, dated_sales = self.request("GET", "/api/reports/sales-by-product?fromDate="+today+"&toDate="+today, token=token)
        self.assertEqual(status, 200)
        self.assertTrue(any(item["productId"]=="product-1" and item["quantity"]>=1 for item in dated_sales["items"]))
        status, future_orders = self.request("GET", "/api/orders?fromDate=2099-01-01&toDate=2099-01-01", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(future_orders["items"], [])
        status, invalid_sales_range = self.request("GET", "/api/reports/sales-by-product?fromDate=2026-11-18&toDate=2026-11-17", token=token)
        self.assertEqual(status, 400)
        db = server.connect()
        try:
            self.assertIsNotNone(db.execute("SELECT 1 FROM orders WHERE id='public-public-order-01'").fetchone())
        finally:
            db.close()
        order_number = result["orderNumber"]
        status, result = self.request("POST", "/api/public/orders", {
            "clientOrderId": "public-order-01", "customerName": "Online Guest", "phone": "+491111111",
            "items": [{"productId": "product-1", "quantity": 1}]
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["orderNumber"], order_number)
        for _ in range(3):
            status, result = self.request("POST", "/api/public/orders", {})
            self.assertEqual(status, 400)
        status, result = self.request("POST", "/api/public/orders", {})
        self.assertEqual(status, 429)
        status, result = self.request("GET", "/api/inventory", token=token)
        self.assertEqual(result["items"][0]["quantity"], 11)

        status, result = self.request("DELETE", "/api/products/product-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/products", token=token)
        self.assertEqual(result["items"][0]["active"], 0)

        status, result = self.request("DELETE", "/api/orders/order-test-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/orders", token=token)
        self.assertEqual(next(x for x in result["items"] if x["id"] == "order-test-1")["status"], "cancelled")
        status, cancelled_order_retry = self.request("POST","/api/orders",{
            "id":"order-test-1","customerId":"customer-1","discount":5000,
            "paymentMethod":"card","items":[{"productId":"product-1","quantity":2}]
        },token)
        self.assertEqual(status,409)
        status, reversed_loyalty = self.request("GET","/api/customers/customer-1/loyalty",token=token)
        self.assertEqual(status,200)
        self.assertEqual(reversed_loyalty["balance"],0)
        self.assertEqual(len(reversed_loyalty["items"]),2)
        self.assertEqual(sum(x["pointsDelta"] for x in reversed_loyalty["items"]),0)
        status, repeated_return = self.request("DELETE", "/api/orders/order-test-1", token=token)
        self.assertEqual(status,200)
        self.assertTrue(repeated_return["duplicate"])
        status, missing_return = self.request("DELETE", "/api/orders/not-found", token=token)
        self.assertEqual(status,404)
        db = server.connect()
        try:
            restored = db.execute("SELECT COALESCE(SUM(quantity_delta),0) FROM inventory_movements WHERE product_id='product-1'").fetchone()[0]
            self.assertEqual(restored, 13)
            # A returned order must reverse both revenue and cost of goods sold.
            returned_order = db.execute("SELECT tenant_id,order_number FROM orders WHERE id='order-test-1'").fetchone()
            tenant_id, returned_order_number = returned_order["tenant_id"], returned_order["order_number"]
            cogs_net = db.execute("""SELECT COALESCE(SUM(l.debit-l.credit),0)
                FROM journal_lines l JOIN accounts a ON a.id=l.account_id
                JOIN journal_entries e ON e.id=l.entry_id
                WHERE a.tenant_id=? AND a.code='5100' AND (
                    (e.source_type='order_cogs' AND e.source_id='order-test-1') OR
                    (e.source_type='inventory_movement' AND e.source_id IN
                        (SELECT id FROM inventory_movements WHERE tenant_id=? AND note=?))
                )""", (tenant_id,tenant_id,"برگشت سفارش "+returned_order_number)).fetchone()[0]
            self.assertEqual(cogs_net, 0)
        finally:
            db.close()
        status, trial_after_return = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(trial_after_return["totalDebit"], trial_after_return["totalCredit"])
        status, balance_after_return = self.request("GET", "/api/accounting/balance-sheet", token=token)
        self.assertTrue(balance_after_return["balanced"])

        status, result = self.request("DELETE", "/api/transactions/tx-test-1", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/transactions", token=token)
        self.assertEqual(len(result["items"]), 3)
        self.assertIn("order-order-credit-1", [x["id"] for x in result["items"]])
        self.assertIn("tx-expense-retry", [x["id"] for x in result["items"]])
        status, result = self.request("GET", "/api/accounting/profit-loss", token=token)
        self.assertEqual(result["netProfit"], -65000)
        status, result = self.request("GET", "/api/accounting/journal", token=token)
        self.assertEqual(len(result["items"]), 14)
        self.assertTrue(all(item["debit"] == item["credit"] for item in result["items"]))

        db = server.connect()
        try:
            self.assertGreaterEqual(db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], 3)
        finally:
            db.close()

        status, result = self.request("GET", "/api/backup", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["schema"], "mg-business-server-backup")
        self.assertEqual(result["version"], 4)
        self.assertIn("transactions", result["tables"])
        self.assertIn("suppliers", result["tables"])
        self.assertIn("purchase_invoices", result["tables"])
        self.assertIn("supplier_payments", result["tables"])
        self.assertIn("gamenet_reservations", result["tables"])
        self.assertIn("game_session_segments", result["tables"])
        self.assertIn("accounting_periods", result["tables"])
        self.assertIn("cash_register_shifts", result["tables"])
        self.assertNotIn("password_hash", result["tables"]["users"][0])

        status, result = self.request("POST", "/api/gamenet/devices", {
            "id": "pc-01", "name": "PC-01", "deviceType": "pc",
            "hourlyRate": 120000, "isFree": False
        }, token)
        self.assertEqual(status, 201)
        # Device updates must be handled only by POST; GET must never attempt a write.
        status, result = self.request("GET", "/api/gamenet/devices/pc-01", token=token)
        self.assertEqual(status, 404)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-01", "deviceId": "pc-01", "customerId": "customer-1", "playersCount": 2
        }, token)
        self.assertEqual(status, 201)
        status, retry_session = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-01","deviceId":"pc-01","customerId":"customer-1","playersCount":2
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_session["duplicate"])
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-02", "deviceId": "pc-01", "playersCount": 1
        }, token)
        self.assertEqual(status, 409)
        status, result = self.request("POST", "/api/gamenet/devices", {
            "id":"pc-02","name":"PC-02","deviceType":"pc","hourlyRate":180000,"isFree":False
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/gamenet/devices/pc-01", {
            "name": "PC-01 Updated", "deviceType": "pc", "hourlyRate": 130000, "isFree": False
        }, token)
        self.assertEqual(status, 200)
        db = server.connect()
        try:
            old_start=int(__import__('time').time())-1200
            db.execute("UPDATE game_sessions SET started_at=? WHERE id='session-01'", (old_start,))
            db.execute("UPDATE game_session_segments SET started_at=? WHERE session_id='session-01'", (old_start,))
            db.commit()
        finally:
            db.close()
        status, invalid_adjust = self.request("POST", "/api/gamenet/sessions/session-01/adjust", {
            "playersCount":0
        }, token)
        self.assertEqual(status,400)
        # Moving a running session must not bypass an active reservation for another customer.
        from datetime import datetime, timezone, timedelta
        reservation_start=datetime.now(timezone.utc).isoformat(timespec="seconds")
        reservation_end=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(timespec="seconds")
        status, reserved_device = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-blocking-transfer","deviceId":"pc-02",
            "startsAt":reservation_start,"endsAt":reservation_end,"playersCount":1
        }, token)
        self.assertEqual(status,201)
        status, blocked_transfer = self.request("POST", "/api/gamenet/sessions/session-01/adjust", {
            "deviceId":"pc-02","playersCount":3,"isFree":True
        }, token)
        self.assertEqual(status,409)
        self.assertIn("رزرو", blocked_transfer["error"])
        status, cancelled_reservation = self.request("POST",
            "/api/gamenet/reservations/reservation-blocking-transfer/cancel", {}, token)
        self.assertEqual(status,200)
        status, moved = self.request("POST", "/api/gamenet/sessions/session-01/adjust", {
            "deviceId":"pc-02","playersCount":3,"isFree":True
        }, token)
        self.assertEqual(status,200)
        self.assertEqual(moved["deviceId"],"pc-02")
        self.assertEqual(moved["playersCount"],3)
        self.assertTrue(moved["isFree"])
        db = server.connect()
        try:
            db.execute("UPDATE game_session_segments SET started_at=? WHERE session_id='session-01' AND ended_at IS NULL",
                (int(__import__('time').time())-600,))
            db.commit()
        finally:
            db.close()
        status, result = self.request("POST", "/api/gamenet/sessions/session-01/end", {}, token)
        self.assertEqual(status, 200)
        self.assertEqual(result["paymentMethod"],"credit")
        self.assertGreaterEqual(result["amountDue"], 39000)
        self.assertLess(result["amountDue"], 43000)
        game_charge = result["amountDue"]
        status, retry_end = self.request("POST", "/api/gamenet/sessions/session-01/end", {}, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_end["duplicate"])
        self.assertEqual(retry_end["amountDue"],game_charge)
        self.assertEqual(retry_end["paymentMethod"],"credit")
        # Credit settlement retries must also verify the customer receivable entry.
        db=server.connect()
        try:
            saved_game_receivable=dict(db.execute("""SELECT * FROM customer_ledger WHERE tenant_id=?
                AND source_type='game_session' AND source_id='session-01'""",
                (db.execute("SELECT tenant_id FROM users WHERE username='owner'").fetchone()["tenant_id"],)).fetchone())
            db.execute("DELETE FROM customer_ledger WHERE id=?",(saved_game_receivable["id"],))
            db.commit()
        finally:
            db.close()
        status, missing_game_receivable=self.request("POST","/api/gamenet/sessions/session-01/end",{},token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            columns=list(saved_game_receivable)
            placeholders=",".join("?" for _ in columns)
            db.execute("INSERT INTO customer_ledger("+",".join(columns)+") VALUES("+placeholders+")",
                tuple(saved_game_receivable[column] for column in columns))
            db.commit()
        finally:
            db.close()
        status, result = self.request("GET", "/api/gamenet/devices", token=token)
        self.assertEqual(result["items"][0]["name"], "PC-01 Updated")
        status, result = self.request("GET", "/api/customers", token=token)
        self.assertEqual(next(x for x in result["items"] if x["id"] == "customer-1")["balance"], 35000 + game_charge)
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["status"], "ended")
        status, segments = self.request("GET","/api/gamenet/sessions/session-01/segments",token=token)
        self.assertEqual(status,200)
        self.assertEqual(len(segments["items"]),2)
        self.assertEqual(segments["items"][0]["deviceId"],"pc-01")
        self.assertEqual(segments["items"][1]["deviceId"],"pc-02")
        self.assertEqual(segments["items"][0]["playersCount"],2)
        self.assertEqual(segments["items"][1]["playersCount"],3)
        self.assertTrue(segments["items"][1]["isFree"])
        status, game_backup = self.request("GET","/api/backup",token=token)
        self.assertEqual(status,200)
        self.assertEqual(len(game_backup["tables"]["game_session_segments"]),2)

        status, result = self.request("POST", "/api/gamenet/devices", {
            "id": "pc-free", "name": "Free PC", "deviceType": "pc",
            "hourlyRate": 120000, "isFree": True
        }, token)
        self.assertEqual(status, 201)
        # An unassigned reservation must block sessions too; null customer IDs
        # must never be treated as proof that the reservation belongs to the session.
        from datetime import datetime, timezone, timedelta
        unassigned_start=datetime.now(timezone.utc).isoformat(timespec="seconds")
        unassigned_end=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(timespec="seconds")
        status, unassigned_reservation = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-unassigned","deviceId":"pc-free",
            "startsAt":unassigned_start,"endsAt":unassigned_end,"playersCount":1
        }, token)
        self.assertEqual(status,201)
        status, blocked_unassigned = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-free-blocked","deviceId":"pc-free","playersCount":1
        }, token)
        self.assertEqual(status,409)
        status, cancelled_unassigned = self.request("POST",
            "/api/gamenet/reservations/reservation-unassigned/cancel", {}, token)
        self.assertEqual(status,200)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id": "session-free", "deviceId": "pc-free", "playersCount": 1
        }, token)
        self.assertEqual(status, 201)
        status, result = self.request("POST", "/api/gamenet/sessions/session-free/end", {}, token)
        self.assertEqual(status, 200)
        self.assertEqual(result["amountDue"], 0)
        status, free_retry = self.request("POST", "/api/gamenet/sessions/session-free/end", {}, token)
        self.assertEqual(status,200)
        self.assertTrue(free_retry["duplicate"])
        self.assertEqual(free_retry["amountDue"],0)
        status, result = self.request("POST", "/api/gamenet/devices", {
            "id":"pc-card","name":"Card PC","deviceType":"pc","hourlyRate":120000,"isFree":False
        },token)
        self.assertEqual(status,201)
        status, result = self.request("POST","/api/gamenet/sessions",{
            "id":"session-card","deviceId":"pc-card","customerId":"customer-1","playersCount":1
        },token)
        self.assertEqual(status,201)
        db=server.connect()
        try:
            card_start=int(__import__('time').time())-120
            db.execute("UPDATE game_sessions SET started_at=? WHERE id='session-card'",(card_start,))
            db.execute("UPDATE game_session_segments SET started_at=? WHERE session_id='session-card'",(card_start,))
            db.commit()
        finally:
            db.close()
        status, card_settlement = self.request("POST","/api/gamenet/sessions/session-card/end",{
            "paymentMethod":"card"
        },token)
        self.assertEqual(status,200)
        self.assertEqual(card_settlement["paymentMethod"],"card")
        self.assertGreater(card_settlement["amountDue"],0)
        status, changed_card_retry = self.request("POST","/api/gamenet/sessions/session-card/end",{
            "paymentMethod":"credit"
        },token)
        self.assertEqual(status,409)
        status, card_retry = self.request("POST","/api/gamenet/sessions/session-card/end",{
            "paymentMethod":"card"
        },token)
        self.assertEqual(status,200)
        self.assertTrue(card_retry["duplicate"])
        self.assertEqual(card_retry["paymentMethod"],"card")
        # A voided settlement transaction must not be accepted as a successful retry.
        db=server.connect()
        try:
            db.execute("UPDATE transactions SET status='voided' WHERE id='game-session-card'")
            db.commit()
        finally:
            db.close()
        status, voided_game_retry=self.request("POST","/api/gamenet/sessions/session-card/end",{
            "paymentMethod":"card"
        },token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            db.execute("UPDATE transactions SET status='posted' WHERE id='game-session-card'")
            db.commit()
        finally:
            db.close()
        # An ended GameNet session cannot report a successful retry after its journal is damaged.
        db=server.connect()
        try:
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            tenant_id=db.execute("SELECT tenant_id FROM users WHERE username='owner'").fetchone()["tenant_id"]
            entry_id=db.execute("""SELECT id FROM journal_entries WHERE tenant_id=?
                AND source_type='transaction' AND source_id='game-session-card'""",(tenant_id,)).fetchone()["id"]
            saved_game_lines=[dict(row) for row in db.execute(
                "SELECT * FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,entry_id))]
            db.execute("DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,entry_id))
            db.commit()
        finally:
            db.close()
        status, damaged_game_retry=self.request("POST","/api/gamenet/sessions/session-card/end",{
            "paymentMethod":"card"
        },token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            columns=list(saved_game_lines[0])
            placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(line[column] for column in columns) for line in saved_game_lines])
            db.commit()
        finally:
            db.close()
        status, customers_after_card = self.request("GET","/api/customers",token=token)
        self.assertEqual(next(x for x in customers_after_card["items"] if x["id"]=="customer-1")["balance"],35000+game_charge)
        db=server.connect()
        try:
            paid_receivable=db.execute("""SELECT COUNT(*) FROM customer_ledger
                WHERE tenant_id=(SELECT tenant_id FROM users WHERE username='owner')
                AND source_type='game_session' AND source_id='session-card'""").fetchone()[0]
            self.assertEqual(paid_receivable,0)
            card_debit=db.execute("""SELECT SUM(l.debit) FROM journal_lines l
                JOIN accounts a ON a.id=l.account_id JOIN journal_entries e ON e.id=l.entry_id
                WHERE e.source_type='transaction' AND e.source_id='game-session-card' AND a.code='1010'""").fetchone()[0]
            self.assertEqual(card_debit,card_settlement["amountDue"])
        finally:
            db.close()

        # Supplier invoices must increase stock, create payable journal entries, and settle atomically.
        status, result = self.request("POST", "/api/products", {
            "id":"product-purchase-test","name":"Milk","sku":"MILK-01",
            "sellPrice":40000,"costPrice":20000
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/products", {
            "id":"product-purchase-test-2","name":"Coffee Beans","sku":"BEANS-01",
            "sellPrice":80000,"costPrice":35000
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/suppliers", {
            "id":"supplier-1","name":"Test Dairy","phone":"+49123456789"
        }, token)
        self.assertEqual(status,201)
        status, duplicate_supplier = self.request("POST", "/api/suppliers", {
            "id":"supplier-duplicate","name":"Test Dairy"
        }, token)
        self.assertEqual(status,409)
        status, duplicate_supplier_id = self.request("POST", "/api/suppliers", {
            "id":"supplier-1","name":"Second Supplier"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("POST", "/api/purchase-invoices", {
            "id":"purchase-invoice-1","supplierId":"supplier-1","invoiceNumber":"PI-1001",
            "invoiceDate":"2026-11-16","discount":30001,
            "items":[{"productId":"product-purchase-test","quantity":10,"unitCost":25000},{"productId":"product-purchase-test-2","quantity":5,"unitCost":40000}]
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["total"],419999)
        db=server.connect()
        try:
            discounted_lines=db.execute("""SELECT product_id,SUM(quantity) AS quantity,
                SUM(line_total) AS line_total FROM purchase_invoice_items
                WHERE tenant_id=(SELECT tenant_id FROM users WHERE username=?)
                AND invoice_id='purchase-invoice-1' GROUP BY product_id""",("owner",)).fetchall()
            by_product={row["product_id"]:row for row in discounted_lines}
            self.assertEqual(by_product["product-purchase-test"]["line_total"],233333)
            self.assertEqual(by_product["product-purchase-test-2"]["line_total"],186666)
            self.assertEqual(sum(row["line_total"] for row in discounted_lines),419999)
            self.assertEqual(sum(row["quantity"] for row in discounted_lines),15)
        finally:
            db.close()
        status, retry_invoice = self.request("POST", "/api/purchase-invoices", {
            "id":"purchase-invoice-1","supplierId":"supplier-1","invoiceNumber":"PI-1001",
            "invoiceDate":"2026-11-16","discount":30001,
            "items":[{"productId":"product-purchase-test","quantity":10,"unitCost":25000},{"productId":"product-purchase-test-2","quantity":5,"unitCost":40000}]
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_invoice["duplicate"])
        # An invoice retry must not conceal a missing inventory journal.
        db=server.connect()
        try:
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            tenant_id=db.execute("SELECT tenant_id FROM users WHERE username=?",("owner",)).fetchone()["tenant_id"]
            movement_id=db.execute("""SELECT id FROM inventory_movements WHERE tenant_id=?
                AND movement_type='purchase' AND note='فاکتور خرید PI-1001' LIMIT 1""",(tenant_id,)).fetchone()["id"]
            movement_journal=db.execute("""SELECT id FROM journal_entries WHERE tenant_id=?
                AND source_type='inventory_movement' AND source_id=?""",(tenant_id,movement_id)).fetchone()["id"]
            saved_movement_lines=[dict(row) for row in db.execute(
                "SELECT * FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,movement_journal))]
            db.execute("DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,movement_journal))
            db.commit()
        finally:
            db.close()
        status, damaged_invoice_retry=self.request("POST", "/api/purchase-invoices", {
            "id":"purchase-invoice-1","supplierId":"supplier-1","invoiceNumber":"PI-1001",
            "invoiceDate":"2026-11-16","discount":30001,
            "items":[{"productId":"product-purchase-test","quantity":10,"unitCost":25000},{"productId":"product-purchase-test-2","quantity":5,"unitCost":40000}]
        }, token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            columns=list(saved_movement_lines[0])
            placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(line[column] for column in columns) for line in saved_movement_lines])
            db.commit()
        finally:
            db.close()
        status, duplicate_invoice = self.request("POST", "/api/purchase-invoices", {
            "id":"purchase-invoice-duplicate","supplierId":"supplier-1","invoiceNumber":"PI-1001",
            "invoiceDate":"2026-11-16","discount":0,
            "items":[{"productId":"product-purchase-test","quantity":1,"unitCost":25000}]
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("GET", "/api/inventory", token=token)
        self.assertEqual(next(x for x in result["items"] if x["productId"]=="product-purchase-test")["quantity"],10)
        self.assertEqual(next(x for x in result["items"] if x["productId"]=="product-purchase-test-2")["quantity"],5)
        status, result = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "id":"supplier-payment-01","amount":100000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["remainingBalance"],319999)
        status, partial_statement = self.request("GET", "/api/suppliers/supplier-1/ledger", token=token)
        self.assertEqual(status,200)
        self.assertEqual(partial_statement["totalPurchases"],419999)
        self.assertEqual(partial_statement["totalPayments"],100000)
        self.assertEqual(partial_statement["balance"],319999)
        status, unknown_supplier = self.request("GET", "/api/suppliers/missing-supplier/ledger", token=token)
        self.assertEqual(status,404)
        # A retry must reject invoice totals/status that no longer reconcile to supplier payment rows.
        db=server.connect()
        try:
            db.execute("UPDATE purchase_invoices SET paid_amount=100001 WHERE id='purchase-invoice-1'")
            db.commit()
        finally:
            db.close()
        status, inconsistent_invoice_retry = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "id":"supplier-payment-01","amount":100000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            db.execute("UPDATE purchase_invoices SET paid_amount=100000,status='partial' WHERE id='purchase-invoice-1'")
            db.commit()
        finally:
            db.close()
        status, duplicate_supplier_payment = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "id":"supplier-payment-01","amount":100000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(duplicate_supplier_payment["duplicate"])
        self.assertEqual(duplicate_supplier_payment["remainingBalance"],319999)
        # An idempotent retry must not report success if its accounting journal was damaged.
        db=server.connect()
        try:
            db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
            tenant_id=db.execute("SELECT tenant_id FROM users WHERE username=?",("owner",)).fetchone()["tenant_id"]
            journal_id=db.execute("""SELECT id FROM journal_entries
                WHERE tenant_id=? AND source_type='supplier_payment' AND source_id=?""",
                (tenant_id,"supplier-payment-01")).fetchone()["id"]
            saved_payment_lines=[dict(row) for row in db.execute(
                "SELECT * FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id)
            ).fetchall()]
            db.execute("DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id))
            db.commit()
        finally:
            db.close()
        status, corrupted_supplier_retry=self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "id":"supplier-payment-01","amount":100000,"paymentMethod":"card"
        }, token)
        self.assertEqual(status,409)
        db=server.connect()
        try:
            columns=list(saved_payment_lines[0])
            placeholders=",".join("?" for _ in columns)
            db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                [tuple(line[column] for column in columns) for line in saved_payment_lines])
            db.commit()
        finally:
            db.close()
        status, changed_supplier_method = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "id":"supplier-payment-01","amount":100000,"paymentMethod":"cash"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "amount":319999,"paymentMethod":"cash"
        }, token)
        self.assertEqual(status,201)
        self.assertEqual(result["status"],"paid")
        self.assertEqual(result["remainingBalance"],0)
        status, statement = self.request("GET", "/api/suppliers/supplier-1/ledger", token=token)
        self.assertEqual(status,200)
        self.assertEqual(statement["totalPurchases"],419999)
        self.assertEqual(statement["totalPayments"],419999)
        self.assertEqual(statement["balance"],0)
        self.assertEqual(len(statement["items"]),3)
        self.assertEqual([row["balanceAfter"] for row in statement["items"]],[0,319999,419999])
        self.assertEqual(statement["items"][0]["sourceType"],"payment")
        self.assertEqual(statement["items"][0]["balanceAfter"],0)
        self.assertEqual(statement["items"][-1]["sourceType"],"invoice")
        self.assertEqual(statement["items"][-1]["balanceAfter"],419999)
        status, result = self.request("POST", "/api/purchase-invoices/purchase-invoice-1/payments", {
            "amount":1,"paymentMethod":"cash"
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("GET", "/api/purchase-invoices", token=token)
        self.assertEqual(result["items"][0]["status"],"paid")
        status, trial = self.request("GET", "/api/accounting/trial-balance", token=token)
        self.assertEqual(trial["totalDebit"],trial["totalCredit"])

        # GameNet reservations prevent overlapping bookings and bind check-in to the reserved customer.
        datetime_module=__import__("datetime")
        start_at=datetime_module.datetime.fromtimestamp(time.time()-10,datetime_module.timezone.utc).isoformat(timespec="seconds")
        end_at=datetime_module.datetime.fromtimestamp(time.time()+3600,datetime_module.timezone.utc).isoformat(timespec="seconds")
        naive_start=datetime_module.datetime.fromtimestamp(time.time()+7200).isoformat(timespec="seconds")
        naive_end=datetime_module.datetime.fromtimestamp(time.time()+10800).isoformat(timespec="seconds")
        status, invalid_timezone=self.request("POST","/api/gamenet/reservations",{
            "id":"reservation-naive-time","deviceId":"pc-01","startsAt":naive_start,"endsAt":naive_end
        },token)
        self.assertEqual(status,400)
        status, invalid_customer = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-invalid-customer","deviceId":"pc-free","customerId":["customer-1"],
            "startsAt":start_at,"endsAt":end_at
        },token)
        self.assertEqual(status,400)
        status, invalid_device = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-invalid-device","deviceId":"d"*101,
            "startsAt":start_at,"endsAt":end_at
        },token)
        self.assertEqual(status,400)
        status, reservation = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-01","deviceId":"pc-free","customerId":"customer-1",
            "startsAt":start_at,"endsAt":end_at,"playersCount":2,"notes":"Birthday booking"
        }, token)
        self.assertEqual(status,201)
        status, malformed_session = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-invalid-customer","deviceId":"pc-free","customerId":["customer-1"],"playersCount":1
        },token)
        self.assertEqual(status,400)
        status, malformed_device = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-invalid-device","deviceId":"d"*101,"playersCount":1
        },token)
        self.assertEqual(status,400)
        status, retry_reservation = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-01","deviceId":"pc-free","customerId":"customer-1",
            "startsAt":start_at,"endsAt":end_at,"playersCount":2,"notes":"Birthday booking"
        }, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_reservation["duplicate"])
        status, conflict = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-02","deviceId":"pc-free","customerId":"customer-1",
            "startsAt":start_at,"endsAt":end_at,"playersCount":1
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-wrong-customer","deviceId":"pc-free","playersCount":1
        }, token)
        self.assertEqual(status,409)
        status, result = self.request("POST", "/api/gamenet/sessions", {
            "id":"session-reserved","deviceId":"pc-free","customerId":"customer-1","playersCount":2
        }, token)
        self.assertEqual(status,201)
        status, reservations = self.request("GET", "/api/gamenet/reservations", token=token)
        self.assertEqual(status,200)
        self.assertEqual(next(x for x in reservations["items"] if x["id"]=="reservation-01")["status"],"completed")
        status, result = self.request("POST", "/api/gamenet/sessions/session-reserved/end", {}, token)
        self.assertEqual(status,200)
        self.assertEqual(result["amountDue"],0)
        future_start=datetime_module.datetime.fromtimestamp(time.time()+7200,datetime_module.timezone.utc).isoformat(timespec="seconds")
        future_end=datetime_module.datetime.fromtimestamp(time.time()+10800,datetime_module.timezone.utc).isoformat(timespec="seconds")
        status, result = self.request("POST", "/api/gamenet/reservations", {
            "id":"reservation-cancel","deviceId":"pc-01","startsAt":future_start,"endsAt":future_end
        }, token)
        self.assertEqual(status,201)
        status, result = self.request("POST", "/api/gamenet/reservations/reservation-cancel/cancel", {}, token)
        self.assertEqual(status,200)
        self.assertFalse(result["duplicate"])
        status, retry_cancel = self.request("POST", "/api/gamenet/reservations/reservation-cancel/cancel", {}, token)
        self.assertEqual(status,200)
        self.assertTrue(retry_cancel["duplicate"])
        status, missing_cancel = self.request("POST", "/api/gamenet/reservations/not-found/cancel", {}, token)
        self.assertEqual(status,404)
        # Include non-empty license state so backup/restore exercises its FK order.
        db=server.connect()
        try:
            backup_tenant_id=db.execute("SELECT tenant_id FROM users WHERE username=?",("owner",)).fetchone()["tenant_id"]
            db.execute("""INSERT INTO licenses(id,tenant_id,code_hash,plan,expires_at,activated_at,status)
                VALUES(?,?,?,?,?,?,?)""",("backup-license-test",backup_tenant_id,"backup-license-hash-test",
                "test",int(time.time())+86400,int(time.time()),"active"))
            db.execute("""INSERT INTO tenant_modules(tenant_id,module_key,enabled,expires_at,license_id,updated_at)
                VALUES(?,?,?,?,?,?)""",(backup_tenant_id,"backup-test",1,None,"backup-license-test",int(time.time())))
            db.commit()
        finally:
            db.close()
        status, backup_user = self.request("POST","/api/users",{
            "username":"backup-preserved-user","password":"backup-preserved-password-2026","role":"viewer"
        },token)
        self.assertEqual(status,201)
        status, snapshot = self.request("GET", "/api/backup", token=token)
        self.assertEqual(status, 200)
        self.assertTrue(any(row["id"]=="backup-license-test" for row in snapshot["tables"]["licenses"]))
        self.assertTrue(any(row["username"]=="backup-preserved-user" for row in snapshot["tables"]["users"]))
        # A snapshot containing a running GameNet session is unsafe to restore.
        running_snapshot = json.loads(json.dumps(snapshot))
        running_snapshot["tables"]["game_sessions"][0]["status"] = "running"
        status, running_result = self.request("POST", "/api/backup/restore", running_snapshot, token)
        self.assertEqual(status, 409)
        self.assertIn("نشست‌های فعال", running_result["error"])
        # Live sessions must also be closed before restore, even with a clean snapshot.
        db=server.connect()
        try:
            db.execute("UPDATE game_sessions SET status='running' WHERE id='session-01'")
            db.commit()
        finally:
            db.close()
        status, live_result = self.request("POST", "/api/backup/restore", snapshot, token)
        self.assertEqual(status, 409)
        self.assertIn("نشست فعال", live_result["error"])
        db=server.connect()
        try:
            db.execute("UPDATE game_sessions SET status='ended' WHERE id='session-01'")
            db.commit()
        finally:
            db.close()
        # Restore must not roll back current credentials, roles, or account activation.
        status, changed_backup_user = self.request("POST","/api/users/"+backup_user["id"]+"/role",{"role":"cashier"},token)
        self.assertEqual(status,200)
        self.assertTrue(any(row["module_key"]=="backup-test" for row in snapshot["tables"]["tenant_modules"]))
        self.assertIn("licenses",snapshot["tables"])
        self.assertIn("tenant_modules",snapshot["tables"])
        altered = json.loads(json.dumps(snapshot))
        altered["tenant"]["id"] = "different-workspace"
        status, result = self.request("POST", "/api/backup/restore", altered, token)
        self.assertEqual(status, 400)
        missing_chart = json.loads(json.dumps(snapshot))
        missing_chart["tables"]["accounts"] = [
            row for row in missing_chart["tables"]["accounts"] if row["code"] != "5100"
        ]
        status, result = self.request("POST", "/api/backup/restore", missing_chart, token)
        self.assertEqual(status, 400)
        incomplete_snapshot = json.loads(json.dumps(snapshot))
        incomplete_snapshot["tables"].pop("orders")
        status, incomplete_result = self.request(
            "POST", "/api/backup/restore", incomplete_snapshot, token
        )
        self.assertEqual(status,400)
        self.assertIn("ناقص",incomplete_result["error"])
        # Rejected partial snapshots must not delete any existing workspace data.
        status, sessions_after_incomplete = self.request("GET","/api/gamenet/sessions",token=token)
        self.assertEqual(status,200)
        self.assertGreaterEqual(len(sessions_after_incomplete["items"]),2)
        unbalanced = json.loads(json.dumps(snapshot))
        unbalanced["tables"]["journal_lines"][0]["debit"] += 1
        status, result = self.request("POST", "/api/backup/restore", unbalanced, token)
        self.assertEqual(status, 400)
        malformed_journal_reference = json.loads(json.dumps(snapshot))
        malformed_journal_reference["tables"]["journal_lines"][0]["entry_id"] = ["invalid", "unhashable"]
        status, result = self.request("POST", "/api/backup/restore", malformed_journal_reference, token)
        self.assertEqual(status, 400)
        self.assertIn("شناسه سند", result["error"])
        broken = json.loads(json.dumps(snapshot))
        broken["tables"]["order_items"].append({
            "id": "broken-backup-item", "tenant_id": broken["tenant"]["id"],
            "order_id": "missing-order", "product_id": "missing-product",
            "product_name": "Broken", "quantity": 1, "unit_price": 1,
            "unit_cost": 0, "line_total": 1
        })
        status, result = self.request("POST", "/api/backup/restore", broken, token)
        self.assertEqual(status, 409)
        invalid_sql_value = json.loads(json.dumps(snapshot))
        invalid_sql_value["tables"]["products"][0]["sell_price"] = {"unexpected": "object"}
        status, result = self.request("POST", "/api/backup/restore", invalid_sql_value, token)
        self.assertEqual(status, 409)
        self.assertIn("نوع نامعتبر", result["error"])
        # A failed restore must leave the pre-restore workspace intact.
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result["items"]), 2)
        # Reversal rows may share a timestamp with their source. Deliberately
        # reverse snapshot order to prove restore honors the self-referencing FK.
        journal_rows = snapshot["tables"]["journal_entries"]
        self.assertTrue(any(row.get("reversal_of") for row in journal_rows))
        snapshot["tables"]["journal_entries"] = list(reversed(journal_rows))
        status, result = self.request("POST", "/api/backup/restore", snapshot, token)
        self.assertEqual(status, 200)
        self.assertGreater(result["restoredRows"], 0)
        db=server.connect()
        try:
            preserved_user=db.execute("SELECT role,active FROM users WHERE id=? AND tenant_id=?",
                (backup_user["id"],backup_tenant_id)).fetchone()
            self.assertEqual(preserved_user["role"],"cashier")
            self.assertEqual(preserved_user["active"],1)
        finally:
            db.close()
        db=server.connect()
        try:
            self.assertIsNotNone(db.execute("SELECT 1 FROM licenses WHERE id=? AND tenant_id=?",
                ("backup-license-test",backup_tenant_id)).fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM tenant_modules WHERE tenant_id=? AND module_key=?",
                (backup_tenant_id,"backup-test")).fetchone())
        finally:
            db.close()
        status, restored_loyalty = self.request("GET","/api/customers/customer-1/loyalty",token=token)
        self.assertEqual(status,200)
        self.assertEqual(restored_loyalty["balance"],0)
        self.assertEqual(len(restored_loyalty["items"]),2)
        db = server.connect()
        try:
            journal_id = db.execute("SELECT id FROM journal_entries LIMIT 1").fetchone()[0]
            with self.assertRaises(Exception):
                db.execute("UPDATE journal_entries SET memo='tampered-after-restore' WHERE id=?", (journal_id,))
                db.commit()
            db.rollback()
        finally:
            db.close()
        status, result = self.request("GET", "/api/gamenet/sessions", token=token)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(result["items"]), 2)
        status, restored_purchases = self.request("GET", "/api/purchase-invoices", token=token)
        self.assertEqual(status,200)
        self.assertEqual(restored_purchases["items"][0]["status"],"paid")
        status, restored_reservations = self.request("GET", "/api/gamenet/reservations", token=token)
        self.assertEqual(status,200)
        self.assertEqual(next(x for x in restored_reservations["items"] if x["id"]=="reservation-01")["status"],"completed")
        self.assertEqual(next(x for x in restored_reservations["items"] if x["id"]=="reservation-cancel")["status"],"cancelled")

        status, result = self.request("GET", "/api/me")
        self.assertEqual(status, 401)

        # Logout must not mutate state through a safe GET request.
        status, result = self.request("GET", "/api/logout", token=token)
        self.assertNotEqual(status, 200)
        status, result = self.request("GET", "/api/me", token=token)
        self.assertEqual(status, 200)
        status, result = self.request("POST", "/api/logout", {}, token=token)
        self.assertEqual(status, 200)
        status, result = self.request("GET", "/api/me", token=token)
        self.assertEqual(status, 401)

    def test_owner_can_change_user_roles_and_revoke_existing_sessions(self):
        original_db_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"user-role-change.sqlite3"
        try:
            status,setup=self.request("POST","/api/setup",{
                "businessName":"Role Change","username":"role-owner",
                "password":"role-owner-password-2026"
            })
            self.assertEqual(status,200)
            owner_token=setup["token"]
            status,manager=self.request("POST","/api/users",{
                "username":"role-manager","password":"role-manager-password-2026","role":"manager"
            },owner_token)
            self.assertEqual(status,201)
            status,cashier=self.request("POST","/api/users",{
                "username":"role-cashier","password":"role-cashier-password-2026","role":"cashier"
            },owner_token)
            self.assertEqual(status,201)
            status,manager_login=self.request("POST","/api/login",{
                "username":"role-manager","password":"role-manager-password-2026"
            })
            self.assertEqual(status,200)
            status,denied=self.request("POST",f"/api/users/{cashier['id']}/role",{"role":"viewer"},manager_login["token"])
            self.assertEqual(status,403)
            status,cashier_login=self.request("POST","/api/login",{
                "username":"role-cashier","password":"role-cashier-password-2026"
            })
            self.assertEqual(status,200)
            status,changed=self.request("POST",f"/api/users/{cashier['id']}/role",{"role":"viewer"},owner_token)
            self.assertEqual(status,200)
            self.assertTrue(changed["sessionsRevoked"])
            status,old_session=self.request("GET","/api/me",token=cashier_login["token"])
            self.assertEqual(status,401)
            status,viewer_login=self.request("POST","/api/login",{
                "username":"role-cashier","password":"role-cashier-password-2026"
            })
            self.assertEqual(status,200)
            self.assertEqual(viewer_login["user"]["role"],"viewer")
            status,denied_users=self.request("GET","/api/users",token=viewer_login["token"])
            self.assertEqual(status,403)
            status,invalid=self.request("POST",f"/api/users/{cashier['id']}/role",{"role":"owner"},owner_token)
            self.assertEqual(status,400)
            status,self_change=self.request("POST",f"/api/users/{setup['user']['id']}/role",{"role":"viewer"},owner_token)
            self.assertEqual(status,400)
            status,missing=self.request("POST","/api/users/nonexistent-role-target/role",{"role":"manager"},owner_token)
            self.assertEqual(status,404)
            status,no_change=self.request("POST",f"/api/users/{cashier['id']}/role",{"role":"viewer"},owner_token)
            self.assertEqual(status,200)
            self.assertFalse(no_change["changed"])
            self.assertFalse(no_change["sessionsRevoked"])
            # Inactive accounts must not be re-enabled or have their role altered
            # through the role editor; the endpoint should leave the stored role intact.
            db=server.connect()
            try:
                db.execute("UPDATE users SET active=0 WHERE id=?",
                           (cashier["id"],))
                db.commit()
            finally:
                db.close()
            status,inactive=self.request("POST",f"/api/users/{cashier['id']}/role",{"role":"manager"},owner_token)
            self.assertEqual(status,409)
            db=server.connect()
            try:
                stored=db.execute("SELECT role,active FROM users WHERE id=?",
                                  (cashier["id"],)).fetchone()
                self.assertEqual(stored["role"],"viewer")
                self.assertEqual(stored["active"],0)
            finally:
                db.close()
            # A valid user ID from another tenant must remain invisible to this owner.
            db=server.connect()
            try:
                now=int(time.time())
                db.execute("INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)",
                           ("foreign-role-tenant","Foreign Role Tenant",now))
                db.execute("""INSERT INTO users(id,tenant_id,username,password_hash,role,active,created_at)
                    VALUES(?,?,?,?,?,1,?)""",
                    ("foreign-role-user","foreign-role-tenant","foreign-role-user",
                     server.password_hash("foreign-role-password-2026"),"manager",now))
                db.commit()
            finally:
                db.close()
            status,foreign=self.request("POST","/api/users/foreign-role-user/role",
                                        {"role":"viewer"},owner_token)
            self.assertEqual(status,404)
            status,foreign_deactivation=self.request("DELETE","/api/users/foreign-role-user",token=owner_token)
            self.assertEqual(status,404)
            db=server.connect()
            try:
                foreign_user=db.execute("SELECT role,active FROM users WHERE id='foreign-role-user'").fetchone()
                self.assertEqual(foreign_user["role"],"manager")
                self.assertEqual(foreign_user["active"],1)
            finally:
                db.close()
            status,audit=self.request("GET","/api/audit-log",token=owner_token)
            self.assertEqual(status,200)
            role_events=[item for item in audit["items"] if item["action"]=="user.role.changed" and item["entityId"]==cashier["id"]]
            self.assertEqual(len(role_events),1)
            self.assertEqual(role_events[0]["details"],{"from":"cashier","to":"viewer"})
        finally:
            server.DB_PATH=original_db_path

    def test_public_orders_support_addons_and_reject_reused_ids_with_changed_content(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "public-addons-test.sqlite3"
        with server.LOGIN_LOCK:
            server.PUBLIC_ORDER_FAILURES.pop("127.0.0.1",None)
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName":"Online Addon Cafe","username":"online-addon-owner",
                "password":"online-addon-owner-password-2026"
            })
            self.assertEqual(status,200)
            token=setup["token"]
            code=make_license(["cafe","inventory","accounting"],"public-addon-license")
            status, _ = self.request("POST","/api/licenses/activate",{"code":code},token)
            self.assertEqual(status,200)
            status, product = self.request("POST","/api/products",{
                "id":"public-addon-product","name":"Latte","sellPrice":100,"costPrice":30
            },token)
            self.assertEqual(status,201)
            db=server.connect()
            try:
                tenant_id=db.execute("SELECT tenant_id FROM users WHERE username='online-addon-owner'").fetchone()[0]
                db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,
                    quantity_delta,unit_cost,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("public-addon-stock",tenant_id,"public-addon-product",setup["user"]["id"],"purchase",4,30,"opening",int(time.time())))
                db.commit()
            finally:
                db.close()
            status, addon = self.request("POST","/api/product-addons",{
                "id":"public-addon-01","productId":"public-addon-product","name":"Oat milk","price":25
            },token)
            self.assertEqual(status,201)
            status, menu = self.request("GET","/api/public/menu")
            self.assertEqual(status,200)
            menu_product=next(x for x in menu["items"] if x["id"]=="public-addon-product")
            self.assertEqual(menu_product["addons"],[{"id":"public-addon-01","productId":"public-addon-product","name":"Oat milk","price":25}])
            payload={
                "clientOrderId":"public-addon-order-01","customerName":"Online Guest",
                "phone":"+49123456789",
                "items":[{"productId":"public-addon-product","quantity":2,"addons":["public-addon-01"]}]
            }
            status, order = self.request("POST","/api/public/orders",payload)
            self.assertEqual(status,201)
            self.assertEqual(order["total"],250)
            db=server.connect()
            try:
                public_fulfillment=db.execute("SELECT fulfillment_status FROM orders WHERE id=?",
                    ("public-public-addon-order-01",)).fetchone()["fulfillment_status"]
                self.assertEqual(public_fulfillment,"queued",
                    "new public QR orders must enter the kitchen queue")
            finally:
                db.close()
            # Simulate an installation upgraded from schema 12: migration 13
            # must backfill a fingerprint before later customer profile edits.
            db=server.connect()
            try:
                db.execute("UPDATE orders SET request_fingerprint=NULL WHERE id='public-public-addon-order-01'")
                db.execute("DELETE FROM schema_migrations WHERE version=13")
                db.commit()
            finally:
                db.close()
            server.connect().close()
            # Customer profiles are mutable; a retry must remain idempotent even
            # if the customer record changes after the original order was accepted.
            db=server.connect()
            try:
                fingerprint=db.execute("SELECT request_fingerprint FROM orders WHERE id='public-public-addon-order-01'").fetchone()[0]
                self.assertTrue(fingerprint)
                db.execute("UPDATE customers SET full_name=?,phone=? WHERE phone=?",
                           ("Updated Profile","+49123456780","+49123456789"))
                db.commit()
            finally:
                db.close()
            status, duplicate = self.request("POST","/api/public/orders",payload)
            self.assertEqual(status,200)
            self.assertTrue(duplicate["duplicate"])
            changed=dict(payload)
            changed["items"]=[{"productId":"public-addon-product","quantity":2}]
            status, conflict = self.request("POST","/api/public/orders",changed)
            self.assertEqual(status,409)
            # Backup/restore must preserve the idempotency fingerprint, not
            # silently fall back to mutable customer profile fields.
            status, public_snapshot=self.request("GET","/api/backup",token=token)
            self.assertEqual(status,200)
            db=server.connect()
            try:
                db.execute("UPDATE customers SET full_name=?,phone=? WHERE phone=?",
                           ("Profile Before Restore","+49123456781","+49123456780"))
                db.commit()
            finally:
                db.close()
            status, restored=self.request("POST","/api/backup/restore",public_snapshot,token)
            self.assertEqual(status,200)
            db=server.connect()
            try:
                db.execute("UPDATE customers SET full_name=?,phone=? WHERE phone=?",
                           ("Profile After Restore","+49123456782","+49123456789"))
                db.commit()
            finally:
                db.close()
            status, restored_duplicate=self.request("POST","/api/public/orders",payload)
            self.assertEqual(status,200)
            self.assertTrue(restored_duplicate["duplicate"])
            # Public retry must reject a missing revenue posting even with a valid fingerprint.
            db=server.connect()
            try:
                db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
                tenant_id=db.execute("SELECT tenant_id FROM users WHERE username='online-addon-owner'").fetchone()["tenant_id"]
                journal_id=db.execute("""SELECT id FROM journal_entries WHERE tenant_id=?
                    AND source_type='transaction' AND source_id=?""",
                    (tenant_id,"order-public-public-addon-order-01")).fetchone()["id"]
                saved_public_lines=[dict(row) for row in db.execute(
                    "SELECT * FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id)
                ).fetchall()]
                db.execute("DELETE FROM journal_lines WHERE tenant_id=? AND entry_id=?",(tenant_id,journal_id))
                db.commit()
            finally:
                db.close()
            status, damaged_public_retry=self.request("POST","/api/public/orders",payload)
            self.assertEqual(status,409)
            db=server.connect()
            try:
                columns=list(saved_public_lines[0]); placeholders=",".join("?" for _ in columns)
                db.executemany("INSERT INTO journal_lines("+",".join(columns)+") VALUES("+placeholders+")",
                    [tuple(row[column] for column in columns) for row in saved_public_lines])
                db.execute("""CREATE TRIGGER IF NOT EXISTS journal_lines_no_delete BEFORE DELETE ON journal_lines
                    BEGIN SELECT RAISE(ABORT,'journal lines are immutable'); END""")
                db.commit()
            finally:
                db.close()
            db=server.connect()
            try:
                item=db.execute("SELECT unit_price,line_total,addons_json FROM order_items WHERE order_id='public-public-addon-order-01'").fetchone()
                self.assertEqual((item["unit_price"],item["line_total"]),(125,250))
                self.assertEqual(json.loads(item["addons_json"]),[{"id":"public-addon-01","name":"Oat milk","price":25}])
            finally:
                db.close()
            status, removed = self.request("DELETE","/api/product-addons/public-addon-01",token=token)
            self.assertEqual(status,200)
            with server.LOGIN_LOCK:
                server.PUBLIC_ORDER_FAILURES.pop("127.0.0.1",None)
            status, duplicate = self.request("POST","/api/public/orders",payload)
            self.assertEqual(status,200)
            self.assertTrue(duplicate["duplicate"])
            # Keep the independent unavailable-stock assertion isolated from
            # the public endpoint's per-IP abuse throttle.
            with server.LOGIN_LOCK:
                server.PUBLIC_ORDER_FAILURES.pop("127.0.0.1",None)
            unavailable=dict(payload)
            unavailable["clientOrderId"]="public-addon-order-02"
            status, result = self.request("POST","/api/public/orders",unavailable)
            self.assertEqual(status,409)
        finally:
            server.DB_PATH=original_db_path
            with server.LOGIN_LOCK:
                server.PUBLIC_ORDER_FAILURES.pop("127.0.0.1",None)

    def test_cafe_product_addons_are_priced_snapshotted_and_idempotent(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "product-addons-test.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Addon Cafe", "username": "addon-owner",
                "password": "addon-owner-password-2026"
            })
            self.assertEqual(status, 200)
            token = setup["token"]
            license_code = make_license(["cafe","inventory","accounting"], "addon-license")
            status, activated = self.request("POST", "/api/licenses/activate", {"code":license_code}, token)
            self.assertEqual(status, 200)
            status, product = self.request("POST", "/api/products", {
                "id":"addon-product","name":"Coffee","sellPrice":100,"costPrice":30
            }, token)
            self.assertEqual(status, 201)
            db = server.connect()
            try:
                tenant_id = db.execute("SELECT tenant_id FROM users WHERE username='addon-owner'").fetchone()[0]
                db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,
                    quantity_delta,unit_cost,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("addon-stock",tenant_id,"addon-product",setup["user"]["id"],"purchase",5,30,"opening stock",int(time.time())))
                db.commit()
            finally:
                db.close()
            status, too_large = self.request("POST", "/api/product-addons", {
                "id":"addon-too-large","productId":"addon-product","name":"Unsafe price","price":9007199254740992
            }, token)
            self.assertEqual(status,400)
            status, created = self.request("POST", "/api/product-addons", {
                "id":"addon-01","productId":"addon-product","name":"Extra shot","price":20
            }, token)
            self.assertEqual(status, 201)
            status, unsafe_update = self.request("POST", "/api/product-addons/addon-01", {
                "name":"Extra shot","price":9007199254740992
            }, token)
            self.assertEqual(status,400)
            status, updated = self.request("POST", "/api/product-addons/addon-01", {
                "name":"Extra shot","price":25
            }, token)
            self.assertEqual(status, 200)
            status, addons = self.request("GET", "/api/product-addons", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(addons["items"][0]["price"], 25)

            order_payload = {
                "id":"addon-order-01",
                "items":[{"productId":"addon-product","quantity":2,"addons":["addon-01"]}],
                "discount":0,"paymentMethod":"cash"
            }
            status, order = self.request("POST", "/api/orders", order_payload, token)
            self.assertEqual(status, 201)
            self.assertEqual(order["subtotal"], 250)
            self.assertEqual(order["total"], 250)
            status, details = self.request("GET", "/api/orders/addon-order-01", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(details["order"]["total"],250)
            self.assertEqual(details["items"][0]["addons"],[{"id":"addon-01","name":"Extra shot","price":25}])
            status, duplicate = self.request("POST", "/api/orders", order_payload, token)
            self.assertEqual(status, 200)
            self.assertTrue(duplicate["duplicate"])
            status, conflict = self.request("POST", "/api/orders", {
                **order_payload,
                "items":[{"productId":"addon-product","quantity":2}]
            }, token)
            self.assertEqual(status, 409)

            db = server.connect()
            try:
                item = db.execute("SELECT unit_price,line_total,addons_json FROM order_items WHERE order_id='addon-order-01'").fetchone()
                self.assertEqual((item["unit_price"],item["line_total"]),(125,250))
                saved = json.loads(item["addons_json"])
                self.assertEqual(saved,[{"id":"addon-01","name":"Extra shot","price":25}])
                stock = db.execute("SELECT SUM(quantity_delta) FROM inventory_movements WHERE product_id='addon-product'").fetchone()[0]
                self.assertEqual(stock,3)
            finally:
                db.close()

            status, snapshot = self.request("GET", "/api/backup", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(snapshot["tables"]["product_addons"][0]["id"],"addon-01")
            self.assertEqual(snapshot["tables"]["order_items"][0]["addons_json"],item["addons_json"])
            status, removed = self.request("DELETE", "/api/product-addons/addon-01", token=token)
            self.assertEqual(status, 200)
            status, unavailable = self.request("POST", "/api/orders", {
                "id":"addon-order-02",
                "items":[{"productId":"addon-product","quantity":1,"addons":["addon-01"]}],
                "discount":0,"paymentMethod":"cash"
            }, token)
            self.assertEqual(status, 409)
            db = server.connect()
            try:
                saved = db.execute("SELECT addons_json FROM order_items WHERE order_id='addon-order-01'").fetchone()
                self.assertEqual(json.loads(saved["addons_json"])[0]["name"],"Extra shot")
            finally:
                db.close()
            status, restored = self.request("POST", "/api/backup/restore", snapshot, token)
            self.assertEqual(status, 200)
            status, restored_addons = self.request("GET", "/api/product-addons", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(restored_addons["items"][0]["id"],"addon-01")
            db = server.connect()
            try:
                restored_item = db.execute("SELECT addons_json FROM order_items WHERE order_id='addon-order-01'").fetchone()
                self.assertEqual(json.loads(restored_item["addons_json"])[0]["price"],25)
            finally:
                db.close()
        finally:
            server.DB_PATH = original_db_path

    def test_backup_scheduler_retries_transient_failures_with_backoff(self):
        delays = []

        class StopAfterThreeWaits:
            def is_set(self):
                return False

            def wait(self, seconds):
                delays.append(seconds)
                return len(delays) >= 3

        original_interval = server.BACKUP_INTERVAL_SECONDS
        try:
            server.BACKUP_INTERVAL_SECONDS = 86400
            with mock.patch("server.create_snapshot", side_effect=[
                OSError("temporary disk issue"),
                OSError("temporary disk issue"),
                Path("/tmp/mg-test-snapshot.sqlite3"),
            ]) as create:
                server.backup_scheduler(StopAfterThreeWaits())
                self.assertEqual(create.call_count, 3)
            self.assertEqual(delays, [60, 120, 86400])
        finally:
            server.BACKUP_INTERVAL_SECONDS = original_interval

    def test_backup_export_rejects_oversized_workspace_before_fetching_rows(self):
        original_limit = server.BACKUP_MAX_ROWS
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "backup-export-limit.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Backup Limit Test", "username": "backup-limit-owner",
                "password": "backup-limit-password-2026"
            })
            self.assertEqual(status, 200)
            server.BACKUP_MAX_ROWS = 1
            status, result = self.request("GET", "/api/backup", token=setup["token"])
            self.assertEqual(status, 413)
            self.assertIn("پشتیبان", result["error"])
        finally:
            server.BACKUP_MAX_ROWS = original_limit
            server.DB_PATH = original_db_path

    def test_client_ip_trusts_real_ip_only_from_configured_proxy(self):
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"X-Real-IP": "198.51.100.23"}
        old = os.environ.get("MG_TRUSTED_PROXIES")
        try:
            os.environ.pop("MG_TRUSTED_PROXIES", None)
            self.assertEqual(handler.client_ip(), "127.0.0.1")
            os.environ["MG_TRUSTED_PROXIES"] = "127.0.0.1/32"
            self.assertEqual(handler.client_ip(), "198.51.100.23")
            handler.headers = {"X-Real-IP": "not-an-ip"}
            self.assertEqual(handler.client_ip(), "127.0.0.1")
            os.environ["MG_TRUSTED_PROXIES"] = "192.0.2.0/24"
            self.assertEqual(handler.client_ip(), "127.0.0.1")
        finally:
            if old is None:
                os.environ.pop("MG_TRUSTED_PROXIES", None)
            else:
                os.environ["MG_TRUSTED_PROXIES"] = old

    def test_readiness_endpoint_checks_database_schema_and_integrity(self):
        with urllib.request.urlopen(self.base + "/api/ready", timeout=5) as response:
            self.assertEqual(response.status,200)
            payload=json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["service"],"mg-business")
        self.assertEqual(payload["checks"],{
            "databaseIntegrity":True,"foreignKeys":True,"requiredSchema":True,"migrations":True
        })
        self.assertEqual(payload["missingTables"],[])
        self.assertEqual(payload["schemaVersion"],server.SCHEMA_VERSION)
        self.assertEqual(payload["expectedSchemaVersion"],server.SCHEMA_VERSION)
        # A newer database must be rejected by connect() before any migration
        # can mutate it; readiness is intentionally unreachable in this state.
        future_version=server.SCHEMA_VERSION+1
        raw=sqlite3.connect(server.DB_PATH)
        try:
            raw.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                        (future_version,int(time.time())))
            raw.commit()
        finally:
            raw.close()
        with self.assertRaisesRegex(RuntimeError,"newer than this application supports"):
            server.connect()
        raw=sqlite3.connect(server.DB_PATH)
        try:
            self.assertEqual(raw.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                             future_version)
            raw.execute("DELETE FROM schema_migrations WHERE version=?",(future_version,))
            raw.commit()
        finally:
            raw.close()
        status,restored=self.request("GET","/api/ready")
        self.assertEqual(status,200)
        self.assertTrue(restored["ready"])

    def test_production_responses_include_hsts_and_cross_origin_guards(self):
        old_env = {key: os.environ.get(key) for key in ("MG_ENV", "MG_PUBLIC_URL")}
        try:
            os.environ["MG_ENV"] = "production"
            os.environ["MG_PUBLIC_URL"] = "https://business.example.com"
            with urllib.request.urlopen(self.base + "/api/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Strict-Transport-Security"), "max-age=31536000")
                self.assertEqual(response.headers.get("Cross-Origin-Opener-Policy"), "same-origin")
                self.assertEqual(response.headers.get("Cross-Origin-Resource-Policy"), "same-origin")
                self.assertEqual(response.headers.get("X-Permitted-Cross-Domain-Policies"), "none")
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_public_static_server_hides_source_and_development_files(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as response:
            self.assertEqual(response.status, 200)
        for path in ("/server.py", "/license_tool.py", "/backup_manager.py", "/README.md", "/tests/test_server.py"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + path, timeout=5)
            self.assertEqual(caught.exception.code, 404)

    def test_successful_login_does_not_clear_ip_wide_failures(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "login-throttle-test.sqlite3"
        ip = "127.0.0.1"
        with server.LOGIN_LOCK:
            original_user = server.LOGIN_FAILURES.copy()
            original_ip = server.LOGIN_IP_FAILURES.copy()
            server.LOGIN_FAILURES.clear()
            server.LOGIN_IP_FAILURES.clear()
            server.LOGIN_FAILURES[(ip, "other-account")] = [int(server.time.time())]
            server.LOGIN_IP_FAILURES[ip] = [int(server.time.time())]
        try:
            status, _ = self.request("POST", "/api/setup", {
                "businessName": "Throttle Test Shop", "username": "throttle-owner",
                "password": "correct-horse-battery"
            })
            self.assertEqual(status, 200)
            status, _ = self.request("POST", "/api/login", {
                "username": "throttle-owner", "password": "correct-horse-battery"
            })
            self.assertEqual(status, 200)
            with server.LOGIN_LOCK:
                self.assertIn(ip, server.LOGIN_IP_FAILURES)
                self.assertEqual(server.LOGIN_FAILURES.get((ip, "other-account")), [server.LOGIN_IP_FAILURES[ip][0]])
                self.assertNotIn((ip, "throttle-owner"), server.LOGIN_FAILURES)
        finally:
            with server.LOGIN_LOCK:
                server.LOGIN_FAILURES.clear()
                server.LOGIN_FAILURES.update(original_user)
                server.LOGIN_IP_FAILURES.clear()
                server.LOGIN_IP_FAILURES.update(original_ip)
            server.DB_PATH = original_db_path

    def test_session_idle_expiry_and_absolute_lifetime(self):
        # Use an isolated database so this security test cannot contaminate
        # the shared integration fixtures or depend on test execution order.
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "session-test.sqlite3"
        try:
            status, result = self.request("POST", "/api/setup", {
                "businessName": "Session Test Shop", "username": "session-owner",
                "password": "correct-horse-battery"
            })
            self.assertEqual(status, 200)
            token = result["token"]
            token_hash = server.hashlib.sha256(token.encode()).hexdigest()
            db = server.connect()
            try:
                now = int(server.time.time())
                db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (now + 5, token_hash))
                db.commit()
            finally:
                db.close()
            status, _ = self.request("GET", "/api/me", token=token)
            self.assertEqual(status, 200)
            db = server.connect()
            try:
                refreshed = db.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
                self.assertGreaterEqual(refreshed["expires_at"], int(server.time.time()) + 29 * 60)
                db.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (int(server.time.time()) - 1, token_hash))
                db.commit()
            finally:
                db.close()
            status, _ = self.request("GET", "/api/me", token=token)
            self.assertEqual(status, 401)

            status, result = self.request("POST", "/api/login", {
                "username": "session-owner", "password": "correct-horse-battery"
            })
            self.assertEqual(status, 200)
            token = result["token"]
            token_hash = server.hashlib.sha256(token.encode()).hexdigest()
            db = server.connect()
            try:
                db.execute("UPDATE sessions SET created_at=? WHERE token_hash=?",
                           (int(server.time.time()) - server.SESSION_SECONDS - 1, token_hash))
                db.commit()
            finally:
                db.close()
            status, _ = self.request("GET", "/api/me", token=token)
            self.assertEqual(status, 401)
        finally:
            server.DB_PATH = original_db_path

    def test_public_order_throttle_state_is_bounded_and_enforced(self):
        with server.LOGIN_LOCK:
            original = server.PUBLIC_ORDER_FAILURES.copy()
            server.PUBLIC_ORDER_FAILURES.clear()
            try:
                for index in range(2100):
                    ip = "203.0."+str(index // 256)+"."+str(index % 256)
                    self.assertTrue(server.remember_public_order_attempt(ip, 1000))
                self.assertLessEqual(len(server.PUBLIC_ORDER_FAILURES), 2048)
                for attempt in range(5):
                    self.assertTrue(server.remember_public_order_attempt("198.51.100.7", 2000 + attempt))
                self.assertFalse(server.remember_public_order_attempt("198.51.100.7", 2005))
                self.assertTrue(server.remember_public_order_attempt("198.51.100.7", 3000))
            finally:
                server.PUBLIC_ORDER_FAILURES.clear()
                server.PUBLIC_ORDER_FAILURES.update(original)

    def test_login_throttle_state_is_bounded(self):
        with server.LOGIN_LOCK:
            original_user = server.LOGIN_FAILURES.copy()
            original_ip = server.LOGIN_IP_FAILURES.copy()
            server.LOGIN_FAILURES.clear()
            server.LOGIN_IP_FAILURES.clear()
            try:
                for index in range(4200):
                    ip = "198.51."+str(index // 256)+"."+str(index % 256)
                    server.remember_login_failure((ip,"user-"+str(index)), ip, index)
                self.assertLessEqual(len(server.LOGIN_FAILURES), 4096)
                self.assertLessEqual(len(server.LOGIN_IP_FAILURES), 2048)
            finally:
                server.LOGIN_FAILURES.clear()
                server.LOGIN_FAILURES.update(original_user)
                server.LOGIN_IP_FAILURES.clear()
                server.LOGIN_IP_FAILURES.update(original_ip)

    def test_login_rejects_oversized_username_before_rate_limit_state(self):
        ip = "127.0.0.1"
        with server.LOGIN_LOCK:
            original_user = server.LOGIN_FAILURES.copy()
            original_ip = server.LOGIN_IP_FAILURES.copy()
            server.LOGIN_FAILURES.clear()
            server.LOGIN_IP_FAILURES.clear()
        try:
            for username in ("x" * 65, "x" * 10000, ["not", "a", "string"]):
                status, result = self.request("POST", "/api/login", {
                    "username": username, "password": "incorrect-password"
                })
                self.assertEqual(status, 400)
                self.assertIn("error", result)
            with server.LOGIN_LOCK:
                self.assertNotIn(ip, server.LOGIN_IP_FAILURES)
                self.assertEqual(server.LOGIN_FAILURES, {})
        finally:
            with server.LOGIN_LOCK:
                server.LOGIN_FAILURES.clear()
                server.LOGIN_FAILURES.update(original_user)
                server.LOGIN_IP_FAILURES.clear()
                server.LOGIN_IP_FAILURES.update(original_ip)

    def test_login_rejects_oversized_or_non_string_password_before_hashing(self):
        ip = "127.0.0.1"
        with server.LOGIN_LOCK:
            original_user = server.LOGIN_FAILURES.copy()
            original_ip = server.LOGIN_IP_FAILURES.copy()
            server.LOGIN_FAILURES.clear()
            server.LOGIN_IP_FAILURES.clear()
        try:
            with mock.patch.object(server, "verify_password", side_effect=AssertionError("password hashing must not run")):
                for password in ("p" * 257, ["not", "a", "string"]):
                    status, result = self.request("POST", "/api/login", {
                        "username": "known-or-unknown-user", "password": password
                    })
                    self.assertEqual(status, 400)
                    self.assertIn("error", result)
            with server.LOGIN_LOCK:
                self.assertNotIn(ip, server.LOGIN_IP_FAILURES)
                self.assertEqual(server.LOGIN_FAILURES, {})
        finally:
            with server.LOGIN_LOCK:
                server.LOGIN_FAILURES.clear()
                server.LOGIN_FAILURES.update(original_user)
                server.LOGIN_IP_FAILURES.clear()
                server.LOGIN_IP_FAILURES.update(original_ip)

    def test_login_rate_limit_covers_username_rotation_per_ip(self):
        ip = "127.0.0.1"
        with server.LOGIN_LOCK:
            server.LOGIN_IP_FAILURES.pop(ip, None)
        try:
            for index in range(30):
                status, _ = self.request("POST", "/api/login", {
                    "username": "unknown-user-" + str(index),
                    "password": "incorrect-password"
                })
                self.assertEqual(status, 401)
            status, result = self.request("POST", "/api/login", {
                "username": "another-unknown-user", "password": "incorrect-password"
            })
            self.assertEqual(status, 429)
        finally:
            with server.LOGIN_LOCK:
                server.LOGIN_IP_FAILURES.pop(ip, None)
                for key in list(server.LOGIN_FAILURES):
                    if key[0] == ip:
                        server.LOGIN_FAILURES.pop(key, None)

    def test_manager_cannot_reset_owner_password(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "owner-password-guard.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Owner Guard", "username": "owner-guard",
                "password": "owner-secure-password-2026"
            })
            self.assertEqual(status, 200)
            owner_token = setup["token"]
            status, created = self.request("POST", "/api/users", {
                "username": "delegated-manager",
                "password": "manager-secure-password-2026",
                "role": "manager"
            }, owner_token)
            self.assertEqual(status, 201)
            status, manager_login = self.request("POST", "/api/login", {
                "username": "delegated-manager",
                "password": "manager-secure-password-2026"
            })
            self.assertEqual(status, 200)
            status, denied = self.request("POST", "/api/users/"+setup["user"]["id"]+"/password", {
                "password": "attacker-controlled-password"
            }, manager_login["token"])
            self.assertEqual(status, 403)
            status, owner_login = self.request("POST", "/api/login", {
                "username": "owner-guard",
                "password": "owner-secure-password-2026"
            })
            self.assertEqual(status, 200)
        finally:
            server.DB_PATH = original_db_path

    def test_transaction_void_rejects_missing_journal_without_changing_status(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "transaction-missing-journal.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Missing Journal Test", "username": "missing-journal-owner",
                "password": "missing-journal-secure-password-2026"
            })
            self.assertEqual(status, 200)
            token = setup["token"]
            status, _ = self.request("POST", "/api/transactions", {
                "id": "missing-journal-tx", "title": "Test income", "amount": 5000,
                "kind": "income", "category": "Test", "createdAt": "2026-11-17T09:00:00Z"
            }, token)
            self.assertEqual(status, 201)
            db = server.connect()
            try:
                db.execute("DROP TRIGGER IF EXISTS journal_lines_no_delete")
                db.execute("DROP TRIGGER IF EXISTS journal_lines_immutable_delete")
                db.execute("""DELETE FROM journal_lines WHERE entry_id IN (
                    SELECT id FROM journal_entries WHERE tenant_id=? AND source_type='transaction' AND source_id=?
                )""", (setup["user"]["tenantId"], "missing-journal-tx"))
                db.commit()
            finally:
                db.close()
            status, result = self.request("DELETE", "/api/transactions/missing-journal-tx", token=token)
            self.assertEqual(status, 409)
            db = server.connect()
            try:
                row = db.execute("SELECT status FROM transactions WHERE id=? AND tenant_id=?",
                    ("missing-journal-tx", setup["user"]["tenantId"])).fetchone()
                self.assertEqual(row["status"], "posted")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM journal_entries WHERE source_type='transaction_reversal' AND source_id=?",
                    ("missing-journal-tx",)).fetchone()[0], 0)
            finally:
                db.close()
        finally:
            server.DB_PATH = original_db_path

    def test_user_can_change_own_password_and_revoke_old_sessions(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "password-change-test.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Password Test Shop", "username": "password-owner",
                "password": "correct-horse-battery"
            })
            self.assertEqual(status, 200)
            old_token = setup["token"]
            status, denied = self.request("POST", "/api/account/password", {
                "currentPassword": "wrong-current-password",
                "newPassword": "new-secure-password-2026"
            }, old_token)
            self.assertEqual(status, 403)
            status, changed = self.request("POST", "/api/account/password", {
                "currentPassword": "correct-horse-battery",
                "newPassword": "new-secure-password-2026"
            }, old_token)
            self.assertEqual(status, 200)
            self.assertTrue(changed.get("token"))
            status, _ = self.request("GET", "/api/me", token=old_token)
            self.assertEqual(status, 401)
            status, me = self.request("GET", "/api/me", token=changed["token"])
            self.assertEqual(status, 200)
            self.assertEqual(me["user"]["username"], "password-owner")
            status, login = self.request("POST", "/api/login", {
                "username": "password-owner", "password": "new-secure-password-2026"
            })
            self.assertEqual(status, 200)
        finally:
            server.DB_PATH = original_db_path

    def test_password_hash_is_salted_and_verified(self):
        encoded = server.password_hash("a-long-test-password")
        self.assertTrue(server.verify_password("a-long-test-password", encoded))
        self.assertFalse(server.verify_password("wrong-password", encoded))
        self.assertNotEqual(encoded, server.password_hash("a-long-test-password"))

    def test_password_verification_rejects_unsafe_hash_parameters_before_pbkdf2(self):
        malformed = [
            "pbkdf2_sha256$1$" + ("11" * 16) + "$" + ("00" * 32),
            "pbkdf2_sha256$999999999999999999999$" + ("11" * 16) + "$" + ("00" * 32),
            "pbkdf2_sha256$310000$abcd$" + ("00" * 32),
            "pbkdf2_sha256$310000$" + ("11" * 16) + "$not-hex",
        ]
        with mock.patch.object(server.hashlib, "pbkdf2_hmac", side_effect=AssertionError("PBKDF2 must not run")):
            for encoded in malformed:
                self.assertFalse(server.verify_password("valid-password", encoded))
            valid_shape = "pbkdf2_sha256$310000$" + ("11" * 16) + "$" + ("00" * 32)
            self.assertFalse(server.verify_password("x" * 257, valid_shape))


    def test_inventory_movement_rejects_stock_total_above_safe_integer(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "inventory-stock-overflow.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Stock Overflow Test", "username": "stock-overflow-owner",
                "password": "stock-overflow-secure-password-2026"
            })
            self.assertEqual(status, 200)
            token = setup["token"]
            status, product = self.request("POST", "/api/products", {
                "id": "stock-overflow-product", "name": "Overflow stock",
                "sellPrice": 100, "costPrice": 10
            }, token)
            self.assertEqual(status, 201)
            db = server.connect()
            try:
                db.execute("""INSERT INTO inventory_movements(id,tenant_id,product_id,created_by,movement_type,
                    quantity_delta,unit_cost,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("seed-overflow-stock", setup["user"]["tenantId"], "stock-overflow-product",
                     setup["user"]["id"], "adjustment", 9007199254740991, 0, "test seed", int(time.time())))
                db.commit()
            finally:
                db.close()
            status, result = self.request("POST", "/api/inventory/movements", {
                "id": "stock-overflow-increment", "productId": "stock-overflow-product",
                "movementType": "purchase", "quantityDelta": 1, "unitCost": 0
            }, token)
            self.assertEqual(status, 409)
            db = server.connect()
            try:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM inventory_movements WHERE id=?",
                    ("stock-overflow-increment",)).fetchone()[0], 0)
            finally:
                db.close()
        finally:
            server.DB_PATH = original_db_path

    def test_transaction_reversal_is_balanced_and_idempotent(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "transaction-reversal.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Reversal Test", "username": "reversal-owner",
                "password": "reversal-secure-password-2026"
            })
            self.assertEqual(status, 200)
            token = setup["token"]
            status, created = self.request("POST", "/api/transactions", {
                "id": "reversal-tx-1", "title": "Test income", "amount": 125000,
                "kind": "income", "category": "Test", "createdAt": "2026-11-17T09:00:00Z"
            }, token)
            self.assertEqual(status, 201)
            status, reversed_result = self.request("DELETE", "/api/transactions/reversal-tx-1", token=token)
            self.assertEqual(status, 200)
            self.assertTrue(reversed_result["voided"])
            status, repeated = self.request("DELETE", "/api/transactions/reversal-tx-1", token=token)
            self.assertEqual(status, 404)
            db = server.connect()
            try:
                net = db.execute("""SELECT COALESCE(SUM(l.debit-l.credit),0) FROM journal_lines l
                    JOIN journal_entries e ON e.id=l.entry_id
                    WHERE e.tenant_id=? AND e.source_type IN ('transaction','transaction_reversal')
                      AND e.source_id='reversal-tx-1'""", (setup["user"]["tenantId"],)).fetchone()[0]
                self.assertEqual(net, 0)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM journal_entries WHERE tenant_id=? AND source_type='transaction_reversal' AND source_id='reversal-tx-1'",
                    (setup["user"]["tenantId"],)
                ).fetchone()[0], 1)
            finally:
                db.close()
            status, visible = self.request("GET", "/api/transactions", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(visible["items"], [])
        finally:
            server.DB_PATH = original_db_path


    def test_viewer_cannot_mutate_cafe_or_gamenet_operations(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "viewer-write-authorization.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Viewer Authorization",
                "username": "viewer-owner",
                "password": "viewer-owner-password-2026"
            })
            self.assertEqual(status, 200)
            owner_token = setup["token"]
            status, created = self.request("POST", "/api/users", {
                "username": "read-only-viewer",
                "password": "read-only-viewer-password-2026",
                "role": "viewer"
            }, owner_token)
            self.assertEqual(status, 201)
            status, login = self.request("POST", "/api/login", {
                "username": "read-only-viewer",
                "password": "read-only-viewer-password-2026"
            })
            self.assertEqual(status, 200)
            viewer_token = login["token"]

            # Read-only access must not expose owner/manager administrative datasets.
            for path in ("/api/backup", "/api/audit-log", "/api/users"):
                with self.subTest(read_path=path):
                    status, result = self.request("GET", path, token=viewer_token)
                    self.assertEqual(status, 403)
                    self.assertIn("error", result)

            attempts = [
                ("POST", "/api/orders", {"items": []}),
                ("POST", "/api/gamenet/devices", {
                    "name": "Unauthorized Device", "deviceType": "pc",
                    "hourlyRate": 10000, "isFree": False
                }),
                ("POST", "/api/gamenet/sessions", {
                    "deviceId": "missing-device", "playersCount": 1
                }),
                ("POST", "/api/gamenet/reservations", {
                    "deviceId": "missing-device", "startsAt": "2026-11-18T10:00:00+03:30",
                    "endsAt": "2026-11-18T11:00:00+03:30"
                }),
                ("POST", "/api/gamenet/sessions/missing-session/adjust", {"playersCount": 2}),
                ("POST", "/api/gamenet/sessions/missing-session/end", {"paymentMethod": "cash"}),
                ("POST", "/api/gamenet/reservations/missing-reservation/cancel", {}),
                ("POST", "/api/licenses/activate", {"code": "invalid-license"}),
                ("POST", "/api/product-addons", {"productId": "missing-product", "name": "Unauthorized", "price": 1}),
                ("POST", "/api/product-addons/missing-addon", {"name": "Unauthorized", "price": 1}),
                ("POST", "/api/products", {"id": "unauthorized-product", "name": "Unauthorized", "sellPrice": 1}),
                ("POST", "/api/suppliers", {"name": "Unauthorized supplier"}),
                ("POST", "/api/purchase-invoices", {"id": "unauthorized-invoice", "supplierId": "missing", "items": []}),
                ("POST", "/api/purchase-invoices/missing-invoice/payments", {"amount": 1, "paymentMethod": "cash"}),
                ("POST", "/api/inventory/movements", {"productId": "missing-product", "quantityDelta": 1}),
                ("POST", "/api/customers", {"name": "Unauthorized customer"}),
                ("POST", "/api/customers/missing-viewer-customer/payments", {
                    "amount": 10000, "paymentMethod": "cash"
                }),
                ("POST", "/api/transactions", {
                    "id": "unauthorized-transaction", "title": "Unauthorized",
                    "amount": 1, "kind": "income", "category": "test"
                })
            ]
            for method, path, payload in attempts:
                with self.subTest(path=path):
                    status, result = self.request(method, path, payload, viewer_token)
                    self.assertEqual(status, 403)
                    self.assertIn("error", result)

            db = server.connect()
            try:
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM orders WHERE tenant_id=?",
                    (setup["user"]["tenantId"],)
                ).fetchone()[0], 0)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM game_devices WHERE tenant_id=? AND name='Unauthorized Device'",
                    (setup["user"]["tenantId"],)
                ).fetchone()[0], 0)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM customer_ledger WHERE tenant_id=? AND source_type='payment'",
                    (setup["user"]["tenantId"],)
                ).fetchone()[0], 0)
            finally:
                db.close()
        finally:
            server.DB_PATH = original_db_path


    def test_cashier_manager_owner_permission_matrix_for_inventory_and_users(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "role-matrix-inventory.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Role Matrix",
                "username": "matrix-owner",
                "password": "matrix-owner-password-2026"
            })
            self.assertEqual(status, 200)
            owner_token = setup["token"]

            created_tokens = {}
            for role in ("manager", "cashier"):
                status, created = self.request("POST", "/api/users", {
                    "username": "matrix-" + role,
                    "password": "matrix-" + role + "-password-2026",
                    "role": role
                }, owner_token)
                self.assertEqual(status, 201)
                status, login = self.request("POST", "/api/login", {
                    "username": "matrix-" + role,
                    "password": "matrix-" + role + "-password-2026"
                })
                self.assertEqual(status, 200)
                created_tokens[role] = login["token"]

            # Cashiers may use operational screens but cannot change master data,
            # stock valuation, supplier liabilities, or user administration.
            denied = [
                ("POST", "/api/products", {"name": "Cashier product", "sellPrice": 100}),
                ("POST", "/api/product-addons", {"productId": "missing", "name": "Cashier addon", "price": 10}),
                ("POST", "/api/suppliers", {"name": "Cashier supplier"}),
                ("POST", "/api/purchase-invoices", {"supplierId": "missing", "invoiceNumber": "C-1",
                    "invoiceDate": "2026-11-17", "items": [{"productId": "missing", "quantity": 1, "unitCost": 100}]}),
                ("POST", "/api/inventory/movements", {"productId": "missing", "quantityDelta": 1,
                    "movementType": "purchase", "unitCost": 100}),
                ("POST", "/api/users", {"username": "cashier-created-user",
                    "password": "cashier-created-password-2026", "role": "viewer"})
            ]
            for method, path, payload in denied:
                with self.subTest(role="cashier", path=path):
                    status, result = self.request(method, path, payload, created_tokens["cashier"])
                    self.assertEqual(status, 403)
                    self.assertIn("error", result)

            # Managers can administer inventory and suppliers, but user creation
            # remains an owner-only operation.
            status, product = self.request("POST", "/api/products", {
                "id": "manager-created-product", "name": "Manager product",
                "sellPrice": 2500, "costPrice": 1000
            }, created_tokens["manager"])
            self.assertEqual(status, 201)
            status, supplier = self.request("POST", "/api/suppliers", {
                "id": "manager-created-supplier", "name": "Manager supplier"
            }, created_tokens["manager"])
            self.assertEqual(status, 201)
            status, movement = self.request("POST", "/api/inventory/movements", {
                "id": "manager-stock-receipt", "productId": product["id"],
                "quantityDelta": 5, "movementType": "purchase", "unitCost": 1000
            }, created_tokens["manager"])
            self.assertEqual(status, 201)

            status, denied_user = self.request("POST", "/api/users", {
                "username": "manager-created-user",
                "password": "manager-created-password-2026", "role": "viewer"
            }, created_tokens["manager"])
            self.assertEqual(status, 403)
            self.assertIn("error", denied_user)

            status, transaction = self.request("POST", "/api/transactions", {
                "id": "manager-reversal-permission", "title": "Manager approved expense",
                "amount": 1200, "kind": "expense", "category": "role-test",
                "createdAt": "2026-11-17T10:00:00"
            }, created_tokens["manager"])
            self.assertEqual(status, 201)
            status, denied_reversal = self.request("DELETE",
                "/api/transactions/" + transaction["id"], token=created_tokens["cashier"])
            self.assertEqual(status, 403)
            self.assertIn("error", denied_reversal)
            status, reversed = self.request("DELETE",
                "/api/transactions/" + transaction["id"], token=created_tokens["manager"])
            self.assertEqual(status, 200)
            self.assertTrue(reversed["voided"])

            status, stock = self.request("GET", "/api/inventory", token=created_tokens["cashier"])
            self.assertEqual(status, 200)
            self.assertEqual(next(item for item in stock["items"] if item["productId"] == product["id"])["quantity"], 5)

            db = server.connect()
            try:
                tenant_id = setup["user"]["tenantId"]
                self.assertEqual(db.execute("SELECT COUNT(*) FROM products WHERE tenant_id=? AND name='Cashier product'",
                    (tenant_id,)).fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM suppliers WHERE tenant_id=? AND name='Cashier supplier'",
                    (tenant_id,)).fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE tenant_id=? AND username='manager-created-user'",
                    (tenant_id,)).fetchone()[0], 0)
            finally:
                db.close()
        finally:
            server.DB_PATH = original_db_path

    def test_account_ledger_paginates_newest_first_with_global_totals_and_running_balances(self):
        original_db_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"account-ledger-pages.sqlite3"
        try:
            status,setup=self.request("POST","/api/setup",{
                "businessName":"Ledger Pages","username":"ledger-pages-owner",
                "password":"ledger-pages-owner-password-2026"})
            self.assertEqual(status,200)
            token=setup["token"]
            status,tx=self.request("POST","/api/transactions",{
                "id":"ledger-page-base","title":"Base income","amount":1000,
                "kind":"income","category":"test","createdAt":"2026-11-17T10:00:00Z"},token)
            self.assertEqual(status,201)
            db=server.connect()
            try:
                tenant_id=setup["user"]["tenantId"]
                accounts={row["code"]:row["id"] for row in db.execute(
                    "SELECT id,code FROM accounts WHERE tenant_id=?",(tenant_id,)).fetchall()}
                for entry_id,amount,created_at in (
                    ("ledger-page-middle",200,9999999998),
                    ("ledger-page-latest",300,9999999999),
                ):
                    db.execute("""INSERT INTO journal_entries
                        (id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
                        VALUES(?,?,? ,?,'test',?,?,?)""",
                        (entry_id,tenant_id,"2026-11-17",entry_id,entry_id,setup["user"]["id"],created_at))
                    db.executemany("""INSERT INTO journal_lines
                        (id,tenant_id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?,?)""",[
                        (entry_id+"-cash",tenant_id,entry_id,accounts["1000"],amount,0),
                        (entry_id+"-revenue",tenant_id,entry_id,accounts["4000"],0,amount),
                    ])
                db.commit()
            finally:
                db.close()

            base="/api/accounting/account-ledger?accountId="+accounts["1000"]+"&fromDate=2026-11-17&toDate=2026-11-17"
            status,first=self.request("GET",base+"&limit=1&offset=0",token=token)
            self.assertEqual(status,200)
            self.assertEqual(first["totalItems"],3)
            self.assertEqual(first["periodDebit"],1500)
            self.assertEqual(first["periodCredit"],0)
            self.assertEqual(first["closingBalance"],1500)
            self.assertEqual(first["items"][0]["entryId"],"ledger-page-latest")
            self.assertEqual(first["items"][0]["runningBalance"],1500)
            self.assertTrue(first["hasMore"])
            self.assertEqual(first["nextOffset"],1)

            status,second=self.request("GET",base+"&limit=1&offset=1",token=token)
            self.assertEqual(status,200)
            self.assertEqual(second["items"][0]["entryId"],"ledger-page-middle")
            self.assertEqual(second["items"][0]["runningBalance"],1200)
            self.assertEqual(second["periodDebit"],first["periodDebit"])
            self.assertTrue(second["hasMore"])

            status,last=self.request("GET",base+"&limit=1&offset=2",token=token)
            self.assertEqual(status,200)
            self.assertEqual(last["items"][0]["sourceId"],tx["id"])
            self.assertEqual(last["items"][0]["runningBalance"],1000)
            self.assertFalse(last["hasMore"])
            self.assertEqual(last["nextOffset"],3)

            for query in ("&limit=0","&limit=501","&offset=-1","&offset=1000001","&limit=abc"):
                status,_=self.request("GET",base+query,token=token)
                self.assertEqual(status,400)
        finally:
            server.DB_PATH=original_db_path

    def test_cash_register_shift_reconciles_cash_and_survives_versioned_backup(self):
        original_db_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"cash-register-shifts.sqlite3"
        try:
            status,setup=self.request("POST","/api/setup",{
                "businessName":"Cash Register","username":"cash-owner",
                "password":"cash-owner-password-2026"})
            self.assertEqual(status,200)
            owner=setup["token"]

            status,invalid=self.request("POST","/api/cash-register/shifts/open",{
                "openingCash":-1},owner)
            self.assertEqual(status,400)
            status,opened=self.request("POST","/api/cash-register/shifts/open",{
                "openingCash":5000,"note":"morning handover"},owner)
            self.assertEqual(status,201)
            self.assertEqual(opened["expectedCash"],5000)
            shift_id=opened["id"]

            status,duplicate=self.request("POST","/api/cash-register/shifts/open",{
                "openingCash":1000},owner)
            self.assertEqual(status,409)

            now=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
            status,income=self.request("POST","/api/transactions",{
                "id":"cash-shift-income","title":"Cash shift sale","amount":1000,
                "kind":"income","category":"cash test","createdAt":now},owner)
            self.assertEqual(status,201)
            status,expense=self.request("POST","/api/transactions",{
                "id":"cash-shift-expense","title":"Cash shift expense","amount":250,
                "kind":"expense","category":"cash test","createdAt":now},owner)
            self.assertEqual(status,201)

            status,active=self.request("GET","/api/cash-register/shifts",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(active["active"]["id"],shift_id)
            self.assertEqual(active["active"]["expectedCash"],5750)

            status,cashier_created=self.request("POST","/api/users",{
                "username":"cash-shift-cashier","password":"cash-shift-cashier-password","role":"cashier"},owner)
            self.assertEqual(status,201)
            status,cashier_login=self.request("POST","/api/login",{
                "username":"cash-shift-cashier","password":"cash-shift-cashier-password"})
            self.assertEqual(status,200)
            cashier=cashier_login["token"]

            status,forbidden=self.request("POST",f"/api/cash-register/shifts/{shift_id}/close",{
                "countedCash":5750},cashier)
            self.assertEqual(status,403)
            status,invalid_close=self.request("POST",f"/api/cash-register/shifts/{shift_id}/close",{
                "countedCash":-1},owner)
            self.assertEqual(status,400)

            status,closed=self.request("POST",f"/api/cash-register/shifts/{shift_id}/close",{
                "countedCash":5700,"note":"shortage counted"},owner)
            self.assertEqual(status,200)
            self.assertEqual(closed["expectedCash"],5750)
            self.assertEqual(closed["countedCash"],5700)
            self.assertEqual(closed["difference"],-50)
            status,duplicate_close=self.request("POST",f"/api/cash-register/shifts/{shift_id}/close",{
                "countedCash":5750},owner)
            self.assertEqual(status,409)

            status,cashier_shift=self.request("POST","/api/cash-register/shifts/open",{
                "openingCash":1000,"note":"cashier shift"},cashier)
            self.assertEqual(status,201)
            status,second_open=self.request("POST","/api/cash-register/shifts/open",{
                "openingCash":0},owner)
            self.assertEqual(status,409)
            status,cashier_close=self.request("POST",f"/api/cash-register/shifts/{cashier_shift['id']}/close",{
                "countedCash":1000},cashier)
            self.assertEqual(status,200)
            self.assertEqual(cashier_close["difference"],0)

            time.sleep(1)
            status,active_shift=self.request("POST","/api/cash-register/shifts/open",{
                "openingCash":2000,"note":"shift retained through restore"},owner)
            self.assertEqual(status,201)
            status,active_sale=self.request("POST","/api/transactions",{
                "id":"cash-shift-active-backup-sale","title":"Cash after shift open","amount":300,
                "kind":"income","category":"cash test","createdAt":now},owner)
            self.assertEqual(status,201)
            status,live_active=self.request("GET","/api/cash-register/shifts",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(live_active["active"]["expectedCash"],2300)

            status,snapshot=self.request("GET","/api/backup",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(snapshot["version"],4)
            self.assertEqual(len(snapshot["tables"]["cash_register_shifts"]),3)
            self.assertTrue(all("__rowid" in row for row in snapshot["tables"]["journal_entries"]))
            db=server.connect()
            try:
                db.execute("DELETE FROM cash_register_shifts WHERE tenant_id=?",(setup["user"]["tenantId"],))
                db.commit()
            finally:
                db.close()
            status,restored=self.request("POST","/api/backup/restore",snapshot,owner)
            self.assertEqual(status,200)
            status,history=self.request("GET","/api/cash-register/shifts",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(len(history["items"]),3)
            self.assertEqual(history["active"]["id"],active_shift["id"])
            self.assertEqual(history["active"]["expectedCash"],2300)
            first=next(item for item in history["items"] if item["id"]==shift_id)
            self.assertEqual(first["difference"],-50)

            legacy_v3=json.loads(json.dumps(snapshot))
            legacy_v3["version"]=3
            for journal in legacy_v3["tables"]["journal_entries"]:
                journal.pop("__rowid",None)
            for shift in legacy_v3["tables"]["cash_register_shifts"]:
                shift.pop("journal_cursor",None)
            status,legacy_v3_restore=self.request("POST","/api/backup/restore",legacy_v3,owner)
            self.assertEqual(status,200)
            status,legacy_v3_active=self.request("GET","/api/cash-register/shifts",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(legacy_v3_active["active"]["expectedCash"],2300,
                "v3 restores must use the legacy time boundary when journal rowids are absent")

            legacy_v2=json.loads(json.dumps(snapshot))
            legacy_v2["version"]=2
            legacy_v2["tables"].pop("cash_register_shifts")
            for journal in legacy_v2["tables"]["journal_entries"]:
                journal.pop("__rowid",None)
            status,legacy_active_restore=self.request("POST","/api/backup/restore",legacy_v2,owner)
            self.assertEqual(status,409,"legacy v2 restore must not replace journal rows while a cash shift is active")
            status,closed_for_legacy=self.request("POST",f"/api/cash-register/shifts/{active_shift['id']}/close",{
                "countedCash":2300},owner)
            self.assertEqual(status,200)
            self.assertEqual(closed_for_legacy["difference"],0)
            status,legacy_restore=self.request("POST","/api/backup/restore",legacy_v2,owner)
            self.assertEqual(status,200)
            status,preserved=self.request("GET","/api/cash-register/shifts",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(len(preserved["items"]),3,
                "v2 restores must preserve current cash shifts absent from that backup format")
        finally:
            server.DB_PATH=original_db_path

    def test_cafe_kitchen_fulfillment_state_machine_is_audited_and_restored(self):
        original_db_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"cafe-kitchen-queue.sqlite3"
        try:
            status,setup=self.request("POST","/api/setup",{
                "businessName":"Kitchen Queue","username":"kitchen-owner",
                "password":"kitchen-owner-password-2026"})
            self.assertEqual(status,200)
            owner=setup["token"]
            status,product=self.request("POST","/api/products",{
                "id":"kitchen-product","name":"قهوه آزمایشی","sellPrice":100000,"costPrice":30000},owner)
            self.assertEqual(status,201)
            db=server.connect()
            try:
                db.execute("""INSERT INTO inventory_movements
                    (id,tenant_id,product_id,created_by,movement_type,quantity_delta,unit_cost,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    ("kitchen-stock",setup["user"]["tenantId"],product["id"],setup["user"]["id"],
                     "purchase",10,30000,"opening stock",int(time.time())))
                db.commit()
            finally:
                db.close()

            status,viewer_created=self.request("POST","/api/users",{
                "username":"kitchen-viewer","password":"kitchen-viewer-password","role":"viewer"},owner)
            self.assertEqual(status,201)
            status,viewer_login=self.request("POST","/api/login",{
                "username":"kitchen-viewer","password":"kitchen-viewer-password"})
            self.assertEqual(status,200)
            viewer=viewer_login["token"]

            status,order=self.request("POST","/api/orders",{
                "id":"kitchen-order-main","items":[{"productId":product["id"],"quantity":1}],
                "discount":0,"paymentMethod":"cash"},owner)
            self.assertEqual(status,201)
            status,orders=self.request("GET","/api/orders",token=owner)
            self.assertEqual(status,200)
            order_row=next(item for item in orders["items"] if item["id"]==order["id"])
            self.assertEqual(order_row["fulfillmentStatus"],"queued")

            path="/api/orders/"+order["id"]+"/fulfillment"
            status,forbidden=self.request("POST",path,{"status":"preparing"},viewer)
            self.assertEqual(status,403)
            status,invalid=self.request("POST",path,{"status":"cancelled"},owner)
            self.assertEqual(status,400)
            status,skipped=self.request("POST",path,{"status":"ready"},owner)
            self.assertEqual(status,409)

            status,preparing=self.request("POST",path,{"status":"preparing"},owner)
            self.assertEqual(status,200)
            self.assertEqual(preparing["status"],"preparing")
            status,duplicate=self.request("POST",path,{"status":"preparing"},owner)
            self.assertEqual(status,200)
            self.assertTrue(duplicate["duplicate"])
            status,ready=self.request("POST",path,{"status":"ready"},owner)
            self.assertEqual(status,200)
            status,served=self.request("POST",path,{"status":"served"},owner)
            self.assertEqual(status,200)

            status,cancelled_order=self.request("POST","/api/orders",{
                "id":"kitchen-order-cancelled","items":[{"productId":product["id"],"quantity":1}],
                "discount":0,"paymentMethod":"cash"},owner)
            self.assertEqual(status,201)
            status,cancelled=self.request("DELETE","/api/orders/"+cancelled_order["id"],token=owner)
            self.assertEqual(status,200)
            status,rejected=self.request("POST","/api/orders/"+cancelled_order["id"]+"/fulfillment",
                {"status":"preparing"},owner)
            self.assertEqual(status,409)

            status,audit_result=self.request("GET","/api/audit-log",token=owner)
            self.assertEqual(status,200)
            transitions=[item for item in audit_result["items"] if item["action"]=="cafe.order.fulfillment.updated"]
            self.assertEqual(len(transitions),3)

            status,snapshot=self.request("GET","/api/backup",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(snapshot["version"],4)
            self.assertTrue(all("fulfillment_status" in row for row in snapshot["tables"]["orders"]))
            db=server.connect()
            try:
                db.execute("UPDATE orders SET fulfillment_status='queued' WHERE id=?",(order["id"],))
                db.commit()
            finally:
                db.close()
            status,restored=self.request("POST","/api/backup/restore",snapshot,owner)
            self.assertEqual(status,200)
            status,orders_after_restore=self.request("GET","/api/orders",token=owner)
            self.assertEqual(status,200)
            restored_order=next(item for item in orders_after_restore["items"] if item["id"]==order["id"])
            self.assertEqual(restored_order["fulfillmentStatus"],"served")
        finally:
            server.DB_PATH=original_db_path

    def test_accounting_period_close_blocks_backdated_postings_and_reversals(self):
        original_db_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"accounting-period-close.sqlite3"
        try:
            status,setup=self.request("POST","/api/setup",{
                "businessName":"Period Close","username":"period-owner",
                "password":"period-owner-password-2026"})
            self.assertEqual(status,200)
            owner=setup["token"]
            yesterday=(date.today()-timedelta(days=1)).isoformat()
            today=date.today().isoformat()

            status,created=self.request("POST","/api/transactions",{
                "id":"period-locked-original","title":"پیش از بستن دوره","amount":25000,
                "kind":"income","category":"test","createdAt":yesterday+"T12:00:00Z"},owner)
            self.assertEqual(status,201)

            status,manager_user=self.request("POST","/api/users",{
                "username":"period-manager","password":"period-manager-password-2026","role":"manager"},owner)
            self.assertEqual(status,201)
            status,manager_login=self.request("POST","/api/login",{
                "username":"period-manager","password":"period-manager-password-2026"})
            self.assertEqual(status,200)
            manager=manager_login["token"]

            close_payload={"startDate":yesterday,"endDate":yesterday}
            status,denied=self.request("POST","/api/accounting/periods/close",close_payload,manager)
            self.assertEqual(status,403)
            status,closed=self.request("POST","/api/accounting/periods/close",close_payload,owner)
            self.assertEqual(status,201)
            self.assertTrue(closed["id"])

            status,periods=self.request("GET","/api/accounting/periods",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(len(periods["items"]),1)
            self.assertEqual(periods["items"][0]["startDate"],yesterday)
            self.assertEqual(periods["items"][0]["endDate"],yesterday)
            self.assertEqual(periods["items"][0]["closedBy"],"period-owner")

            for invalid in (
                {"startDate":today,"endDate":yesterday},
                {"startDate":"2026-02-30","endDate":"2026-02-30"},
                {"startDate":today,"endDate":"2099-01-01"},
                {"startDate":yesterday,"endDate":today},
            ):
                status,_=self.request("POST","/api/accounting/periods/close",invalid,owner)
                self.assertIn(status,(400,409))
            status,duplicate=self.request("POST","/api/accounting/periods/close",close_payload,owner)
            self.assertEqual(status,409)

            status,backdated=self.request("POST","/api/transactions",{
                "id":"period-backdated-new","title":"ثبت در دوره بسته","amount":1000,
                "kind":"income","category":"test","createdAt":yesterday+"T13:00:00Z"},owner)
            self.assertEqual(status,409)
            status,reversal=self.request("DELETE","/api/transactions/"+created["id"],token=owner)
            self.assertEqual(status,409)
            db=server.connect()
            try:
                original_status=db.execute("SELECT status FROM transactions WHERE id=? AND tenant_id=?",
                    (created["id"],setup["user"]["tenantId"])).fetchone()["status"]
                self.assertEqual(original_status,"posted")
            finally:
                db.close()

            status,product=self.request("POST","/api/products",{
                "id":"period-zero-cost-product","name":"کالای بدون بهای خرید",
                "sellPrice":10000,"costPrice":0},owner)
            self.assertEqual(status,201)
            status,open_zero_cost_movement=self.request("POST","/api/inventory/movements",{
                "id":"period-zero-cost-open-movement","productId":product["id"],
                "movementType":"adjustment","quantityDelta":1,"unitCost":0,"note":"open period test"},owner)
            self.assertEqual(status,201,"inventory movements after the prior closed period must remain allowed")

            status,current=self.request("POST","/api/transactions",{
                "id":"period-current-open","title":"ثبت روز باز","amount":5000,
                "kind":"income","category":"test","createdAt":today+"T12:00:00Z"},owner)
            self.assertEqual(status,201)

            status,closedToday=self.request("POST","/api/accounting/periods/close",{
                "startDate":today,"endDate":today},owner)
            self.assertEqual(status,201)
            status,zero_cost_movement=self.request("POST","/api/inventory/movements",{
                "id":"period-zero-cost-closed-movement","productId":product["id"],
                "movementType":"adjustment","quantityDelta":1,"unitCost":0,"note":"closed period test"},owner)
            self.assertEqual(status,409,"zero-value inventory changes must be rejected when their posting date is closed")
            status,audit_result=self.request("GET","/api/audit-log",token=owner)
            self.assertEqual(status,200)
            self.assertTrue(any(item["action"]=="accounting.period.closed" for item in audit_result["items"]))

            status,snapshot=self.request("GET","/api/backup",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(len(snapshot["tables"]["accounting_periods"]),2)
            db=server.connect()
            try:
                db.execute("DELETE FROM accounting_periods WHERE tenant_id=?",(setup["user"]["tenantId"],))
                db.commit()
            finally:
                db.close()
            status,restored=self.request("POST","/api/backup/restore",snapshot,owner)
            self.assertEqual(status,200)
            status,periods_after_restore=self.request("GET","/api/accounting/periods",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(len(periods_after_restore["items"]),2)
            self.assertEqual({item["startDate"] for item in periods_after_restore["items"]},{yesterday,today})

            legacy_snapshot=json.loads(json.dumps(snapshot))
            legacy_snapshot["version"]=1
            legacy_snapshot["tables"].pop("accounting_periods")
            legacy_snapshot["tables"].pop("cash_register_shifts")
            for journal in legacy_snapshot["tables"]["journal_entries"]:
                journal.pop("__rowid",None)
            status,legacy_restored=self.request("POST","/api/backup/restore",legacy_snapshot,owner)
            self.assertEqual(status,200)
            status,periods_after_legacy_restore=self.request("GET","/api/accounting/periods",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(len(periods_after_legacy_restore["items"]),2,
                "legacy v1 restores must preserve current period locks absent from the old format")
        finally:
            server.DB_PATH=original_db_path

    def test_accounting_period_close_rejects_unbalanced_journal_entries(self):
        original_db_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"unbalanced-period.sqlite3"
        try:
            status,setup=self.request("POST","/api/setup",{
                "businessName":"Unbalanced Period","username":"balance-owner",
                "password":"balance-owner-password-2026"})
            self.assertEqual(status,200)
            owner=setup["token"]
            today=date.today().isoformat()
            status,transaction=self.request("POST","/api/transactions",{
                "id":"period-unbalanced-entry","title":"سند آزمایشی","amount":15000,
                "kind":"income","category":"test","createdAt":today+"T12:00:00Z"},owner)
            self.assertEqual(status,201)
            db=server.connect()
            try:
                entry=db.execute("""SELECT id FROM journal_entries WHERE tenant_id=?
                    AND source_type='transaction' AND source_id=?""",
                    (setup["user"]["tenantId"],transaction["id"])).fetchone()
                self.assertIsNotNone(entry)
                cash=db.execute("SELECT id FROM accounts WHERE tenant_id=? AND code='1000'",
                    (setup["user"]["tenantId"],)).fetchone()
                self.assertIsNotNone(cash)
                db.execute("""INSERT INTO journal_entries
                    (id,tenant_id,entry_date,memo,source_type,source_id,created_by,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""",("manual-unbalanced-period",setup["user"]["tenantId"],
                    today,"سند نامتوازن آزمایشی","test_unbalanced","manual-unbalanced",
                    setup["user"]["id"],int(time.time())))
                db.execute("""INSERT INTO journal_lines
                    (id,tenant_id,entry_id,account_id,debit,credit) VALUES(?,?,?,?,?,?)""",
                    ("manual-unbalanced-line",setup["user"]["tenantId"],"manual-unbalanced-period",
                    cash["id"],1000,0))
                db.commit()
            finally:
                db.close()
            status,result=self.request("POST","/api/accounting/periods/close",
                {"startDate":today,"endDate":today},owner)
            self.assertEqual(status,409)
            self.assertIn("نامتوازن",result["error"])
            status,periods=self.request("GET","/api/accounting/periods",token=owner)
            self.assertEqual(status,200)
            self.assertEqual(periods["items"],[])
        finally:
            server.DB_PATH=original_db_path

    def test_custom_chart_of_accounts_is_owner_manager_only_and_tenant_scoped(self):
        original_db_path=server.DB_PATH
        server.DB_PATH=Path(self.temp.name)/"custom-chart.sqlite3"
        try:
            status,setup=self.request("POST","/api/setup",{
                "businessName":"Custom Chart","username":"chart-owner",
                "password":"chart-owner-password-2026"})
            self.assertEqual(status,200)
            owner=setup["token"]
            tokens={}
            for role in ("manager","cashier"):
                status,_=self.request("POST","/api/users",{
                    "username":"chart-"+role,"password":"chart-"+role+"-password-2026","role":role},owner)
                self.assertEqual(status,201)
                status,login=self.request("POST","/api/login",{
                    "username":"chart-"+role,"password":"chart-"+role+"-password-2026"})
                self.assertEqual(status,200)
                tokens[role]=login["token"]

            status,accounts=self.request("GET","/api/accounting/accounts",token=owner)
            self.assertEqual(status,200)
            self.assertTrue(any(item["code"]=="1000" for item in accounts["items"]))

            payload={"code":"1300","name":"سپرده کوتاه‌مدت","accountType":"asset"}
            status,created=self.request("POST","/api/accounting/accounts",payload,owner)
            self.assertEqual(status,201)
            self.assertTrue(created["id"])
            status,duplicate=self.request("POST","/api/accounting/accounts",payload,tokens["manager"])
            self.assertEqual(status,409)
            status,manager_created=self.request("POST","/api/accounting/accounts",{
                "code":"5300","name":"هزینه بازاریابی","accountType":"expense"},tokens["manager"])
            self.assertEqual(status,201)
            status,denied=self.request("POST","/api/accounting/accounts",{
                "code":"1400","name":"حساب غیرمجاز","accountType":"asset"},tokens["cashier"])
            self.assertEqual(status,403)
            status,persian_code=self.request("POST","/api/accounting/accounts",{
                "code":"۱۴۰۰","name":"بدهی کوتاه‌مدت","accountType":"liability"},owner)
            self.assertEqual(status,201)
            for invalid in (
                {"code":"12A4","name":"Invalid code","accountType":"asset"},
                {"code":"1400","name":"Invalid type","accountType":"other"},
                {"code":"1500","name":"Invalid balance","accountType":"asset","normalBalance":"credit"},
                {"code":"1600","name":"","accountType":"asset"},
            ):
                status,_=self.request("POST","/api/accounting/accounts",invalid,owner)
                self.assertEqual(status,400)
            status,refreshed=self.request("GET","/api/accounting/accounts",token=owner)
            self.assertEqual(status,200)
            custom={item["code"]:item for item in refreshed["items"]}
            self.assertEqual(custom["1300"]["normalBalance"],"debit")
            self.assertEqual(custom["5300"]["normalBalance"],"debit")
            self.assertEqual(custom["1300"]["name"],"سپرده کوتاه‌مدت")
            self.assertEqual(custom["1400"]["normalBalance"],"credit")
            self.assertEqual(custom["1400"]["name"],"بدهی کوتاه‌مدت")
            status,denied_deactivate=self.request("DELETE","/api/accounting/accounts/"+created["id"],token=tokens["cashier"])
            self.assertEqual(status,403)
            status,deactivated=self.request("DELETE","/api/accounting/accounts/"+created["id"],token=tokens["manager"])
            self.assertEqual(status,200)
            self.assertTrue(deactivated["deactivated"])
            status,repeated_deactivate=self.request("DELETE","/api/accounting/accounts/"+created["id"],token=owner)
            self.assertEqual(status,404)
            status,posted_for_cash=self.request("POST","/api/transactions",{
                "id":"chart-account-use","title":"Cash account usage","amount":1000,
                "kind":"income","category":"test","createdAt":"2026-11-17T10:00:00Z"},owner)
            self.assertEqual(status,201)
            status,used_account=self.request("DELETE","/api/accounting/accounts/"+custom["1000"]["id"],token=owner)
            self.assertEqual(status,409)
            # System accounts must remain available even before their first journal posting.
            status,protected_unused=self.request("DELETE","/api/accounting/accounts/"+custom["5100"]["id"],token=owner)
            self.assertEqual(status,409)
            self.assertIn("سیستمی",protected_unused["error"])
            status,after_deactivate=self.request("GET","/api/accounting/accounts",token=owner)
            self.assertEqual(status,200)
            self.assertFalse(any(item["code"]=="1300" for item in after_deactivate["items"]))
            self.assertTrue(any(item["code"]=="1000" for item in after_deactivate["items"]))
            db=server.connect()
            try:
                tenant_id=setup["user"]["tenantId"]
                self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE tenant_id=? AND code IN ('1300','1400','5300')",
                    (tenant_id,)).fetchone()[0],3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE tenant_id=? AND action='account.created'",
                    (tenant_id,)).fetchone()[0],3)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_log WHERE tenant_id=? AND action='account.deactivated'",
                    (tenant_id,)).fetchone()[0],1)
                self.assertEqual(db.execute("SELECT active FROM accounts WHERE tenant_id=? AND id=?",
                    (tenant_id,created["id"])).fetchone()["active"],0)
            finally:
                db.close()
        finally:
            server.DB_PATH=original_db_path


    def test_role_and_password_changes_revoke_existing_sessions(self):
        original_db_path = server.DB_PATH
        server.DB_PATH = Path(self.temp.name) / "role-password-session-revocation.sqlite3"
        try:
            status, setup = self.request("POST", "/api/setup", {
                "businessName": "Session Revocation",
                "username": "session-owner",
                "password": "session-owner-password-2026"
            })
            self.assertEqual(status, 200)
            owner_token = setup["token"]

            status, created = self.request("POST", "/api/users", {
                "username": "session-staff",
                "password": "session-staff-password-2026",
                "role": "viewer"
            }, owner_token)
            self.assertEqual(status, 201)
            staff_id = created["id"]

            status, first_login = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-password-2026"
            })
            self.assertEqual(status, 200)
            status, second_login = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-password-2026"
            })
            self.assertEqual(status, 200)

            # Changing a role must invalidate every token issued under the old role.
            status, changed_role = self.request("POST", "/api/users/" + staff_id + "/role",
                {"role": "cashier"}, owner_token)
            self.assertEqual(status, 200)
            self.assertTrue(changed_role["sessionsRevoked"])
            for token in (first_login["token"], second_login["token"]):
                with self.subTest(stage="role-change", token=token[:8]):
                    status, _ = self.request("GET", "/api/me", token=token)
                    self.assertEqual(status, 401)

            status, fresh_login = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-password-2026"
            })
            self.assertEqual(status, 200)
            status, second_fresh_login = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-password-2026"
            })
            self.assertEqual(status, 200)

            # An owner password reset invalidates all active sessions and the old password.
            status, reset = self.request("POST", "/api/users/" + staff_id + "/password",
                {"password": "session-staff-reset-password-2026"}, owner_token)
            self.assertEqual(status, 200)
            self.assertTrue(reset["sessionsRevoked"])
            for token in (fresh_login["token"], second_fresh_login["token"]):
                with self.subTest(stage="owner-password-reset", token=token[:8]):
                    status, _ = self.request("GET", "/api/me", token=token)
                    self.assertEqual(status, 401)
            status, old_password = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-password-2026"
            })
            self.assertEqual(status, 401)
            status, after_reset = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-reset-password-2026"
            })
            self.assertEqual(status, 200)

            # Self-service password change must also revoke parallel sessions,
            # while returning a valid replacement token to the changing client.
            status, parallel_login = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-reset-password-2026"
            })
            self.assertEqual(status, 200)
            status, changed_password = self.request("POST", "/api/account/password", {
                "currentPassword": "session-staff-reset-password-2026",
                "newPassword": "session-staff-self-change-password-2026"
            }, after_reset["token"])
            self.assertEqual(status, 200)
            self.assertTrue(changed_password.get("token"))
            status, me = self.request("GET", "/api/me", token=changed_password["token"])
            self.assertEqual(status, 200)
            self.assertEqual(me["user"]["username"], "session-staff")
            status, revoked_parallel = self.request("GET", "/api/me", token=parallel_login["token"])
            self.assertEqual(status, 401)
            status, wrong_old_password = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-reset-password-2026"
            })
            self.assertEqual(status, 401)
            status, new_password_login = self.request("POST", "/api/login", {
                "username": "session-staff",
                "password": "session-staff-self-change-password-2026"
            })
            self.assertEqual(status, 200)
        finally:
            server.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
