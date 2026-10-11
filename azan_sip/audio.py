"""Audio loading and G.711 encoding (8 kHz mono, PCMU or PCMA).

Uses ffmpeg when available (any format: mp3, m4a, wav ...). Without ffmpeg,
WAV files (PCM 8/16-bit, any sample rate, mono or stereo) are still supported.
Converted audio is cached so each file is only converted once.
"""
from __future__ import annotations

import hashlib
import logging
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 8000
CODECS = {"PCMU": 0, "PCMA": 8}  # RTP payload types

CACHE_DIR = Path.home() / ".cache" / "azan-sip"


# ---------------------------------------------------------------- G.711 tables
def _ulaw(sample: int) -> int:
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, CLIP) + BIAS
    exponent = max(((sample >> 7) & 0xFF).bit_length() - 1, 0)
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_SEG_AEND = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _alaw(sample: int) -> int:
    pcm = sample >> 3
    if pcm >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        pcm = -pcm - 1
    seg = next((i for i, end in enumerate(_SEG_AEND) if pcm <= end), 8)
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << 4
    aval |= ((pcm >> 1) if seg < 2 else (pcm >> seg)) & 0x0F
    return aval ^ mask


_TABLES: dict[str, bytes] = {}


def _table(codec: str) -> bytes:
    if codec not in _TABLES:
        fn = _ulaw if codec == "PCMU" else _alaw
        # index = unsigned 16-bit representation of the signed sample
        _TABLES[codec] = bytes(fn(i - 65536 if i >= 32768 else i) for i in range(65536))
    return _TABLES[codec]


def encode_pcm16(samples: list[int], codec: str) -> bytes:
    tbl = _table(codec)
    return bytes(tbl[s & 0xFFFF] for s in samples)


# ------------------------------------------------------------------- loading
def _cache_key(path: Path, codec: str, volume_db: float) -> Path:
    st = path.stat()
    h = hashlib.sha1(f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{codec}|{volume_db}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{path.stem}-{h}.{codec.lower()}"


def load(path: str | Path, codec: str = "PCMU", volume_db: float = 0.0) -> bytes:
    """Return raw G.711 bytes for an audio file (8000 bytes per second)."""
    codec = codec.upper()
    if codec not in CODECS:
        raise ValueError(f"codec must be PCMU or PCMA, not {codec}")
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")
    cache = _cache_key(path, codec, volume_db)
    if cache.exists():
        return cache.read_bytes()

    if shutil.which("ffmpeg"):
        data = _ffmpeg(path, codec, volume_db)
    elif path.suffix.lower() == ".wav":
        data = _wav_python(path, codec, volume_db)
    else:
        raise RuntimeError(f"{path.name}: install ffmpeg to use non-WAV audio files")

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    except OSError as exc:
        log.debug("Could not write cache: %s", exc)
    log.info("Prepared %s (%.0f s, %s)", path.name, len(data) / SAMPLE_RATE, codec)
    return data


def _ffmpeg(path: Path, codec: str, volume_db: float) -> bytes:
    fmt = "mulaw" if codec == "PCMU" else "alaw"
    # Light compression/limiting keeps the recitation intelligible on small phone speakers.
    af = f"highpass=f=200,lowpass=f=3400,volume={volume_db}dB,alimiter=limit=0.95"
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-af", af, "-f", fmt, "-"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on {path}: {res.stderr.decode(errors='replace').strip()}")
    return res.stdout


def _wav_python(path: Path, codec: str, volume_db: float) -> bytes:
    with wave.open(str(path), "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        vals = struct.unpack(f"<{len(raw)//2}h", raw)
    elif width == 1:
        vals = [(b - 128) << 8 for b in raw]
    else:
        raise RuntimeError(f"{path.name}: only 8/16-bit WAV supported without ffmpeg")
    mono = [sum(vals[i:i + ch]) // ch for i in range(0, len(vals), ch)] if ch > 1 else list(vals)
    gain = 10 ** (volume_db / 20)
    if rate != SAMPLE_RATE:
        ratio = rate / SAMPLE_RATE
        out_n = int(len(mono) / ratio)
        res = []
        for i in range(out_n):
            pos = i * ratio
            j = int(pos)
            frac = pos - j
            a = mono[j]
            b = mono[j + 1] if j + 1 < len(mono) else a
            res.append(a + (b - a) * frac)
        mono = res
    samples = [max(-32768, min(32767, int(s * gain))) for s in mono]
    return encode_pcm16(samples, codec)


def tone(seconds: float = 6.0, codec: str = "PCMU", gain_db: float = 0.0) -> bytes:
    """A gentle chime pattern for testing without an azan file."""
    out = []
    peak = min(32000.0, 12000.0 * 10 ** (gain_db / 20))
    notes = [659.25, 523.25, 587.33, 392.0]
    n_total = int(seconds * SAMPLE_RATE)
    seg = int(0.6 * SAMPLE_RATE)
    for i in range(n_total):
        k = (i // seg) % (len(notes) + 1)
        if k == len(notes):
            out.append(0)
            continue
        t = (i % seg) / SAMPLE_RATE
        env = math.exp(-3.5 * t)
        out.append(int(peak * env * math.sin(2 * math.pi * notes[k] * t)))
    return encode_pcm16(out, codec.upper())


def silence(ms: int, codec: str) -> bytes:
    return bytes([0xFF if codec.upper() == "PCMU" else 0xD5]) * int(SAMPLE_RATE * ms / 1000)
