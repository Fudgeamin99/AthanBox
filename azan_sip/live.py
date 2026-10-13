"""Makkah live: relays an IPTV (HLS / .m3u8) stream through Azan SIP.

Screens play /live/stream.m3u8 from Azan SIP itself instead of the TV channel's server:
- it works in every browser (many IPTV servers don't allow web pages to read them directly),
- the box's own screen, TVs and phones on the network can all show it,
- several screens share one download of each video segment.

Only links that Azan SIP wrote into a playlist itself (signed with a random key) are fetched,
so the relay can't be used to open any other address.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import quote, urljoin, urlparse

log = logging.getLogger("azan-sip")

# Saudi Quran TV (Al Quran Al Kareem), broadcast live from Masjid al-Haram in Makkah.
DEFAULT_URL = "https://cdn-globecast.akamaized.net/live/eds/saudi_quran/hls_roku/index.m3u8"
M3U8 = "application/vnd.apple.mpegurl"
UA = "Mozilla/5.0 (compatible; AzanSIPBox; +https://github.com/Fudgeamin99/azan-sip-box)"
PLAYLIST_TTL = 1.0         # seconds; live playlists change every few seconds
SEGMENT_TTL = 120.0
CACHE_LIMIT = 96_000_000   # bytes of video kept in memory for other screens
MAX_PLAYLIST = 2_000_000
MAX_SEGMENT = 40_000_000
TIMEOUT = 12


class StreamError(Exception):
    """A problem reaching or reading the stream, worded for the person using the app."""


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme not in ("http", "https"):
            raise StreamError("The stream redirected somewhere that isn't a web address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_HttpOnlyRedirects())


def check_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("The Makkah live link must start with http:// or https://")
    return url


def is_playlist(body: bytes) -> bool:
    return body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U")


def friendly(exc: Exception) -> str:
    if isinstance(exc, StreamError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return f"The stream server refused the request (HTTP {exc.code}). The link may only work in some countries, or it has changed."
        if exc.code == 404:
            return "The stream wasn't found (HTTP 404). The link has probably changed."
        return f"The stream server returned an error (HTTP {exc.code})."
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return "The stream server didn't answer in time. Check the internet connection."
        return "Couldn't reach the stream server. Check the internet connection."
    if isinstance(exc, TimeoutError):
        return "The stream server didn't answer in time. Check the internet connection."
    return str(exc) or exc.__class__.__name__


class LiveRelay:
    def __init__(self, get_url: Callable[[], str]):
        self.get_url = get_url
        self.key = secrets.token_bytes(32)
        self.lock = threading.Lock()
        self.cache: collections.OrderedDict[str, tuple[float, str, bytes]] = collections.OrderedDict()
        self.cache_bytes = 0
        self.flights: dict[str, threading.Lock] = {}
        self.viewers: dict[str, float] = {}
        self.last_error_log = 0.0

    # ---------------------------------------------------------------- signed links
    def sign(self, url: str) -> str:
        return hmac.new(self.key, url.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def link(self, url: str) -> str:
        return f"/live/x?u={quote(url, safe='')}&k={self.sign(url)}"

    def allowed(self, url: str, sig: str | None) -> bool:
        return (urlparse(url).scheme in ("http", "https")
                and hmac.compare_digest(self.sign(url), sig or ""))

    def rewrite(self, text: str, base: str) -> str:
        """Point every address in a playlist back at this relay."""
        def fix(uri: str) -> str:
            absolute = urljoin(base, uri.strip())
            if urlparse(absolute).scheme not in ("http", "https"):
                return uri       # data: and other odd links are left alone (and won't be fetched)
            return self.link(absolute)

        out = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                out.append(re.sub(r'URI="([^"]*)"', lambda m: f'URI="{fix(m.group(1))}"', s))
            else:
                out.append(fix(s))
        return "\n".join(out) + "\n"

    # ---------------------------------------------------------------- fetching
    def _download(self, url: str, limit: int, timeout: float = TIMEOUT) -> tuple[str, str, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with _opener.open(req, timeout=timeout) as r:
            body = r.read(limit + 1)
            if len(body) > limit:
                raise StreamError("The stream sent more data than expected")
            return r.geturl(), r.headers.get_content_type(), body

    def _cached(self, url: str):
        with self.lock:
            hit = self.cache.get(url)
            if hit and hit[0] > time.monotonic():
                self.cache.move_to_end(url)
                return hit[1], hit[2]
        return None

    def _store(self, url: str, ttl: float, ctype: str, body: bytes):
        with self.lock:
            old = self.cache.pop(url, None)
            if old:
                self.cache_bytes -= len(old[2])
            self.cache[url] = (time.monotonic() + ttl, ctype, body)
            self.cache_bytes += len(body)
            now = time.monotonic()
            for k in [k for k, v in self.cache.items() if v[0] <= now]:
                self.cache_bytes -= len(self.cache.pop(k)[2])
            while self.cache_bytes > CACHE_LIMIT and self.cache:
                _, v = self.cache.popitem(last=False)
                self.cache_bytes -= len(v[2])

    def get(self, url: str) -> tuple[str, bytes]:
        """Return (content type, body) for a playlist or a piece of video, via the cache."""
        hit = self._cached(url)
        if hit:
            return hit
        with self.lock:
            flight = self.flights.setdefault(url, threading.Lock())
        with flight:          # several screens asking for the same piece share one download
            hit = self._cached(url)
            if hit:
                return hit
            try:
                final, ctype, body = self._download(url, MAX_SEGMENT)
            finally:
                with self.lock:
                    self.flights.pop(url, None)
            if is_playlist(body):
                text = body.decode("utf-8", errors="replace")
                ctype, body, ttl = M3U8, self.rewrite(text, final).encode("utf-8"), PLAYLIST_TTL
            else:
                if ctype in ("text/plain", "application/octet-stream", "binary/octet-stream"):
                    path = urlparse(final).path.lower()
                    ctype = ("video/mp2t" if path.endswith(".ts") else
                             "video/mp4" if path.endswith((".mp4", ".m4s", ".m4v")) else
                             "audio/aac" if path.endswith(".aac") else ctype)
                ttl = SEGMENT_TTL
            self._store(url, ttl, ctype, body)
            return ctype, body

    def entry(self, viewer: str) -> tuple[str, bytes]:
        """The playlist screens start from: the configured stream, rewritten."""
        url = self.get_url()
        now = time.time()
        new_viewer = now - self.viewers.get(viewer, 0) > 600
        self.viewers[viewer] = now
        try:
            ctype, body = self.get(url)
        except Exception as exc:
            if now - self.last_error_log > 60:
                self.last_error_log = now
                log.warning("Makkah live: %s", friendly(exc))
            raise
        if ctype != M3U8:
            raise StreamError("The Makkah live link isn't an IPTV (.m3u8) stream")
        if new_viewer:
            log.info("Makkah live started on %s", "the box's own screen" if viewer.startswith("127.") or viewer == "::1"
                     else viewer)
        return ctype, body

    def allows_pages(self, url: str) -> bool:
        """Whether the stream lets a web page from another address read it (CORS), which screens need to
        fetch it themselves instead of through the box."""
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", "Origin": "http://azan.local"})
        try:
            with _opener.open(req, timeout=10) as r:
                return r.headers.get("Access-Control-Allow-Origin", "").strip() in ("*", "http://azan.local")
        except Exception:
            return False

    # ---------------------------------------------------------------- settings check
    def test(self, url: str) -> dict:
        """Check a stream link without playing it. Returns a short summary for Settings."""
        url = check_url(url) or self.get_url()
        try:
            final, _, body = self._download(url, MAX_PLAYLIST, timeout=10)
            if not is_playlist(body):
                raise StreamError("That link isn't an IPTV stream (it should be a .m3u8 playlist)")
            text = body.decode("utf-8", errors="replace")
            qualities, first = [], None
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
                    qualities.append((int(m.group(1)), int(m.group(2))) if m else None)
                    for nxt in lines[i + 1:]:
                        if nxt.strip() and not nxt.startswith("#"):
                            first = first or urljoin(final, nxt.strip())
                            break
            media = text
            if first:                        # a master playlist: check one quality actually plays
                _, _, body2 = self._download(first, MAX_PLAYLIST, timeout=10)
                if not is_playlist(body2):
                    raise StreamError("The stream's playlist is broken")
                media = body2.decode("utf-8", errors="replace")
            if "#EXTINF" not in media:
                raise StreamError("The stream is reachable but isn't sending any video right now")
            live = "#EXT-X-ENDLIST" not in media
            sizes = [q for q in qualities if q]
            best = max(sizes, key=lambda q: q[0] * q[1]) if sizes else None
            parts = ["Working"]
            if len(qualities) > 1:
                parts.append(f"{len(qualities)} qualities" + (f" up to {best[0]}×{best[1]}" if best else ""))
            elif best:
                parts.append(f"{best[0]}×{best[1]}")
            if not live:
                parts.append("recorded, not live")
            return {"working": True, "summary": " · ".join(parts), "live": live, "direct": self.allows_pages(final)}
        except Exception as exc:
            return {"working": False, "summary": friendly(exc)}
