"""Crypto prices for the prayer-times screen's Crypto view, from CoinGecko's free API.

AthanBox fetches the prices itself, at most once a minute, and every screen reads them from
/api/crypto, so the screens need no internet of their own and CoinGecko's limits aren't reached.
Which coins and the currency are chosen on the Display page (config [crypto]).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("azan-sip")

API = os.environ.get("AZAN_CRYPTO_URL", "https://api.coingecko.com/api/v3")
UA = "Mozilla/5.0 (compatible; AthanBox; +https://github.com/Fudgeamin99/AthanBox)"
TTL = 60.0              # seconds between fetches
TIMEOUT = 12
MAX_COINS = 8
CURRENCIES = {"gbp": "£", "usd": "$", "eur": "€", "aed": "AED ", "sar": "SAR ", "pkr": "Rs ", "inr": "₹",
              "bdt": "৳", "myr": "RM ", "try": "₺", "cad": "C$", "aud": "A$"}
DEFAULT = {"coins": ["bitcoin", "ethereum"], "currency": "gbp", "button": True}
# Offered on the Display page, with CoinGecko's ids
POPULAR = [("bitcoin", "Bitcoin", "BTC"), ("ethereum", "Ethereum", "ETH"), ("tether", "Tether", "USDT"),
           ("ripple", "XRP", "XRP"), ("binancecoin", "BNB", "BNB"), ("solana", "Solana", "SOL"),
           ("usd-coin", "USDC", "USDC"), ("dogecoin", "Dogecoin", "DOGE"), ("cardano", "Cardano", "ADA"),
           ("tron", "TRON", "TRX"), ("chainlink", "Chainlink", "LINK"), ("avalanche-2", "Avalanche", "AVAX"),
           ("the-open-network", "Toncoin", "TON"), ("litecoin", "Litecoin", "LTC"),
           ("polkadot", "Polkadot", "DOT"), ("shiba-inu", "Shiba Inu", "SHIB")]
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def clean(cfg: dict | None) -> dict:
    """The [crypto] settings, checked: up to MAX_COINS CoinGecko ids, a known currency, and whether
    screens show the Crypto button."""
    c = cfg if isinstance(cfg, dict) else {}
    coins = []
    for x in c.get("coins", DEFAULT["coins"]) if isinstance(c.get("coins", DEFAULT["coins"]), list) else []:
        x = str(x).strip().lower()
        if ID_RE.match(x) and x not in coins:
            coins.append(x)
    if len(coins) > MAX_COINS:
        raise ValueError(f"Choose up to {MAX_COINS} coins for the Crypto view")
    cur = str(c.get("currency") or DEFAULT["currency"]).lower()
    return {"coins": coins, "currency": cur if cur in CURRENCIES else DEFAULT["currency"],
            "button": c.get("button", True) is not False}


def _get(path: str, **params) -> object:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read(2_000_000))


def _spark(prices: list, n: int = 48) -> list[float]:
    """The last 7 days, thinned to n points for a small line."""
    p = [float(x) for x in prices if isinstance(x, (int, float))]
    if len(p) <= n:
        return p
    step = (len(p) - 1) / (n - 1)
    return [p[round(i * step)] for i in range(n)]


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


class Prices:
    def __init__(self):
        self.lock = threading.Lock()
        self.key: tuple | None = None
        self.data: dict | None = None
        self.at = 0.0
        self.error = ""
        self.fetching = False

    def get(self, cfg: dict | None) -> dict:
        c = clean(cfg)
        key = (tuple(c["coins"]), c["currency"])
        base = {"currency": c["currency"], "symbol": CURRENCIES[c["currency"]], "coins": [], "updated": None}
        if not c["coins"]:
            return dict(base, error="No coins are chosen. Choose them on the Display page of the control panel.")
        with self.lock:
            fresh = self.key == key and time.time() - self.at < TTL
            fetch = not fresh and not self.fetching
            if fetch:
                self.fetching = True
        if fetch:
            try:
                rows = _get("coins/markets", vs_currency=c["currency"], ids=",".join(c["coins"]), sparkline="true",
                            price_change_percentage="24h")
                by_id = {r.get("id"): r for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
                coins = []
                for cid in c["coins"]:   # in the order chosen
                    r = by_id.get(cid)
                    if not r:
                        continue
                    coins.append({"id": cid, "name": str(r.get("name") or cid)[:40],
                                  "symbol": str(r.get("symbol") or "").upper()[:12],
                                  "price": _num(r.get("current_price")),
                                  "change": _num(r.get("price_change_percentage_24h")),
                                  "high": _num(r.get("high_24h")), "low": _num(r.get("low_24h")),
                                  "spark": _spark(((r.get("sparkline_in_7d") or {}).get("price")) or [])})
                with self.lock:
                    self.key, self.data, self.at, self.error = key, dict(base, coins=coins, updated=int(time.time())), time.time(), ""
            except Exception as exc:
                msg = f"Couldn't get the prices from CoinGecko ({getattr(exc, 'reason', None) or exc})"
                with self.lock:
                    if self.error != msg:
                        log.warning("Crypto: %s", msg)
                    self.error = msg
                    if self.key != key:
                        self.data = None
                    self.at = time.time() - TTL + 20   # try again in 20 seconds
            finally:
                with self.lock:
                    self.fetching = False
        with self.lock:
            if self.data and self.key == key:   # now: so screens can show "updated 20:11" on AthanBox's clock
                return dict(self.data, error=self.error, stale=bool(self.error), now=int(time.time()))
            return dict(base, error=self.error or "Getting the prices…")

    def search(self, query: str) -> list[dict]:
        """Coins matching a name or symbol, for the Display page."""
        q = str(query or "").strip()[:40]
        if not q:
            return []
        r = _get("search", query=q)
        return [{"id": str(c.get("id")), "name": str(c.get("name"))[:40], "symbol": str(c.get("symbol") or "").upper()[:12],
                 "rank": c.get("market_cap_rank")}
                for c in (r.get("coins") or [])[:12] if isinstance(c, dict) and ID_RE.match(str(c.get("id") or ""))]
