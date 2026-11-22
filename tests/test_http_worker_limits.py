"""Regression tests for bounded HTTP worker admission and cleanup."""

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import server


class BoundedHttpServerTests(unittest.TestCase):
    def setUp(self):
        self.httpd = server.BoundedThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)

    def tearDown(self):
        self.httpd.server_close()

    def test_saturated_pool_returns_503_and_closes_connection(self):
        client, peer = socket.socketpair()
        self.httpd._worker_slots = threading.BoundedSemaphore(0)
        try:
            with mock.patch.object(socket.socket, "settimeout", autospec=True) as settimeout:
                self.httpd.process_request(client, ("127.0.0.1", 12345))
            settimeout.assert_called_once_with(server.REQUEST_TIMEOUT_SECONDS)
            response = peer.recv(1024)
            self.assertEqual(
                response,
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Connection: close\r\nContent-Length: 0\r\n\r\n",
            )
            self.assertEqual(peer.recv(1), b"")
            self.assertEqual(self.httpd._worker_slots._value, 0)
        finally:
            peer.close()

    def test_incomplete_request_is_closed_after_socket_inactivity_timeout(self):
        client, peer = socket.socketpair()
        peer.settimeout(1)
        try:
            with mock.patch.object(server, "REQUEST_TIMEOUT_SECONDS", 0.05):
                self.httpd.process_request(client, ("127.0.0.1", 12345))
            peer.sendall(b"GET / HTTP/1.1\\r\\n")
            self.assertEqual(peer.recv(1), b"")
            self.assertEqual(self.httpd._worker_slots._value, server.MAX_HTTP_WORKERS)
        finally:
            peer.close()

    def test_thread_start_failure_releases_slot_and_closes_socket(self):
        client, peer = socket.socketpair()
        try:
            with mock.patch.object(ThreadingHTTPServer, "process_request",
                                   side_effect=RuntimeError("thread start failed")):
                with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                    self.httpd.process_request(client, ("127.0.0.1", 12345))
            self.assertEqual(self.httpd._worker_slots._value, server.MAX_HTTP_WORKERS)
            self.assertEqual(client.fileno(), -1)
        finally:
            peer.close()

    def test_worker_slot_is_released_after_request_thread_finishes(self):
        acquired = self.httpd._worker_slots.acquire(blocking=False)
        self.assertTrue(acquired)
        with mock.patch.object(ThreadingHTTPServer, "process_request_thread", return_value=None):
            self.httpd.process_request_thread(object(), ("127.0.0.1", 12345))
        self.assertEqual(self.httpd._worker_slots._value, server.MAX_HTTP_WORKERS)


if __name__ == "__main__":
    unittest.main()
