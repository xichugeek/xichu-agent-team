"""LAN view authentication and write isolation, using synthetic clients only."""
import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from control_center.app import ControlCenter, Handler, Server
from control_center.lan_access import LanAccess

PASSWORD = "SyntheticOnly_12345"


def credential(password=PASSWORD):
    return "Basic " + base64.b64encode(("viewer:" + password).encode()).decode()


class SyntheticLanHandler(Handler):
    def setup(self):
        super().setup()
        self.client_address = ("192.168.50.22", self.client_address[1])


class LanAccessTests(unittest.TestCase):
    def test_explicit_configuration_is_private_persistent_and_validated_before_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            password = LanAccess.configure(root, ["192.168.50.10/24"])
            self.assertGreaterEqual(len(password), 12)
            path = root / "output/control_center/lan-access.local.json"
            self.assertTrue(LanAccess.load(root).enabled)
            self.assertEqual(LanAccess.configure(root, ["192.168.50.20/24"]), password)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                LanAccess.configure(root, ["0.0.0.0/0"])
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_default_disabled_and_config_stays_local(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertFalse(LanAccess.load(root).enabled)
            path = root / "output/control_center/lan-access.local.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"enabled": True, "addresses": ["192.168.50.10/24"], "password": PASSWORD}), encoding="utf-8")
            access = LanAccess.load(root)
            self.assertTrue(access.enabled)
            self.assertEqual(access.urls(18765), ["http://192.168.50.10:18765/"])

    def test_only_explicit_host_and_subnet_are_allowed(self):
        access = LanAccess(["192.168.50.10/24"], PASSWORD)
        self.assertTrue(access.allows("192.168.50.22", "192.168.50.10:18765", 18765))
        for peer, host in (("192.168.51.22", "192.168.50.10:18765"), ("192.168.50.22", "evil.example:18765"),
                           ("192.168.50.22", "localhost:18765"), ("192.168.50.22", "192.168.50.10:80"),
                           ("203.0.113.1", "192.168.50.10:18765")):
            self.assertFalse(access.allows(peer, host, 18765))
        for address in ("0.0.0.0/0", "192.168.50.1/0", "127.0.0.1/8", "203.0.113.1/24", "192.168.50.0/24"):
            with self.assertRaises(ValueError):
                LanAccess([address], PASSWORD)
        with self.assertRaises(ValueError):
            LanAccess(["192.168.50.10/24"], "short")

    def test_bad_passwords_are_rate_limited_without_logging_credentials(self):
        access = LanAccess(["192.168.50.10/24"], PASSWORD)
        self.assertEqual(access.authenticate("192.168.50.22", ""), 401)
        self.assertEqual(access.authenticate("192.168.50.22", "Basic !!malformed!!"), 401)
        for _ in range(7):
            self.assertEqual(access.authenticate("192.168.50.22", credential("wrong-password")), 401)
        self.assertEqual(access.authenticate("192.168.50.22", credential()), 429)
        with patch("control_center.lan_access.time.monotonic", return_value=10**12):
            self.assertEqual(access.authenticate("192.168.50.22", credential()), 200)

    def test_http_auth_all_pages_and_remote_cannot_write_or_obtain_reset_token(self):
        with tempfile.TemporaryDirectory() as folder:
            center = ControlCenter(Path(folder))
            server = Server(0, center, LanAccess(["192.168.50.10/24"], PASSWORD))
            server.RequestHandlerClass = SyntheticLanHandler
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, path, auth=None, host=None, body=None, extra=None):
                headers = {"Host": host or f"192.168.50.10:{server.server_port}", **(extra or {})}
                if auth:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
                try:
                    conn.request(method, path, body=body, headers=headers)
                    response = conn.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    conn.close()
            try:
                for route in ("/", "/static/studio.js", "/api/agent", "/api/snapshot", "/api/site", "/api/health"):
                    code, headers, body = request("GET", route)
                    self.assertEqual(code, 401)
                    self.assertIn("Basic", headers["WWW-Authenticate"])
                    self.assertNotIn(PASSWORD.encode(), body)
                    self.assertEqual(request("GET", route, credential())[0], 200)
                self.assertEqual(request("GET", "/", credential(), "evil.example")[0], 403)
                self.assertEqual(request("GET", "/", credential(), f"localhost:{server.server_port}", extra={"X-Forwarded-For": "127.0.0.1"})[0], 403)
                agent = json.loads(request("GET", "/api/agent", credential())[2])
                self.assertTrue(agent["lan_read_only"])
                self.assertIsNone(agent["reset_token"])
                health = json.loads(request("GET", "/api/health", credential())[2])
                self.assertTrue(health["read_only"])
                self.assertFalse(health["account_reset_enabled"])
                with patch.object(center.resets, "start") as reset, patch.object(center, "start_validation") as validate:
                    for route in ("/api/accounts/codex_a/reset", "/api/episodes/005/validate"):
                        self.assertEqual(request("POST", route, credential(), body="{}")[0], 403)
                    reset.assert_not_called()
                    validate.assert_not_called()
                self.assertNotIn(PASSWORD.encode(), request("GET", "/api/health", credential())[2])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
