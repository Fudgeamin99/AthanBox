"""TV screens: the prayer-times display on a TV (an old iPad, a smart TV browser, a mini PC ...)
that also plays the azan out loud, at the same moment as the phones.

A screen opens /tv and asks /api/tv twice a second. The answer says what is playing, when
it started (so the screen joins in at the right place) and what plays next (so it can download
the recording beforehand). The recording itself comes from /tv/sound/<id>:

* WAV, MP3 and M4A recordings are sent as they are, for the best sound on a TV. The screen
  applies the lead-in, the length limit and the repeat itself.
* The chime is made here at TV quality.
* Anything else (or a screen that can't read the file) gets the phone audio as a WAV.
"""
from __future__ import annotations

import collections
import hashlib
import io
import math
import struct
import threading
import time
import wave
from array import array

from . import audio

SEEN_SECONDS = 20          # a screen that asked within this long counts as connected
FORGET_SECONDS = 24 * 3600
MAX_SCREENS = 100
TV_RATE = 24000            # sample rate of the WAVs made here
DIRECT = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac"}


# ------------------------------------------------------------------ connected screens
class Screens:
    def __init__(self):
        self.lock = threading.Lock()
        self.seen: dict[str, dict] = {}

    def touch(self, sid: str, ip: str, name: str, sound: bool, audio_state: str, playing: int | None = None):
        sid = "".join(c for c in str(sid) if c.isalnum() or c in "-_")[:40]
        if not sid:
            return
        now = time.time()
        with self.lock:
            # the same device opened again (say from the Home Screen instead of Safari): forget the old one
            for k in [k for k, v in self.seen.items() if k != sid and v["ip"] == ip and v["name"] == str(name)[:40]
                      and now - v["last"] >= SEEN_SECONDS]:
                del self.seen[k]
            s = self.seen.get(sid) or {"first": now}
            s.update(id=sid, ip=ip, name=str(name)[:40] or "Screen", sound=bool(sound),
                     audio=str(audio_state)[:16], last=now, playing=playing)
            self.seen[sid] = s
            for k in [k for k, v in self.seen.items() if now - v["last"] > FORGET_SECONDS]:
                del self.seen[k]
            while len(self.seen) > MAX_SCREENS:   # a box on the internet: never let the list grow without end
                del self.seen[min(self.seen, key=lambda k: self.seen[k]["last"])]

    def clear(self):
        with self.lock:
            self.seen.clear()

    def listening(self) -> bool:
        """Is any connected screen ready to play the azan out loud?"""
        now = time.time()
        with self.lock:
            return any(now - s["last"] < SEEN_SECONDS and s["sound"] and s["audio"] == "on"
                       for s in self.seen.values())

    def info(self) -> list[dict]:
        now = time.time()
        with self.lock:
            out = []
            for s in sorted(self.seen.values(), key=lambda s: -s["last"]):
                age = now - s["last"]
                if age > 3600:
                    continue
                state = ("away" if age >= SEEN_SECONDS else
                         "muted" if not s["sound"] else
                         "tap" if s["audio"] != "on" else
                         "playing" if s.get("playing") else "ready")
                out.append({"id": s["id"], "name": s["name"], "ip": s["ip"], "state": state,
                            "seen": int(age)})
            return out


# ------------------------------------------------------------------ the recordings
def sound_id(clip) -> str:
    try:
        st = clip.path.stat() if clip.path is not None else None
    except OSError:
        st = None
    key = (f"{clip.source}|{st.st_size if st else 0}|{st.st_mtime_ns if st else 0}|{clip.max_seconds}|"
           f"{clip.repeat_seconds}|{clip.tone_seconds}|{clip.volume_db}|{clip.lead_in_ms}")
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def direct(clip) -> bool:
    """Can the screen be sent the recording as it is?"""
    return clip.path is not None and clip.path.suffix.lower() in DIRECT


def sound_info(clip, sid: str) -> dict:
    """What a screen needs to play a clip. When "raw" the screen adds the lead-in, cuts the
    recording to "max" seconds and repeats it for "repeat" seconds, as the phones get it."""
    raw = direct(clip)
    return {"id": sid, "url": f"/tv/sound/{sid}", "fallback": f"/tv/sound/{sid}?phone=1", "raw": raw,
            "lead": clip.lead_in_ms / 1000 if raw else 0,
            "max": clip.max_seconds if raw else None,
            "repeat": clip.repeat_seconds if raw else None}


def _wav(samples: array, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        if struct.pack("=h", 1) != struct.pack("<h", 1):
            samples = array("h", samples)
            samples.byteswap()
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def _ulaw_table() -> array:
    out = array("h")
    for u in range(256):
        v = ~u & 0xFF
        sign, exp, man = v & 0x80, (v >> 4) & 7, v & 0x0F
        s = (((man << 3) + 0x84) << exp) - 0x84
        out.append(-s if sign else s)
    return out


_ULAW = _ulaw_table()


def phone_wav(clip) -> bytes:
    """The audio exactly as the phones hear it (lead-in, length and repeat included), as a WAV.
    Each sample is repeated so the file is 24 kHz, a rate every browser can open."""
    data = clip.get("PCMU")
    src = array("h", [_ULAW[b] for b in data])
    k = TV_RATE // audio.SAMPLE_RATE
    out = array("h", bytes(2 * len(src) * k))
    for j in range(k):
        out[j::k] = src
    return _wav(out, TV_RATE)


def tone_wav(clip) -> bytes:
    """The test chime at TV quality, with the same lead-in and ending as on the phones."""
    rate, seconds = TV_RATE, clip.tone_seconds
    peak = min(32000.0, 12000.0 * 10 ** (clip.volume_db / 20))
    notes = [659.25, 523.25, 587.33, 392.0]
    seg = int(0.6 * rate)
    lead, tail = int(rate * clip.lead_in_ms / 1000), int(rate * 0.6)
    out = array("h", bytes(2 * lead))
    for i in range(int(seconds * rate)):
        k = (i // seg) % (len(notes) + 1)
        if k == len(notes):
            out.append(0)
            continue
        t = (i % seg) / rate
        # the phones' chime, with a few milliseconds of fade-in so it doesn't click on a big speaker
        env = math.exp(-3.5 * t) * min(1.0, t / 0.004)
        out.append(int(peak * env * math.sin(2 * math.pi * notes[k] * t)))
    out.extend(array("h", bytes(2 * tail)))
    return _wav(out, rate)


class Sounds:
    """The clips screens may ask for (only ones that are playing or coming up), and made WAVs."""

    def __init__(self, keep: int = 16):
        self.lock = threading.Lock()
        self.clips: collections.OrderedDict[str, object] = collections.OrderedDict()
        self.made: collections.OrderedDict[tuple, bytes] = collections.OrderedDict()
        self.keep = keep

    def add(self, clip) -> dict:
        sid = sound_id(clip)
        with self.lock:
            self.clips[sid] = clip
            self.clips.move_to_end(sid)
            while len(self.clips) > self.keep:
                self.clips.popitem(last=False)
        return sound_info(clip, sid)

    def body(self, sid: str, phone: bool = False) -> tuple[str, bytes] | None:
        with self.lock:
            clip = self.clips.get(sid)
        if clip is None:
            return None
        if not phone and direct(clip):
            return DIRECT[clip.path.suffix.lower()], clip.path.read_bytes()
        key = (sid, phone or clip.path is not None)
        with self.lock:
            if key in self.made:
                return "audio/wav", self.made[key]
        body = tone_wav(clip) if clip.path is None and not phone else phone_wav(clip)
        with self.lock:
            self.made[key] = body
            while len(self.made) > 4:
                self.made.popitem(last=False)
        return "audio/wav", body
