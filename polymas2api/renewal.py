"""Automatic credential renewal.

Measured against the live platform (2026-11-13):

``GET /oauth/token/getToken?userNid=<nid>``
    Returns a **freshly signed** platform token when the request carries a
    valid session. The response is a JWT with the same shape as the
    ``ai-poly`` cookie::

        {"loginType":"login","loginId":"<nid>","rnStr":"<random>",
         "type":"MOCK","userNid":"<nid>","clientid":""}

    The ``rnStr`` and ``clientid`` differ on every call, so each response is
    a distinct credential. The returned token authenticates on its own:
    ``Authorization: <token>`` works against every endpoint the cookie
    worked against.

This is what makes unattended renewal possible without touching the
password, the CAPTCHA or the CAS ticket:

* ``jt-cas`` expires after 24 hours and cannot be renewed this way — it is
  a CAS-issued artifact, and the platform only accepts it while the CAS
  session behind it is alive.
* ``ai-poly`` has no ``exp`` claim at all; the platform decides its life.
  When the platform stops accepting it, a renewal call made **before** the
  expiry produced the replacement that keeps the gateway alive.

So the renewal strategy is: refresh ``ai-poly`` on a schedule, always
keeping the previous token until the new one has been verified, and use the
renewed token as the primary credential from then on.

The renewal cannot save ``jt-cas``: that one needs a fresh CAS ticket,
which in turn needs a real browser login (the智慧树 login page carries a
slider CAPTCHA, 网易易盾 and a phone-code challenge). The gateway therefore
treats ``jt-cas`` as an optional secondary credential — everything works
with ``ai-poly`` alone.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

__all__ = ["TokenRenewer", "RenewalError", "fetch_token"]

_TOKEN_PATH = "/oauth/token/getToken"


class RenewalError(Exception):
    """The platform did not issue a replacement token."""


class TokenRenewer:
    """Renews the platform token and hands the result back.

    The renewer keeps the last known-good token, so a failed renewal never
    replaces a working credential with a broken one.
    """

    def __init__(self, config, logger=None):
        self.cfg = config
        self.log = logger or (lambda message, **_: None)
        self._lock = threading.Lock()
        self._current = config.ai_poly
        self._renewed_at: Optional[float] = None
        self._renewals = 0
        self._failures = 0
        self._last_error = ""
        self._last_token = ""

    # -- state --------------------------------------------------------------
    @property
    def current(self) -> str:
        """The token in use right now."""
        with self._lock:
            return self._current or self.cfg.ai_poly

    @property
    def renewals(self) -> int:
        with self._lock:
            return self._renewals

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "renewals": self._renewals,
                "failures": self._failures,
                "renewed_at": self._renewed_at,
                "renewed_at_local": (time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(self._renewed_at))
                                     if self._renewed_at else ""),
                "last_error": self._last_error,
                "using_renewed": bool(self._renewed_at),
            }

    # -- renewal ------------------------------------------------------------
    def _fetch(self) -> str:
        """Ask the platform for a replacement token."""
        return fetch_token(self.cfg.user_nid, self.current)

    def renew(self, *, verify: Optional[Callable[[str], bool]] = None) -> Optional[str]:
        """Renew the token.

        Returns the new token, or ``None`` when the renewal failed (the
        previous token stays in use).
        """
        try:
            token = self._fetch()
        except RenewalError as error:
            with self._lock:
                self._failures += 1
                self._last_error = str(error)
            self.log(f"token renewal failed: {error}")
            return None
        if verify is not None:
            try:
                if not verify(token):
                    raise RenewalError("the renewed token did not pass verification")
            except RenewalError as error:
                with self._lock:
                    self._failures += 1
                    self._last_error = str(error)
                self.log(f"token renewal rejected: {error}")
                return None
        with self._lock:
            self._current = token
            self._last_token = token
            self._renewed_at = time.time()
            self._renewals += 1
            self._last_error = ""
        self.log(f"token renewed (rnStr={_rn_str(token)})")
        return token

    def apply(self, token: str) -> None:
        """Install a token that was obtained elsewhere."""
        with self._lock:
            self._current = token
            self._renewed_at = time.time()
            self._last_token = token

    def persist(self, path: str) -> bool:
        """Write the renewed token back to the config file."""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(raw, dict):
            return False
        with self._lock:
            if not self._current:
                return False
            raw["ai_poly"] = self._current
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(raw, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            return True
        except OSError:
            return False


def fetch_token(user_nid: str, credential: str) -> str:
    """Ask the platform for a replacement token for one account.

    Free function so the account pool can renew each account with its own
    ``userNid``/``ai_poly`` pair, instead of only the configured one.
    """
    if not user_nid:
        raise RenewalError("renewal needs a userNid")
    if not credential:
        raise RenewalError("renewal needs an existing ai-poly")
    url = f"https://cloudapi.polymas.com{_TOKEN_PATH}?userNid={user_nid}"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    request.add_header("Authorization", credential)
    request.add_header("Cookie", f"ai-poly={credential}")
    request.add_header("route", "/stu-hike/chat")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        raise RenewalError(f"HTTP {error.code} from the token endpoint") from error
    except urllib.error.URLError as error:
        raise RenewalError(f"cannot reach the token endpoint: {error.reason}") from error
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RenewalError(f"the token endpoint returned non-JSON: {body[:120]}") from error
    if payload.get("code") not in (200, "200"):
        raise RenewalError(str(payload.get("msg") or "the platform refused to issue a token"))
    token = payload.get("data")
    if not isinstance(token, str) or len(token) < 40:
        raise RenewalError("the token endpoint returned no usable token")
    return token


def _rn_str(token: str) -> str:
    """The random component of a platform token, for the log line."""
    import base64
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment))
        return str(claims.get("rnStr", "?"))[:12]
    except Exception:  # noqa: BLE001
        return "?"
