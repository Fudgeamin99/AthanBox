"""Tests for the Azan SIP Client, against a small stand-in for the box:
    python3 -m unittest discover -s client"""
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
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
                          ("/client/api/connect", {"address": "10.0.0.9"}), ("/client/api/chime", {})):
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
        r, body = self.phone("/client/api/settings", {"volume": 30, "remote": False}, cookie=cookie)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(tv()["volume"], 30)
        self.assertTrue(tv()["remote_on"])   # a phone can't switch phones off
        self.assertEqual(tv()["signed_in"], 1)
        before = tv()["chime"]
        self.assertTrue(json.loads(self.phone("/client/api/chime", {}, cookie=cookie)[1])["ok"])
        self.assertGreater(tv()["chime"], before)   # the TV plays it
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
            ac.watch_screens(every=0, read=lambda: next(readings), restart=lambda: restarts.append(len(shown)), rounds=7)
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
