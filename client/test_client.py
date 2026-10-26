"""Tests for the Azan SIP Client, against a small stand-in for the box:
    python3 -m unittest discover -s client"""
import hashlib
import http.client
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import azan_client as ac


class FakeBox(BaseHTTPRequestHandler):
    allowed = True
    seen = []
    tv = {"now": 1, "volume": 0.3, "playing": None}
    state = {"now": "2026-10-25T15:00:00+00:00", "timezone": "Europe/London", "look": {"scale": 120},
             "today": [{"name": "fajr", "time": "05:30"}, {"name": "isha", "time": "18:30"}]}

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
            return self._send(200, json.dumps(FakeBox.tv).encode(), "application/json")
        if self.path == "/api/state":
            return self._send(200, json.dumps(FakeBox.state).encode(), "application/json")
        if self.path == "/tv/sound/abc":
            return self._send(200, b"RIFF" + b"\0" * 100000, "audio/wav")
        return self._send(404, b"no", "text/plain")


# What wayland-info prints inside cage with a TV and a laptop's own screen (HiDPI, scale 2)
WAYLAND_INFO = """interface: 'wl_compositor',                              version:  4, name: 1
interface: 'wl_output',                                  version:  4, name: 12
	x: 0, y: 0, scale: 1,
	physical_width: 600 mm, physical_height: 340 mm,
	make: 'Samsung', model: 'TV',
	subpixel_orientation: unknown, output_transform: normal,
	mode:
		width: 1920 px, height: 1080 px, refresh: 60.000 Hz,
		flags: current preferred
	mode:
		width: 1280 px, height: 720 px, refresh: 60.000 Hz,
		flags: none
	name: HDMI-A-1
interface: 'wl_output',                                  version:  4, name: 13
	x: 1920, y: 0, scale: 2,
	make: 'BOE', model: '0x0abc',
	mode:
		width: 2560 px, height: 1600 px, refresh: 60.000 Hz,
		flags: current preferred
	name: eDP-1
interface: 'zxdg_output_manager_v1',                     version:  3, name: 7
	xdg_output_v1
		output: 12
		name: 'HDMI-A-1'
		description: 'Samsung TV'
		logical_x: 0, logical_y: 0
		logical_width: 1920, logical_height: 1080
	xdg_output_v1
		output: 13
		name: 'eDP-1'
		description: 'Built-in display'
		logical_x: 1920, logical_y: 0
		logical_width: 1280, logical_height: 800
interface: 'wl_seat',                                    version:  7, name: 8
"""


class FakeGitHub(BaseHTTPRequestHandler):
    deb = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if self.path.split("?")[0] == "/releases":
            def client(v, **kw):
                return {"tag_name": f"client-v{v}", "assets": [
                    {"name": "azan-sip-client.deb", "browser_download_url": base + "/deb", "size": len(self.deb)},
                    {"name": f"azan-sip-client_{v}_all.deb", "browser_download_url": base + "/deb", "size": len(self.deb),
                     "digest": "sha256:" + hashlib.sha256(self.deb).hexdigest()}], **kw}
            body = json.dumps([
                client("9.9.11", prerelease=True), client("9.9.10", draft=True),
                # A Box release: the client packages attached to Box releases up to 0.7.2 don't count
                {"tag_name": "box-v10.0.0", "assets": [
                    {"name": "AzanTV.apk", "browser_download_url": base + "/apk", "size": 1},
                    {"name": "azan-sip-client_10.0.0_all.deb", "browser_download_url": base + "/deb", "size": 1}]},
                client("9.9.9"), client("9.9.1")]).encode()
        elif self.path == "/deb":
            body = self.deb
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
        ac.UPDATE_STATE = Path(cls.tmp.name) / "update" / "state.json"
        cls.github = serve(FakeGitHub)
        ac.RELEASES = f"http://127.0.0.1:{cls.github.server_address[1]}/releases?per_page=100"
        ac.Handler.watch = ac.Watch()
        ac.Handler.updates = ac.Updates()
        ac.Handler.pairing = ac.Pairing()
        ac.POWER_STATE = Path(cls.tmp.name) / "power"
        ac.ETC = Path(cls.tmp.name) / "etc"
        cls.client = serve(ac.Handler)
        cls.port = cls.client.server_address[1]
        cls.remote = serve(ac.RemoteHandler)   # the settings from a phone or computer
        cls.rport = cls.remote.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.box.shutdown()
        cls.github.shutdown()
        cls.client.shutdown()
        cls.remote.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        FakeBox.allowed = True
        FakeBox.seen = []
        FakeBox.tv = {"now": 1, "volume": 0.3, "playing": None}
        ac.save_config({"box": self.box_base, "name": "Main hall", "volume": 60})
        ac.screens_file().unlink(missing_ok=True)

    def get(self, path, method="GET", body=None, host="127.0.0.1", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers={"Host": f"{host}:{self.port}", "Cookie": "mine=1",
                                                    **({"Content-Type": "application/json"} if body else {}),
                                                    **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def phone(self, path, body=None, cookie="", headers=None):
        """A request from a phone or computer on the network, to the remote settings port."""
        c = http.client.HTTPConnection("127.0.0.1", self.rport, timeout=10)
        h = {"Host": f"192.168.1.31:{self.rport}", **({"Cookie": cookie} if cookie else {}),
             **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
        c.request("POST" if body is not None else "GET", path, body=None if body is None else json.dumps(body), headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_phone_signs_in_with_the_code_on_the_tv(self):
        ac.Handler.pairing = ac.Pairing()
        tv = lambda: json.loads(self.get("/client/api/status")[1])
        # Not signed in: the sign-in page, and nothing else
        r, body = self.phone("/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"The code on the TV", body)
        for path, req in (("/client/api/status", None), ("/client/api/settings", {"volume": 5}), ("/client/api/update", {}),
                          ("/client/api/connect", {"address": "10.0.0.9"}), ("/client/api/chime", {}),
                          ("/client/api/shutdown", {})):
            self.assertEqual(self.phone(path, req)[0].status, 401, path)
        for path in ("/tv", "/api/tv", "/client/wall", "/client/api/forget", "/display"):   # never the box's pages
            self.assertEqual(self.phone(path)[0].status, 404, path)
        self.assertEqual(tv()["volume"], 60)
        self.assertIsNone(tv()["pair_code"])
        # Asking for a code shows it on the TV, and only there
        r, body = self.phone("/client/api/signin/code", {})
        self.assertTrue(json.loads(body)["ok"])
        code = tv()["pair_code"]
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(json.loads(self.phone("/client/api/signin/code", {})[1])["ok"], True)
        self.assertEqual(tv()["pair_code"], code)   # asking again keeps the same code
        wrong = "%06d" % ((int(code) + 1) % 10 ** 6)
        r, body = self.phone("/client/api/signin", {"code": wrong})
        self.assertFalse(json.loads(body)["ok"])
        self.assertIsNone(r.getheader("Set-Cookie"))
        # From a page elsewhere, or not as JSON: refused
        self.assertEqual(self.phone("/client/api/signin", {"code": code}, headers={"Origin": "http://evil.example"})[0].status, 403)
        self.assertEqual(self.phone("/client/api/signin", {"code": code}, headers={"Content-Type": "text/plain"})[0].status, 403)
        r, body = self.phone("/client/api/signin", {"code": code[:3] + " " + code[3:]})
        self.assertTrue(json.loads(body)["ok"])
        cookie = r.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", r.getheader("Set-Cookie"))
        self.assertIn("SameSite=Strict", r.getheader("Set-Cookie"))
        self.assertIsNone(tv()["pair_code"])   # used: gone from the TV
        self.assertNotIn(cookie.split("=", 1)[1], ac.sessions_file().read_text())   # only its hash is kept
        # Signed in: the settings page and its API
        r, body = self.phone("/", cookie=cookie)
        self.assertIn(b"Screen settings", body)
        self.assertNotIn(b"The code on the TV", body)
        st = json.loads(self.phone("/client/api/status", cookie=cookie)[1])
        self.assertTrue(st["remote"])
        self.assertNotIn("pair_code", st)
        self.assertNotIn("remote_url", st)
        r, body = self.phone("/client/api/settings", {"volume": 30, "remote": False, "live_sound": True}, cookie=cookie)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(tv()["volume"], 30)
        self.assertIs(tv()["live_sound"], True)
        self.assertTrue(tv()["remote_on"])   # a phone can't switch phones off
        self.assertEqual(tv()["signed_in"], 1)
        before = tv()["chime"]
        self.assertTrue(json.loads(self.phone("/client/api/chime", {}, cookie=cookie)[1])["ok"])
        self.assertGreater(tv()["chime"], before)   # the TV plays it
        off = ac.CONFIG.parent / "shutdown-now"
        off.unlink(missing_ok=True)
        self.assertTrue(json.loads(self.phone("/client/api/shutdown", {}, cookie=cookie)[1])["ok"])
        self.assertTrue(off.exists())   # a phone can turn the TV's computer off
        self.assertEqual(self.phone("/client/api/status", cookie="azc=made-up")[0].status, 401)
        # Switched off on the TV: refused; and signing everyone out ends the cookie
        self.get("/client/api/settings", "POST", json.dumps({"remote": False}))
        self.assertEqual(self.phone("/", cookie=cookie)[0].status, 403)
        self.get("/client/api/settings", "POST", json.dumps({"remote": True}))
        self.assertEqual(self.phone("/client/api/status", cookie=cookie)[0].status, 200)
        self.assertEqual(json.loads(self.get("/client/api/forget", "POST", "{}")[1])["signed_in"], 0)
        self.assertEqual(self.phone("/client/api/status", cookie=cookie)[0].status, 401)
        self.assertEqual(self.phone("/client/api/forget", {}, cookie=cookie)[0].status, 404)   # the TV's own

    def test_wrong_codes_are_limited(self):
        pr = ac.Pairing()
        self.assertTrue(pr.start())
        code = pr.shown()
        wrong = "%06d" % ((int(code) + 1) % 10 ** 6)
        for _ in range(4):
            self.assertIs(pr.check(wrong), False)
        self.assertIs(pr.check(wrong), False)   # the fifth: the code is gone
        self.assertIsNone(pr.check(code))
        self.assertIsNone(pr.shown())
        self.assertTrue(pr.start())
        for _ in range(5):
            pr.check("%06d" % ((int(pr.shown() or 0) + 1) % 10 ** 6))
        self.assertFalse(pr.start())   # ten wrong in an hour: no new codes for now
        pr.wrong = [t - 3601 for t in pr.wrong]
        self.assertTrue(pr.start())

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


    def test_makkah_live_sound(self):
        st = lambda: json.loads(self.get("/client/api/status")[1])
        self.assertIsNone(st()["live_sound"])   # not chosen yet: the TV app's own choice (web/screen.js sends it)
        for on in (True, False):
            r, body = self.get("/client/api/settings", "POST", json.dumps({"live_sound": on}))
            self.assertIs(json.loads(body)["live_sound"], on)
            self.assertIs(st()["live_sound"], on)
        r, body = self.get("/client/api/settings", "POST", json.dumps({"live_sound": "yes"}))
        self.assertEqual(r.status, 400)
        self.assertIs(st()["live_sound"], False)

    # -- the volume buttons on the screen
    def test_volume_buttons(self):
        steps = []
        for n in (1, -1, -1, 5, 1):
            _, body = self.get("/client/api/settings", "POST", json.dumps({"volume_step": n}))
            steps.append(json.loads(body)["volume"])
        self.assertEqual(steps, [70, 60, 50, 100, 100])
        _, body = self.get("/client/api/settings", "POST", json.dumps({"volume_step": "x"}))
        self.assertFalse(json.loads(body)["ok"])
        _, body = self.get("/client/api/settings", "POST", json.dumps({"volume_step": -20}))
        self.assertEqual(json.loads(body)["volume"], 0)

    # -- two screens or more
    def test_screens_from_wayland_info(self):
        self.assertEqual(ac.parse_wayland_info(WAYLAND_INFO), [
            {"name": "HDMI-A-1", "x": 0, "y": 0, "w": 1920, "h": 1080},
            {"name": "eDP-1", "x": 1920, "y": 0, "w": 1280, "h": 800}])
        # A compositor without xdg-output: from each output's position, mode and scale
        self.assertEqual(ac.parse_wayland_info(WAYLAND_INFO.split("interface: 'zxdg")[0]), [
            {"name": "HDMI-A-1", "x": 0, "y": 0, "w": 1920, "h": 1080},
            {"name": "eDP-1", "x": 1920, "y": 0, "w": 1280, "h": 800}])
        self.assertEqual(ac.parse_wayland_info(""), [])
        # Listed right one first (as cage does with two headless screens): left to right all the same
        swapped = WAYLAND_INFO.replace("logical_x: 0,", "logical_x: 3200,")
        self.assertEqual([s["name"] for s in ac.parse_wayland_info(swapped)], ["eDP-1", "HDMI-A-1"])
        both = ac.parse_wayland_info(WAYLAND_INFO)
        self.assertEqual(ac.shown_screens(both, closed=False), both)
        self.assertEqual([s["name"] for s in ac.shown_screens(both, closed=True)], ["HDMI-A-1"])   # lid closed
        self.assertEqual(ac.shown_screens(both[1:], closed=True), both[1:])   # the laptop's screen is all there is

    def test_browser_restarts_when_a_screen_is_plugged_in_or_out(self):
        tv_and_laptop = WAYLAND_INFO
        laptop = "interface: 'zxdg_output_manager_v1', version: 3, name: 7\n\txdg_output_v1\n\t\toutput: 13\n" \
                 "\t\tname: 'eDP-1'\n\t\tlogical_x: 0, logical_y: 0\n\t\tlogical_width: 1280, logical_height: 800\n"
        readings = iter([tv_and_laptop, tv_and_laptop, "", laptop, laptop, tv_and_laptop, ""])
        restarts, shown = [], []
        ac.time.sleep, sleep = (lambda t: shown.append(len(ac.read_screens()[1]))), ac.time.sleep
        try:
            ac.watch_screens(every=0, read=lambda: next(readings), restart=lambda: restarts.append(len(shown)), rounds=7,
                             wanted=lambda: {})
        finally:
            ac.time.sleep = sleep
        # Unplugged (round 4) and plugged back in (round 6): the browser restarts each time.
        # A failed reading (rounds 3 and 7) changes nothing.
        self.assertEqual(restarts, [3, 5])
        self.assertEqual(shown, [2, 2, 2, 1, 1, 2, 2])

    def test_two_screens_show_the_prayer_times_on_each(self):
        two = [{"name": "HDMI-A-1", "x": 0, "y": 0, "w": 1920, "h": 1080}, {"name": "HDMI-A-2", "x": 1920, "y": 0, "w": 1280, "h": 720}]
        ac.write_json(ac.screens_file(), {"screens": two, "outputs": two})
        _, body = self.get("/client/api/status")
        st = json.loads(body)
        self.assertEqual((st["screens"], st["outputs"]), (two, two))
        r, _ = self.get("/")
        self.assertEqual(r.getheader("Location"), "/client/wall")
        r, body = self.get("/client/wall")
        self.assertEqual(r.status, 200)
        self.assertIn(b"mirror", body)
        r, _ = self.get("/", headers={"Sec-Fetch-Dest": "iframe"})   # never a wall inside a wall
        self.assertEqual(r.getheader("Location"), "/tv?name=Main+hall")
        # A laptop with its lid closed and a TV: the prayer times on the TV only, not across both
        ac.write_json(ac.screens_file(), {"screens": two[1:], "outputs": [{"name": "eDP-1", "x": 0, "y": 0, "w": 1920, "h": 1080}, two[1]]})
        r, _ = self.get("/")
        self.assertEqual(r.getheader("Location"), "/client/wall")
        ac.write_json(ac.screens_file(), {"screens": two[:1], "outputs": two[:1]})
        r, _ = self.get("/")
        self.assertEqual(r.getheader("Location"), "/tv?name=Main+hall")
        ac.write_json(ac.screens_file(), {"screens": "nonsense", "outputs": [{"x": "a"}]})
        self.assertEqual(ac.read_screens(), ([], []))

    def test_second_screen_is_silent(self):
        mirror = {"Referer": f"http://127.0.0.1:{self.port}/tv?name=Main+hall&mirror=1"}
        _, body = self.get("/api/tv?id=x&sound=1", headers=mirror)
        self.assertEqual(json.loads(body)["volume"], 0)
        _, body = self.get("/api/tv?id=x&sound=1", headers={"Referer": f"http://127.0.0.1:{self.port}/tv?name=Main+hall"})
        self.assertEqual(json.loads(body)["volume"], 0.6)

    # -- updates
    def test_update_check_and_update_now(self):
        _, body = self.get("/client/api/update?check=1")
        u = json.loads(body)
        self.assertEqual((u["current"], u["latest"], u["available"], u["error"]), (ac.__version__, "9.9.9", True, None))
        trigger = ac.CONFIG.parent / "update-now"
        trigger.unlink(missing_ok=True)
        _, body = self.get("/client/api/update", "POST", "{}")
        self.assertTrue(json.loads(body)["ok"])
        self.assertTrue(trigger.exists())   # azan-sip-client-update.path starts the update from this
        ac.write_json(ac.UPDATE_STATE, {"started": 1, "step": "installing", "version": "9.9.9"})
        _, body = self.get("/client/api/update")
        self.assertEqual(json.loads(body)["update"]["step"], "installing")
        self.assertGreater(ac.vtuple("0.10.0"), ac.vtuple("0.9.9"))

    def test_shut_down_restart_and_sleep(self):
        folder, quiet = ac.CONFIG.parent, lambda m: None
        for n in ac.TRIGGERS:
            (folder / n).unlink(missing_ok=True)
        ran = []
        run = lambda cmd: ran.append(cmd[-1]) or 0   # (never the real systemctl, even as root)
        rtc = Path(self.tmp.name) / "wakealarm"
        rtc.write_text("")
        with mock.patch.object(ac.os, "geteuid", return_value=0):
            for what, cmd in (("shutdown", "poweroff"), ("restart", "reboot")):
                _, body = self.get(f"/client/api/{what}", "POST", "{}")
                self.assertTrue(json.loads(body)["ok"])
                self.assertEqual(ac.power(quiet, run, rtc), 0)
                self.assertEqual(ran[-1], cmd)
                self.assertFalse((folder / f"{what}-now").exists())   # acted on once
            self.assertEqual(ac.power(quiet, run, rtc), 0)   # nothing asked: stays on
            self.assertEqual(len(ran), 2)
            trigger = folder / "shutdown-now"
            for when in (-600, 600):   # pressed ten minutes ago, or from the future (a wrong clock): stays on
                trigger.write_text("x")
                os.utime(trigger, (time.time() + when,) * 2)
                self.assertEqual(ac.power(quiet, run, rtc), 0)
            trigger.unlink()
            trigger.symlink_to(ac.CONFIG)   # only the client's own file counts
            self.assertEqual(ac.power(quiet, run, rtc), 0)
            trigger.unlink()
            self.assertEqual(len(ran), 2)
            # Night sleep's try: off for three minutes, with the clock set to switch it back on
            with mock.patch.object(ac, "RTC_ALARM", rtc):
                _, body = self.get("/client/api/sleep-test", "POST", "{}")
            at = json.loads(body)["at"]
            self.assertEqual(ac.power(quiet, run, rtc), 0)
            self.assertEqual(ran[-1], "poweroff")
            self.assertEqual(int(rtc.read_text()), round(at + 180))
            self.assertEqual(ac.read_json(ac.POWER_STATE / "state.json")["wake"], round(at + 180))
            # A time to switch on that isn't right (in the past, or days away): it stays on
            for wake in (time.time() - 60, time.time() + 5 * 86400):
                (folder / "sleep-now").write_text(f"{wake:.0f}\n")
                self.assertEqual(ac.power(quiet, run, rtc), 1)
            self.assertIn("failed", ac.read_json(ac.POWER_STATE / "state.json"))
            # A computer whose clock can't switch it on: it stays on
            (folder / "sleep-now").write_text(f"{time.time() + 3600:.0f}\n")
            self.assertEqual(ac.power(quiet, run, Path(self.tmp.name) / "no-such-alarm"), 1)
            self.assertEqual(len(ran), 3)
        with mock.patch.object(ac.os, "geteuid", return_value=1000):   # not root: refused
            self.get("/client/api/shutdown", "POST", "{}")
            self.assertEqual(ac.power(quiet, run, rtc), 1)
        self.assertEqual(len(ran), 3)
        # A phone can restart the TV's computer too (signed in: see the phone test)
        self.assertEqual(self.phone("/client/api/restart", {})[0].status, 401)

    def test_night_times(self):
        st = {"now": "2026-10-25T23:10:00+00:00", "timezone": "Europe/London",
              "today": [{"name": "fajr", "time": "05:30"}, {"name": "isha", "time": "18:30"}],
              "tomorrow": {"rows": [{"name": "fajr", "time": "05:32"}]}}
        t = lambda s: ac.datetime.fromisoformat(s).timestamp()
        # Evening: tonight's, from Isha + 1 hour to tomorrow's Fajr - 20 minutes
        self.assertEqual(ac.night_window(st, t("2026-10-25T19:00:00+00:00"), 60, 20),
                         (t("2026-10-25T19:30:00+00:00"), t("2026-10-26T05:12:00+00:00")))
        # After midnight (the times were read the evening before): the same night, still under way
        self.assertEqual(ac.night_window(st, t("2026-10-26T03:00:00+00:00"), 60, 20)[1], t("2026-10-26T05:12:00+00:00"))
        # Read after midnight, before Fajr: the night that began yesterday evening
        early = {**st, "now": "2026-10-26T02:00:00+00:00", "tomorrow": None}
        self.assertEqual(ac.night_window(early, t("2026-10-26T02:00:00+00:00"), 60, 20),
                         (t("2026-10-25T19:30:00+00:00"), t("2026-10-26T05:10:00+00:00")))
        # British Summer Time ends this night: the times are London's own
        bst = {**st, "now": "2026-10-24T20:00:00+01:00", "tomorrow": {"rows": [{"name": "fajr", "time": "05:30"}]}}
        self.assertEqual(ac.night_window(bst, t("2026-10-24T20:00:00+01:00"), 60, 0)[1], t("2026-10-25T05:30:00+00:00"))
        self.assertIsNone(ac.night_window({}, time.time(), 60, 20))   # no prayer times yet

    def test_night_sleep(self):
        night = ac.Night(ac.Seen())
        now = ac.datetime.fromisoformat("2026-10-25T21:00:00+00:00").timestamp()
        night.times = {"now": "2026-10-25T20:00:00+00:00", "timezone": "UTC",
                       "today": [{"name": "fajr", "time": "05:30"}, {"name": "isha", "time": "18:30"}]}
        c = {"box": self.box_base, "night": {"on": True, "what": "screen", "after_isha": 60, "before_fajr": 20}}
        self.assertTrue(night.screen_off(c, now))
        self.assertFalse(night.screen_off(c, now - 7200))   # 19:00: not yet
        self.assertFalse(night.screen_off({**c, "night": {**c["night"], "on": False}}, now))
        # Something plays now, or next within a minute and a half: the screen wakes for it
        night.seen.update({"playing": None, "next": {"at": (now + 60) * 1000}}, "", None)
        self.assertFalse(night.screen_off(c, now))
        night.seen.update({"playing": None, "next": None}, "", None)
        self.assertTrue(night.screen_off(c, now))
        night.wake()   # a key: awake for a while
        self.assertFalse(night.screen_off(c, time.time()))
        # The whole computer: it sleeps instead, so the screen stays on (it was switched on by hand)
        night.woken = 0.0
        comp = {**c, "night": {**c["night"], "what": "computer"}}
        with mock.patch.object(ac, "RTC_ALARM", Path(self.tmp.name)):
            self.assertFalse(night.screen_off(comp, now))
        # ...unless it can't switch itself back on: then the screen sleeps
        with mock.patch.object(ac, "RTC_ALARM", Path(self.tmp.name) / "none"):
            self.assertTrue(night.screen_off(comp, now))
        # As its night begins, the client asks for the computer to sleep until Fajr - 20 minutes, once
        ac.save_config(comp)
        sleep_now = ac.CONFIG.parent / "sleep-now"
        sleep_now.unlink(missing_ok=True)
        ac.night_file().unlink(missing_ok=True)
        begins = ac.datetime.fromisoformat("2026-10-25T19:31:00+00:00").timestamp()
        with mock.patch.object(ac, "RTC_ALARM", Path(self.tmp.name)):
            night.tick(begins)
            self.assertEqual(int(sleep_now.read_text()), int(ac.datetime.fromisoformat("2026-10-26T05:10:00+00:00").timestamp()))
            sleep_now.unlink()
            night.tick(begins + 60)   # switched back on by hand: it doesn't sleep again that night
            self.assertFalse(sleep_now.exists())
            ac.night_file().unlink()
            night.tick(begins + 3600)   # an hour in: too late to start
            self.assertFalse(sleep_now.exists())

    def test_sound_for_each_prayer(self):
        tv = lambda: json.loads(self.get("/api/tv?id=x&sound=1")[1])
        azan = {"seq": 7, "label": "Fajr azan", "started": 1000, "sound": {"id": "a1", "url": "/tv/sound/a1"}}
        FakeBox.tv = {"now": 1, "volume": 0.3, "playing": azan, "next": {"at": 2, "label": "Isha azan", "sound": {"id": "a2"}}}
        self.assertEqual((tv()["volume"], tv()["playing"]["sound"]["id"]), (0.6, "a1"))
        self.get("/client/api/settings", "POST", json.dumps({"prayer_sounds": {"fajr": "quiet", "isha": "chime", "asr": "loud"}}))
        st = json.loads(self.get("/client/api/status")[1])
        self.assertEqual(st["prayer_sounds"], {"fajr": "quiet", "dhuhr": "azan", "asr": "azan", "maghrib": "azan", "isha": "chime"})
        j = tv()
        self.assertEqual(j["volume"], 0)                                 # Fajr: quiet here
        self.assertEqual(j["next"]["sound"], ac.CHIME_SOUND)            # Isha: the chime, downloaded early
        FakeBox.tv["playing"] = {**azan, "label": "Fajr reminder"}
        self.assertEqual(tv()["volume"], 0)                              # a quiet prayer's reminder too
        FakeBox.tv["playing"] = {**azan, "label": "Isha azan"}
        self.assertEqual((tv()["volume"], tv()["playing"]["sound"]), (0.6, ac.CHIME_SOUND))
        FakeBox.tv["playing"] = {**azan, "label": "Isha reminder"}
        self.assertEqual(tv()["playing"]["sound"]["id"], "a1")          # a chime prayer's reminder as it is
        FakeBox.tv["playing"] = {**azan, "label": "Suhoor ends"}
        self.assertEqual((tv()["volume"], tv()["playing"]["sound"]["id"]), (0.6, "a1"))   # Ramadan's as they are
        # The chime itself: a short WAV
        r, body = self.get("/client/chime.wav")
        self.assertEqual((r.status, r.getheader("Content-Type"), body[:4]), (200, "audio/wav", b"RIFF"))
        self.assertLess(len(body), 200_000)
        # The last sound, for Screen settings: and whether it played here
        FakeBox.tv["playing"] = {**azan, "seq": 9, "label": "Dhuhr azan"}
        self.get("/api/tv?id=x&sound=1&playing=")
        self.assertFalse(ac.SEEN.last_sound()["played"])
        self.get("/api/tv?id=x&sound=1&playing=9")
        self.assertEqual((ac.SEEN.last_sound()["label"], ac.SEEN.last_sound()["played"]), ("Dhuhr azan", True))

    def test_picture(self):
        self.get("/client/api/settings", "POST", json.dumps({"picture": {"rotate": 45, "margin": 30, "text": 130}}))
        st = json.loads(self.get("/client/api/status")[1])
        self.assertEqual(st["picture"], {"rotate": 0, "margin": 10, "text": 130})   # 45 isn't a way to turn it; 10 % at most
        # The text size: a share of AthanBox's own
        self.assertEqual(json.loads(self.get("/api/state")[1])["look"]["scale"], 156)
        # Kept off the edges: the prayer times go inside the wall, which leaves the margin
        r, _ = self.get("/")
        self.assertEqual(r.getheader("Location"), "/client/wall")
        self.get("/client/api/settings", "POST", json.dumps({"picture": {"margin": 0, "rotate": 90}}))
        r, _ = self.get("/")
        self.assertTrue(r.getheader("Location").startswith("/tv?"))
        self.assertEqual(json.loads(self.get("/client/api/display")[1]), {"rotate": 90})
        self.assertEqual(self.phone("/client/api/display")[0].status, 404)   # the kiosk's own

    def test_screens_turned(self):
        wanted = iter([{"rotate": 90}, {"rotate": 90}, {}, {"rotate": 0}, {"rotate": 0}])
        turns, restarts = [], []
        ac.time.sleep, sleep = (lambda t: None), ac.time.sleep
        try:
            ac.watch_screens(every=0, read=lambda: WAYLAND_INFO, restart=lambda: restarts.append(1), rounds=5,
                             wanted=lambda: next(wanted),
                             rotate=lambda outs, d: turns.append(d) or "no wlr-output-management")
        finally:
            ac.time.sleep = sleep
        self.assertEqual(turns, [90, 0])        # once each, not every round, and not while the client is quiet
        self.assertEqual(restarts, [])
        self.assertEqual(ac.read_json(ac.display_file())["rotate_error"], "no wlr-output-management")

    def test_this_computer(self):
        h = json.loads(self.get("/client/api/health")[1])
        for k in ("addresses", "temperature", "storage", "memory", "up", "last"):
            self.assertIn(k, h)
        st = json.loads(self.get("/client/api/status")[1])
        self.assertTrue(st["show_ip"])
        self.get("/client/api/settings", "POST", json.dumps({"show_ip": False}))
        self.assertFalse(json.loads(self.get("/client/api/status")[1])["show_ip"])

    def test_wifi(self):
        # WPA's key, as wpa_passphrase works it out (IEEE 802.11i's test vector)
        self.assertEqual(ac.wifi_psk(b"IEEE", "password"), "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")
        self.assertEqual(ac.wifi_psk(b"x", "AB" * 32), "ab" * 32)
        self.assertIsNone(ac.wifi_password_problem("12345678"))
        self.assertIsNotNone(ac.wifi_password_problem("short"))
        iw = ("BSS 00:11:22:33:44:55(on wlan0) -- associated\n\tsignal: -40.00 dBm\n\tSSID: Mosque Hall\n\tRSN:\t * Version: 1\n"
              "BSS 00:11:22:33:44:66(on wlan0)\n\tsignal: -70.00 dBm\n\tSSID: Mosque Hall\n\tRSN:\t * Version: 1\n"
              "BSS 00:11:22:33:44:77(on wlan0)\n\tcapability: ESS ShortSlotTime (0x0401)\n\tsignal: -60.00 dBm\n\tSSID: Caf\\xc3\\xa9 \n"
              "BSS 00:11:22:33:44:88(on wlan0)\n\tsignal: -50.00 dBm\n\tSSID: \n")
        self.assertEqual(ac.parse_iw_scan(iw), [
            {"ssid": "Mosque Hall", "signal": 100, "secure": True, "current": True},
            {"ssid": "Café ", "signal": 80, "secure": False, "current": False}])
        nm = "*:Mosque Hall:90:WPA2\n:Office\\:2:50:WPA1 WPA2\n:Guest:30:\n::20:WPA2\n"
        self.assertEqual(ac.parse_nmcli_wifi(nm), [
            {"ssid": "Mosque Hall", "signal": 90, "secure": True, "current": True},
            {"ssid": "Office:2", "signal": 50, "secure": True, "current": False},
            {"ssid": "Guest", "signal": 30, "secure": False, "current": False}])
        key = ac.wifi_psk(b"Hall;1", "password")
        f = ac.nm_keyfile(b"Hall;1", key)
        self.assertIn("ssid=72;97;108;108;59;49;\n", f)   # its bytes: nothing to escape
        self.assertIn(f"psk={key}\n", f)
        net, wpa = ac.ifupdown_files("wlan0", b"Hall;1", key)
        self.assertIn("iface wlan0 inet dhcp", net)
        self.assertIn(f"\tssid={b'Hall;1'.hex()}\n\tscan_ssid=1\n\tpsk={key}\n", wpa)
        self.assertIn("key_mgmt=NONE", ac.ifupdown_files("wlan0", b"Guest", None)[1])
        # ifupdown: only a Wi-Fi that isn't set up in /etc/network/interfaces already
        (ac.ETC / "network").mkdir(parents=True, exist_ok=True)
        interfaces = ac.ETC / "network/interfaces"
        interfaces.write_text("source /etc/network/interfaces.d/*\nauto lo\niface lo inet loopback\nallow-hotplug enp1s0\niface enp1s0 inet dhcp\n")
        self.assertIsNone(ac.ifupdown_problem("wlan0"))
        interfaces.write_text(interfaces.read_text() + "iface wlan0 inet dhcp\n\twpa-ssid Home\n")
        self.assertIn("/etc/network/interfaces", ac.ifupdown_problem("wlan0"))
        # Screen settings asks for it in a file only the client can read (it may hold a password)
        r, body = self.get("/client/api/wifi", "POST", json.dumps({"do": "connect", "ssid": "Hall", "password": "short"}))
        self.assertFalse(json.loads(body)["ok"])
        self.get("/client/api/wifi", "POST", json.dumps({"do": "connect", "ssid": "Hall", "password": "12345678"}))
        ask = ac.CONFIG.parent / "wifi-now"
        self.assertEqual(stat.S_IMODE(ask.stat().st_mode), 0o600)
        self.assertEqual(json.loads(ask.read_text()), {"do": "connect", "ssid": "Hall", "password": "12345678", "secure": True})
        self.assertTrue(json.loads(self.get("/client/api/wifi")[1])["busy"])
        self.assertEqual(self.get("/client/api/wifi", "POST", json.dumps({"do": "rm -rf"}))[0].status, 400)
        ask.unlink()

    def test_updates_come_from_athanbox_releases(self):
        # GitHub serves the packages under the repository's new name; the old one is still accepted
        for url in ("https://github.com/Fudgeamin99/AthanBox/releases/download/client-v0.5.2/azan-sip-client_0.5.2_all.deb",
                    "https://github.com/Fudgeamin99/azan-sip-box/releases/download/client-v0.5.0/azan-sip-client_0.5.0_all.deb"):
            self.assertTrue(url.startswith(ac.OFFICIAL), url)
        for url in ("https://github.com/someone/AthanBox/releases/download/client-v9.9.9/azan-sip-client_9.9.9_all.deb",
                    "https://example.com/Fudgeamin99/AthanBox/releases/download/client-v9.9.9/azan-sip-client_9.9.9_all.deb"):
            self.assertFalse(url.startswith(ac.OFFICIAL), url)

    @unittest.skipUnless(shutil.which("dpkg-deb"), "needs dpkg-deb")
    def test_update_download_is_checked(self):
        pkg = Path(self.tmp.name) / "pkg"
        (pkg / "DEBIAN").mkdir(parents=True, exist_ok=True)
        (pkg / "DEBIAN" / "control").write_text("Package: azan-sip-client\nVersion: 9.9.9\nArchitecture: all\n"
                                                "Maintainer: x <x@example.com>\nDescription: test\n")
        out = Path(self.tmp.name) / "test.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(pkg), str(out)], check=True, capture_output=True)
        FakeGitHub.deb = out.read_bytes()
        rel = ac.latest_release()   # the newest client release, not a Box release, draft or pre-release
        self.assertEqual((rel["version"], rel["tag"]), ("9.9.9", "client-v9.9.9"))
        cache = Path(self.tmp.name) / "cache"
        deb = ac.fetch_deb(rel, cache)
        self.assertEqual(deb.read_bytes(), FakeGitHub.deb)
        with self.assertRaises(ValueError):   # not what GitHub says it is
            ac.fetch_deb({**rel, "digest": "sha256:" + "0" * 64}, cache)
        self.assertFalse(deb.exists())
        with self.assertRaises(ValueError):   # another version than the release's
            ac.fetch_deb({**rel, "version": "9.9.8"}, cache)


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    unittest.main()
