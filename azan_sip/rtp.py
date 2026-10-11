"""RTP streaming of G.711 audio (20 ms packets) to unicast or multicast destinations."""
from __future__ import annotations

import logging
import random
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

PTIME_MS = 20
FRAME = 8 * PTIME_MS  # bytes per packet at 8 kHz G.711


def local_ip_for(remote: str = "8.8.8.8") -> str:
    """Find the local interface address used to reach `remote` (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def is_multicast(ip: str) -> bool:
    try:
        first = int(ip.split(".")[0])
    except ValueError:
        return False
    return 224 <= first <= 239


def stream(payload: bytes, dests: list[tuple[str, int]], pt: int, stop: threading.Event | None = None,
           sock: socket.socket | None = None, ttl: int = 4, interface: str | None = None) -> int:
    """Send `payload` as an RTP stream to every destination. Blocks until done. Returns packets sent."""
    own = sock is None
    if own:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        if any(is_multicast(h) for h, _ in dests):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            if interface:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
    stop = stop or threading.Event()
    seq = random.randint(0, 0xFFFF)
    ts = random.randint(0, 0xFFFFFFFF)
    ssrc = random.randint(0, 0xFFFFFFFF)
    filler = payload[-1:] or b"\xff"
    sent = 0
    nxt = time.monotonic()
    try:
        for off in range(0, len(payload), FRAME):
            if stop.is_set():
                break
            chunk = payload[off:off + FRAME]
            if len(chunk) < FRAME:
                chunk += filler * (FRAME - len(chunk))
            marker = 0x80 if sent == 0 else 0
            hdr = struct.pack("!BBHII", 0x80, marker | pt, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
            pkt = hdr + chunk
            for d in dests:
                try:
                    sock.sendto(pkt, d)
                except OSError as exc:
                    log.warning("RTP send to %s:%s failed: %s", d[0], d[1], exc)
            sent += 1
            seq += 1
            ts += FRAME
            nxt += PTIME_MS / 1000.0
            delay = nxt - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            elif delay < -0.5:  # badly behind (machine suspended?) - resync
                nxt = time.monotonic()
    finally:
        if own:
            sock.close()
    return sent
