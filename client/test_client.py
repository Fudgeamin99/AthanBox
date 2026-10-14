"""Tests for the Azan SIP Client, against a small stand-in for the box:
    python3 -m unittest discover -s client"""
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import azan_client as ac


class FakeBox(BaseHTTPRequestHandler):
    allowed = True
    seen = []

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Set-Cookie", "azan=secret")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeBox.seen.append((self.path, self.headers.get("Host"), self.headers.get("Cookie")))
        if self.path == "/api/ping":
            return self._send(200, b'{"app": "azan-sip", "version": "0.4.4"}', "application/json")
        if not FakeBox.allowed and self.path.startswith(("/tv", "/api/tv")):
            return self._send(403, b"TV screens can't connect", "text/plain")
        if self.path.startswith("/tv?") or self.path == "/tv":
            return self._send(200, b"<html><body><p>prayer times</p></body></html>", "text/html; charset=utf-8")
        if self.path.startswith("/api/tv"):
            return self._send(200, b'{"now": 1, "volume": 0.3, "playing": null}', "application/json")
        if self.path == "/tv/sound/abc":
            return self._send(200, b"RIFF" + b"\0" * 100000, "audio/wav")
        return self._send(404, b"no", "text/plain")


def serve(handler, port=0):
    s = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        ac.CONFIG = Path(cls.tmp.name) / "client.json"
        cls.box = serve(FakeBox)
        cls.box_base = f"http://127.0.0.1:{cls.box.server_address[1]}"
        ac.Handler.watch = ac.Watch()
        cls.client = serve(ac.Handler)
        cls.port = cls.client.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.box.shutdown()
        cls.client.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakeBox.allowed = True
        FakeBox.seen = []
        ac.save_config({"box": self.box_base, "name": "Main hall", "volume": 60})

    def get(self, path, method="GET", body=None, host="127.0.0.1"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers={"Host": f"{host}:{self.port}", "Cookie": "mine=1",
                                                    **({"Content-Type": "application/json"} if body else {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_addresses(self):
        self.assertEqual(ac.parse_address("192.168.1.20"), ("192.168.1.20", None))
        self.assertEqual(ac.parse_address(" http://Azan.local:8765/tv "), ("azan.local", 8765))
        self.assertEqual(ac.parse_address("10.0.0.5:80"), ("10.0.0.5", 80))
        for bad in ("", "http://", "a b", "1.2.3.4:99999", "127.0.0.1", "localhost", "x:y"):
            self.assertIsNone(ac.parse_address(bad), bad)
        self.assertEqual(ac.base_url("azan.local", 80), "http://azan.local")
        self.assertEqual(ac.base_url("10.0.0.5", 8765), "http://10.0.0.5:8765")

    def test_start_page_has_the_screen_name(self):
        r, _ = self.get("/")
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/tv?name=Main+hall")

    def test_tv_page_comes_from_the_box_with_the_settings_button(self):
        r, body = self.get("/tv?name=Main+hall")
        self.assertEqual(r.status, 200)
        self.assertIn(b"prayer times", body)
        self.assertIn(ac.INJECT + b"</body>", body)
        self.assertIsNone(r.getheader("Set-Cookie"))
        self.assertIsNone(r.getheader("X-Frame-Options"))
        path, host, cookie = FakeBox.seen[-1]
        self.assertEqual(path, "/tv?name=Main+hall")
        self.assertEqual(host, self.box_base[7:])   # the box's own address, which it accepts
        self.assertIsNone(cookie)                   # nothing of the screen's goes to the box

    def test_own_volume(self):
        r, body = self.get("/api/tv?id=x&sound=1")
        self.assertEqual(json.loads(body)["volume"], 0.6)
        self.get("/client/api/settings", "POST", json.dumps({"volume": 25}))
        _, body = self.get("/api/tv?id=x&sound=1")
        self.assertEqual(json.loads(body)["volume"], 0.25)

    def test_recordings_pass_through(self):
        r, body = self.get("/tv/sound/abc")
        self.assertEqual((r.status, r.getheader("Content-Type"), len(body)), (200, "audio/wav", 100004))

    def test_screen_cannot_change_the_box(self):
        r, _ = self.get("/api/login", "POST", json.dumps({"password": "x"}))
        self.assertEqual(r.status, 403)
        self.assertFalse(any(p == "/api/login" for p, *_ in FakeBox.seen))

    def test_only_this_computer(self):
        r, _ = self.get("/client/api/status", host="evil.example")
        self.assertEqual(r.status, 403)

    def test_box_gone_shows_connecting(self):
        ac.save_config({"box": f"http://127.0.0.1:{free_port()}"})
        r, _ = self.get("/tv?name=x")
        self.assertEqual(r.status, 302)
        self.assertEqual(r.getheader("Location"), "/client/connecting?to=%2Ftv%3Fname%3Dx")
        r, _ = self.get("/api/tv")
        self.assertEqual(r.status, 502)

    def test_tv_screens_not_allowed_shows_connecting(self):
        FakeBox.allowed = False
        r, _ = self.get("/tv")
        self.assertEqual(r.status, 302)
        self.assertTrue(r.getheader("Location").startswith("/client/connecting"))

    def test_no_box_yet_opens_settings(self):
        ac.save_config({})
        r, _ = self.get("/tv")
        self.assertEqual((r.status, r.getheader("Location")), (302, "/client/"))
        r, body = self.get("/client/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"Screen settings", body)

    def test_connect(self):
        ac.save_config({})
        _, body = self.get("/client/api/connect", "POST", json.dumps({"address": "not an address"}))
        self.assertIn("doesn't look like an address", json.loads(body)["error"])
        _, body = self.get("/client/api/connect", "POST", json.dumps({"address": f"192.0.2.1:{free_port()}"}))
        self.assertIn("Nothing answers", json.loads(body)["error"])
        # The box itself (the client refuses 127.x as an address someone types, so pretend)
        orig = ac.parse_address
        ac.parse_address = lambda t: ("127.0.0.1", self.box.server_address[1]) if t == "the-box" else orig(t)
        try:
            _, body = self.get("/client/api/connect", "POST", json.dumps({"address": "the-box", "name": "Women's hall"}))
        finally:
            ac.parse_address = orig
        r = json.loads(body)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["box"], r["name"], r["state"], r["warning"]), (self.box_base, "Women's hall", "ok", None))
        self.assertEqual(ac.load_config()["box"], self.box_base)

    def test_find_box_tries_the_usual_ports(self):
        port = self.box.server_address[1]
        old = ac.BOX_PORTS
        ac.BOX_PORTS = (free_port(), port)
        try:
            ac.parse_address, orig = (lambda t: ("127.0.0.1", None)), ac.parse_address
            r = ac.find_box("whatever")
        finally:
            ac.BOX_PORTS = old
            ac.parse_address = orig
        self.assertEqual((r["state"], r["base"], r["version"]), ("ok", self.box_base, "0.4.4"))

    def test_status(self):
        _, body = self.get("/client/api/status")
        st = json.loads(body)
        self.assertEqual((st["box"], st["name"], st["volume"], st["version"]), (self.box_base, "Main hall", 60, ac.__version__))


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    unittest.main()
