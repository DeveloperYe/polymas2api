"""Credential freshness: notice an expired cookie before the user does.

The two platform credentials behave differently:

``jt-cas``
    An ES256 JWT issued by the CAS service with a real ``exp`` claim. The
    token decoded on 2026-11-12 carried ``iat`` 1794419368 and ``exp``
    1794505768 — exactly 24 hours. The signature is verified server-side:
    changing any claim (including ``exp``) produces the same 401 as an
    expired token, so the client cannot tell the two apart; the expiry is
    the only reliable signal available locally.

``ai-poly``
    An HS256 JWT with **no** ``exp`` and no ``iat`` — payload is
    ``{"loginType","loginId","rnStr","type","userNid"}``. Its lifetime is
    managed entirely by the server, so it cannot be predicted; it is
    detected by the platform refusing it.

This module therefore does two things: it reports the ``jt-cas`` expiry as
a countdown, and it records the first rejection of either credential so the
panel can say "the cookie expired" instead of a bare 401.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, Optional, Tuple

__all__ = ["CredentialStatus", "decode_jwt", "inspect_credential"]

#: The platform's rejection messages, in the words the service uses.
_REJECTION_MARKERS = ("身份校验失败", "拒绝访问", "未登录", "401", "403")


def decode_jwt(token: str) -> Optional[Dict[str, Any]]:
    """Decode a JWT payload without verifying the signature."""
    if not token or token.count(".") < 2:
        return None
    segment = token.split(".")[1]
    try:
        segment += "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment)
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


class CredentialStatus:
    """The freshness of one credential."""

    def __init__(self, name: str):
        self.name = name
        self.has_exp = False
        self.expires_at: Optional[int] = None
        self.issued_at: Optional[int] = None
        self.rejected = False
        self.rejected_at: Optional[int] = None
        self.last_error = ""

    @property
    def seconds_left(self) -> Optional[int]:
        if self.expires_at is None:
            return None
        return self.expires_at - int(time.time())

    @property
    def state(self) -> str:
        """``ok`` / ``expiring`` / ``expired`` / ``rejected`` / ``opaque``."""
        if self.rejected:
            return "rejected"
        left = self.seconds_left
        if left is None:
            # No expiry claim: the server decides, so the only honest state
            # is "unknown until the server says otherwise".
            return "opaque"
        if left <= 0:
            return "expired"
        if left < 6 * 3600:
            return "expiring"
        return "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "has_exp": self.has_exp,
            "expires_at": self.expires_at,
            "expires_at_local": (time.strftime("%Y-%m-%d %H:%M:%S",
                                             time.localtime(self.expires_at))
                                if self.expires_at else ""),
            "issued_at": self.issued_at,
            "seconds_left": self.seconds_left,
            "rejected": self.rejected,
            "rejected_at": self.rejected_at,
            "last_error": self.last_error,
        }


def inspect_credential(name: str, token: str) -> CredentialStatus:
    """Read one credential's expiry claim, if it has one."""
    status = CredentialStatus(name)
    claims = decode_jwt(token)
    if not claims:
        return status
    exp = claims.get("exp")
    iat = claims.get("iat")
    if isinstance(exp, (int, float)):
        status.has_exp = True
        status.expires_at = int(exp)
    if isinstance(iat, (int, float)):
        status.issued_at = int(iat)
    return status


def looks_rejected(error: str) -> bool:
    """Whether an upstream error says the credential was refused."""
    text = (error or "").lower()
    return any(marker.lower() in text for marker in _REJECTION_MARKERS)
