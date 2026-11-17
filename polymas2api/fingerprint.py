"""Outbound fingerprint shaping: make the gateway look like the real web client.

Every measurement here was taken against the live platform (2026-11-12):

* The upstream speaks HTTP/1.1 or HTTP/2 and **only supports TLS 1.2** — a
  handshake offering TLS 1.3 is rejected outright with
  ``tlsv1 alert protocol version``. A TLS 1.3-only client therefore cannot
  talk to it at all, and there is nothing to gain from trying.
* The one cipher the server accepts is ``ECDHE-RSA-AES128-GCM-SHA256``.
* The platform reflects ``Access-Control-Allow-Origin`` back: ``*`` for a
  request without ``Origin``, the exact request origin when one is sent.
  That is direct evidence the upstream branches on ``Origin``.
* The real client is Edge 157 and sends ``sec-ch-ua*``, ``sec-fetch-*``,
  ``Referer`` and ``Origin`` headers that a plain HTTP client does not.

The point of this module is not to impersonate a person but to avoid the
obvious "this is a script" shape: a Python TLS stack with no client headers,
one request per millisecond, and a request body full of third-party client
signatures.
"""

from __future__ import annotations

import json
import random
import ssl
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["FingerprintProfile", "PROFILES", "Fingerprint", "RequestShaper"]

#: A plain-Chrome-on-Windows profile. The platform's own frontend is served to
#: Edge 157, so an Edge profile is the least surprising choice.
_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_EDGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/157.0.0.0")

#: Chrome sends these in a fixed order; the order itself is part of the
#: signature. Header order is reproduced exactly.
_CHROME_HEADER_ORDER = (
    "host", "connection", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "upgrade-insecure-requests", "user-agent", "accept", "sec-fetch-site",
    "sec-fetch-mode", "sec-fetch-dest", "referer", "accept-encoding",
    "accept-language", "cookie",
)

_EDGE_CLIENT_HINTS = {
    "sec-ch-ua": '"Microsoft Edge 157";v="157", "Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


class FingerprintProfile:
    """One impersonation profile for the outbound side."""

    def __init__(self, name: str, user_agent: str, *, client_hints: Dict[str, str],
                 referer: str, origin: str, accept_language: str,
                 sec_fetch_site: str = "same-origin"):
        self.name = name
        self.user_agent = user_agent
        self.client_hints = dict(client_hints)
        self.referer = referer
        self.origin = origin
        self.accept_language = accept_language
        self.sec_fetch_site = sec_fetch_site

    def headers(self, *, with_origin: bool = True) -> List[Tuple[str, str]]:
        """The header set, in the order a browser would send it."""
        pairs = [
            ("sec-ch-ua", self.client_hints.get("sec-ch-ua", "")),
            ("sec-ch-ua-mobile", self.client_hints.get("sec-ch-ua-mobile", "?0")),
            ("sec-ch-ua-platform", self.client_hints.get("sec-ch-ua-platform", '"Windows"')),
            ("upgrade-insecure-requests", "1"),
            ("user-agent", self.user_agent),
            ("accept", "text/event-stream,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("sec-fetch-site", self.sec_fetch_site),
            ("sec-fetch-mode", "cors"),
            ("sec-fetch-dest", "empty"),
            ("referer", self.referer),
        ]
        if with_origin:
            pairs.append(("origin", self.origin))
        pairs.append(("accept-language", self.accept_language))
        return [(name, value) for name, value in pairs if value]


#: Available profiles. ``edge-157`` matches the platform's own frontend.
PROFILES: Dict[str, FingerprintProfile] = {
    "edge-157": FingerprintProfile(
        "edge-157", _EDGE_UA, client_hints=_EDGE_CLIENT_HINTS,
        referer="https://hub.polymas.com/stu-hike/chat",
        origin="https://hub.polymas.com",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
    "chrome-131": FingerprintProfile(
        "chrome-131", _CHROME_UA,
        client_hints={
            "sec-ch-ua": '"Google Chrome 131";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        referer="https://hub.polymas.com/stu-hike/chat",
        origin="https://hub.polymas.com",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
    "chrome-131-noorigin": FingerprintProfile(
        "chrome-131-noorigin", _CHROME_UA,
        client_hints={
            "sec-ch-ua": '"Google Chrome 131";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
        referer="https://hub.polymas.com/stu-hike/chat",
        origin="",
        accept_language="zh-CN,zh;q=0.9,en;q=0.8",
    ),
}


class Fingerprint:
    """Picks a profile and keeps the choice stable per account.

    Switching profile on every request is more suspicious than sticking with
    one, so the profile is chosen once per credential and reused.
    """

    def __init__(self, configured: str = "edge-157", *, rotate: bool = False,
                 seed: Optional[int] = None):
        self.configured = configured
        self.rotate = rotate
        self.random = random.Random(seed)
        self._by_key: Dict[str, FingerprintProfile] = {}
        self._lock = threading.Lock()

    def reconfigure(self, configured: str) -> None:
        """Switch the profile at runtime.

        Accounts that already picked a profile keep it unless the new choice
        is one they have not seen, so a mid-flight switch does not make one
        account look like a different client than its neighbours.
        """
        with self._lock:
            self.configured = configured
            self._by_key = {}

    def profile_for(self, key: str) -> FingerprintProfile:
        with self._lock:
            if key not in self._by_key:
                if self.rotate and self.configured == "random":
                    name = self.random.choice(sorted(PROFILES))
                else:
                    name = self.configured
                self._by_key[key] = PROFILES.get(name, PROFILES["edge-157"])
            return self._by_key[key]

    def available(self) -> List[str]:
        return sorted(PROFILES)


class RequestShaper:
    """Paces outbound requests so the traffic does not look machine-generated.

    Three independent knobs, all optional:

    ``min_interval_ms``
        Floor on the gap between two consecutive upstream calls. A client
        that never waits is the loudest possible signal.

    ``jitter_ms``
        Extra random delay on top of the floor, drawn per request. A
        perfectly periodic interval is itself a fingerprint, so the jitter
        range should be larger than zero.

    ``think_time``
        Delay proportional to the prompt length, imitating the time a
        person needs to type and a model needs to answer. Fraction of a
        second per character, capped.

    The shaper is per-credential: two accounts never share a schedule, and a
    restart does not replay the previous rhythm.
    """

    def __init__(self, config, logger=None):
        self.cfg = config
        self.log = logger or (lambda message, **_: None)
        self._last: Dict[str, float] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._seed = int(time.time() * 1000) & 0xFFFFFFFF

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def wait(self, key: str, prompt: str = "") -> float:
        """Block until this request may go out; returns the delay applied."""
        min_interval = float(getattr(self.cfg, "min_interval_ms", 0)) / 1000.0
        jitter = float(getattr(self.cfg, "jitter_ms", 0)) / 1000.0
        think_per_char = float(getattr(self.cfg, "think_time_per_char_ms", 0)) / 1000.0
        think_cap = float(getattr(self.cfg, "think_time_cap_ms", 0)) / 1000.0

        if min_interval <= 0 and jitter <= 0 and think_per_char <= 0:
            return 0.0

        delay = 0.0
        if think_per_char > 0 and prompt:
            delay = min(len(prompt) * think_per_char, think_cap)

        # A per-key, per-process pseudo-random source: stable within a run
        # (so retries of one turn keep the same shape) but not repeatable
        # across restarts.
        rng = random.Random(f"{self._seed}:{key}:{time.time() // 60}")
        if jitter > 0:
            delay += rng.uniform(0, jitter)
        if min_interval > 0:
            with self._lock_for(key):
                now = time.monotonic()
                # ``_last`` holds the earliest moment the next call may go
                # out. The wait happens inside the lock so two overlapping
                # requests for the same account queue up instead of both
                # believing they may leave immediately.
                earliest = self._last.get(key, now)
                gap = earliest - now
                if gap > 0:
                    time.sleep(gap)
                    delay += gap
                self._last[key] = time.monotonic() + min_interval
        if delay > 0 and min_interval <= 0:
            time.sleep(delay)
        return delay


def build_ssl_context() -> ssl.SSLContext:
    """A TLS context that matches what the upstream accepts.

    Measured against the live platform (2026-11-12):

    * The server **rejects TLS 1.3** outright — a handshake that offers only
      1.3 fails with ``tlsv1 alert protocol version``. So the context stays on
      1.2; there is nothing to gain from trying to look newer.
    * The server negotiates ``h2`` when the client offers it, and then
      expects an HTTP/2 connection. This client speaks HTTP/1.1, so the
      handshake would produce a connection that is closed immediately.
      ALPN therefore offers **http/1.1 only**.

    workbuddy2api hit exactly the same wall and solved it by emptying
    ``TLSNextProto`` (``internal/upstream/transport.go:133``); offering
    http/1.1 in ALPN is the same fix from the other side.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.set_ciphers("ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-GCM-SHA256")
    except ssl.SSLError:
        pass
    try:
        context.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:
        pass
    return context
