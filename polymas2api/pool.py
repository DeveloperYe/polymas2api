"""Credential pool: several platform accounts, rotated so the gateway does not
look like a single client hammering one assistant.

Two things motivate this, both measured against the platform:

* The assistant keeps **long-term memory per (user, assistant)** pair, so
  more than one account means more than one memory context.
* Traffic from one cookie at a fixed rhythm is the shape of a script. The
  pool spreads calls across accounts and the shaper jitters each one.

Credentials live in a JSON file so they can be edited from the panel without
restarting the service.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

__all__ = ["Credential", "CredentialPool", "PoolError"]


class PoolError(Exception):
    """The pool cannot serve a request."""


#: Error texts that mean the credential itself is no longer usable, as
#: opposed to a transient network or platform problem.
_CREDENTIAL_FAILURE_MARKERS = (
    "身份校验失败",      # the session cookie was rejected
    "拒绝访问",          # not logged in
    "未登录",
    "401",
    "403",
)


def _is_credential_failure(error: str) -> bool:
    """Whether the error says the credential is bad rather than the call failed."""
    text = (error or "").lower()
    return any(marker.lower() in text for marker in _CREDENTIAL_FAILURE_MARKERS)


class Credential:
    """One platform account."""

    def __init__(self, uid: str, *, user_nid: str, friend_nid: str,
                 ai_poly: str = "", jt_cas: str = "", app_code: str = "",
                 model: str = "", label: str = "", enabled: bool = True,
                 weight: int = 1, renewed_at: Optional[float] = None,
                 renew_count: int = 0, renew_error: str = ""):
        self.uid = uid
        self.user_nid = user_nid
        self.friend_nid = friend_nid
        self.ai_poly = ai_poly
        self.jt_cas = jt_cas
        self.app_code = app_code or friend_nid
        self.model = model
        #: A free-form note the operator writes; empty means "no label", which
        #: the panel shows as "—" rather than repeating the uid that is already
        #: in the next column.
        self.label = label
        self.enabled = enabled
        self.weight = max(1, min(int(weight), 100))
        self.created_at = time.time()
        self.last_used: Optional[float] = None
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.disabled_reason = ""
        #: Renewal bookkeeping, per account (2026-11-18). The configured
        #: ``default`` account used to be the only one renewed; these let the
        #: panel show the same information for every pooled account.
        self.renewed_at: Optional[float] = renewed_at
        self.renew_count = renew_count
        self.renew_error = renew_error

    @property
    def credential(self) -> str:
        return self.ai_poly or self.jt_cas

    def to_public(self) -> Dict[str, Any]:
        """The account summary the panel shows — never the secrets."""
        return {
            "uid": self.uid,
            "label": self.label,
            "user_nid": self.user_nid,
            "friend_nid": self.friend_nid,
            "app_code": self.app_code,
            "enabled": self.enabled,
            "weight": self.weight,
            "calls": self.calls,
            "failures": self.failures,
            "last_used": self.last_used,
            "last_error": self.last_error,
            "disabled_reason": self.disabled_reason,
            "has_ai_poly": bool(self.ai_poly),
            "has_jt_cas": bool(self.jt_cas),
            "renewed_at": self.renewed_at,
            "renew_count": self.renew_count,
            "renew_error": self.renew_error,
        }

    @classmethod
    def from_dict(cls, uid: str, raw: Dict[str, Any]) -> "Credential":
        return cls(
            uid,
            user_nid=str(raw.get("user_nid") or ""),
            friend_nid=str(raw.get("friend_nid") or ""),
            ai_poly=str(raw.get("ai_poly") or ""),
            jt_cas=str(raw.get("jt_cas") or ""),
            app_code=str(raw.get("app_code") or ""),
            model=str(raw.get("model") or ""),
            label=str(raw.get("label") or ""),
            enabled=bool(raw.get("enabled", True)),
            weight=int(raw.get("weight") or 1),
            renewed_at=(float(raw["renewed_at"])
                        if raw.get("renewed_at") else None),
            renew_count=int(raw.get("renew_count") or 0),
            renew_error=str(raw.get("renew_error") or ""),
        )


class CredentialPool:
    """Thread-safe pool with weighted round-robin and failure quarantine."""

    def __init__(self, config, logger=None):
        self.cfg = config
        self.log = logger or (lambda message, **_: None)
        self._lock = threading.Lock()
        self._accounts: Dict[str, Credential] = {}
        self._cursor = 0
        self._quarantine: Dict[str, float] = {}
        #: Running credit per account for smooth weighted round-robin. Not
        #: persisted: it is a scheduling detail, not account state.
        self._credit: Dict[str, float] = {}
        self._path: Optional[Path] = None
        #: Empty on construction. The gateway loads ``accounts.json`` first and
        #: only falls back to seeding from ``config.json`` when that file gave
        #: nothing — otherwise deleting an account would be undone by the next
        #: restart, because the old credential is still in the config.
        self._accounts = {}

    # -- persistence --------------------------------------------------------
    def load_from_config(self) -> None:
        """Seed the pool from credentials left inline in ``config.json``.

        ``accounts.json`` is the single source of truth; this only exists so a
        configuration written before that file existed still boots. The seeded
        account is an ordinary one — same uid rules, deletable, saved like any
        other — and :meth:`Gateway._migrate_legacy_account` writes it to the
        accounts file on first start.

        With no credential in the config the pool starts **empty**, which is
        what a fresh install looks like: the panel then asks for the first
        account instead of shipping a privileged row nobody can delete.
        """
        ai_poly = self.cfg.ai_poly or self.cfg.jt_cas
        if not ai_poly or not self.cfg.user_nid:
            with self._lock:
                self._accounts = {}
                self._credit.clear()
                self._quarantine.clear()
            return
        account = Credential(
            str(self.cfg.user_nid),
            user_nid=self.cfg.user_nid,
            friend_nid=self.cfg.friend_nid,
            ai_poly=self.cfg.ai_poly,
            jt_cas=self.cfg.jt_cas,
            app_code=self.cfg.app_code or self.cfg.friend_nid,
            model=self.cfg.model,
            label="",
        )
        with self._lock:
            self._accounts = {account.uid: account}
            self._credit.clear()
            self._quarantine.clear()

    @property
    def accounts_path(self) -> Optional[Path]:
        value = self.cfg.extra.get("accounts_file")
        if not value:
            return None
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path(self.cfg.extra.get("_base_dir", ".")) / candidate
        return candidate

    def load_accounts(self) -> int:
        """Load extra accounts from the accounts file; returns the count."""
        path = self.accounts_path
        if path is None or not path.exists():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self.log(f"cannot read the accounts file: {error}")
            return 0
        entries = raw.get("accounts") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return 0
        loaded = 0
        with self._lock:
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                uid = str(entry.get("uid") or f"acc-{index + 1}")
                account = Credential.from_dict(uid, entry)
                if not account.credential or not account.user_nid or not account.friend_nid:
                    continue
                self._accounts[uid] = account
                loaded += 1
        self.log(f"loaded {loaded} account(s) from the accounts file")
        return loaded

    def save_accounts(self) -> bool:
        """Write the accounts back to the accounts file."""
        path = self.accounts_path
        if path is None:
            return False
        with self._lock:
            # Every account is written now that ``accounts.json`` is the only
            # source of truth. (Before, the config-backed one was skipped to
            # avoid duplicating its secret into a second file.)
            payload = {
                "accounts": [
                    {
                        "uid": account.uid,
                        "label": account.label,
                        "user_nid": account.user_nid,
                        "friend_nid": account.friend_nid,
                        "app_code": account.app_code,
                        "ai_poly": account.ai_poly,
                        "jt_cas": account.jt_cas,
                        "enabled": account.enabled,
                        "weight": account.weight,
                        "renewed_at": account.renewed_at,
                        "renew_count": account.renew_count,
                        "renew_error": account.renew_error,
                    }
                    for account in self._accounts.values()
                ]
            }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            from .config import _restrict
            _restrict(path)
            return True
        except OSError as error:
            self.log(f"cannot write the accounts file: {error}")
            return False

    # -- selection ----------------------------------------------------------
    def _candidates(self) -> List[Credential]:
        now = time.time()
        usable = []
        for account in self._accounts.values():
            if not account.enabled:
                continue
            until = self._quarantine.get(account.uid)
            if until is not None:
                if until > now:
                    continue
                self._quarantine.pop(account.uid, None)
            usable.append(account)
        return usable

    def acquire(self, model: str = "") -> Credential:
        """Pick the next account for a request.

        When every account is quarantined but at least one is enabled, the
        least-recently-used one is returned anyway: a five-minute outage is
        worse than retrying a credential that may have been fine all along,
        and the platform's own error messages are what tell the two apart.
        """
        with self._lock:
            usable = self._candidates()
            if not usable:
                fallback = [a for a in self._accounts.values()
                            if a.enabled and not self._quarantine.get(a.uid)]
                if not fallback:
                    fallback = [a for a in self._accounts.values() if a.enabled]
                if not fallback:
                    raise PoolError("no enabled account is available")
                # Least recently used, so the load spreads out.
                fallback.sort(key=lambda a: (a.last_used or 0.0))
                account = fallback[0]
                account.last_used = time.time()
                account.calls += 1
                return account
            # Smooth weighted round-robin (the nginx algorithm): every account
            # accumulates its weight, the highest credit wins and then pays the
            # total. Expanding a list by weight instead — what this used to do
            # — gives the right proportion but in bursts: with 3:1:1 the first
            # three requests all landed on the same account, which defeats the
            # point of spreading load.
            total = sum(a.weight for a in usable)
            for account in usable:
                self._credit[account.uid] = (self._credit.get(account.uid, 0.0)
                                             + account.weight)
            # Ties go to the least recently used, so equal weights interleave
            # instead of favouring whichever account happens to be first.
            chosen = max(usable, key=lambda a: (self._credit.get(a.uid, 0.0),
                                                -(a.last_used or 0.0)))
            self._credit[chosen.uid] = self._credit.get(chosen.uid, 0.0) - total
            for uid in list(self._credit):
                if uid not in self._accounts:
                    self._credit.pop(uid, None)
            chosen.last_used = time.time()
            chosen.calls += 1
            return chosen

    def report_success(self, account: Credential) -> None:
        with self._lock:
            self._quarantine.pop(account.uid, None)

    def report_failure(self, account: Credential, error: str, *, cooldown: float = 300.0) -> None:
        """Quarantine an account after a failure so one broken cookie cannot
        take the whole gateway down.

        Only a failure that says "this credential is not usable" quarantines
        the account. A timeout or a platform-side 5xx says nothing about the
        cookie, so it is counted but the account keeps serving — quarantining
        on those would turn a short outage into a full outage.
        """
        with self._lock:
            account.failures += 1
            account.last_error = error[:200]
            if _is_credential_failure(error):
                self._quarantine[account.uid] = time.time() + cooldown
                self.log(f"account {account.uid} failed ({error[:80]}), "
                         f"quarantined for {cooldown:.0f}s")
            else:
                self.log(f"account {account.uid} had a transient failure "
                         f"({error[:80]}), keeping it in rotation")

    def is_quarantined(self, uid: str) -> bool:
        with self._lock:
            until = self._quarantine.get(uid)
            if until is None:
                return False
            if until <= time.time():
                self._quarantine.pop(uid, None)
                return False
            return True

    # -- management ---------------------------------------------------------
    def set_enabled(self, uid: str, enabled: bool) -> bool:
        with self._lock:
            account = self._accounts.get(uid)
            if account is None:
                return False
            account.enabled = enabled
            account.disabled_reason = "" if enabled else "disabled from the panel"
            if enabled:
                self._quarantine.pop(uid, None)
            return True

    def remove(self, uid: str) -> bool:
        """Remove one account.

        Emptying the pool is allowed — a fresh install starts empty, so
        refusing to reach that state would be inconsistent. What an empty pool
        means is a clear ``503 no_account`` until an account is added.
        """
        with self._lock:
            if uid not in self._accounts:
                return False
            del self._accounts[uid]
            self._quarantine.pop(uid, None)
            return True

    def set_label(self, uid: str, label: str) -> bool:
        """Rename an account's note. Empty clears it."""
        with self._lock:
            account = self._accounts.get(uid)
            if account is None:
                return False
            account.label = label.strip()[:60]
            return True

    def set_weight(self, uid: str, weight: int) -> bool:
        """Set one account's share of the traffic (1..100)."""
        with self._lock:
            account = self._accounts.get(uid)
            if account is None:
                return False
            account.weight = max(1, min(int(weight), 100))
            return True

    def shares(self) -> Dict[str, float]:
        """Each usable account's expected share of requests, for the panel."""
        with self._lock:
            usable = self._candidates()
            total = sum(a.weight for a in usable)
            if not total:
                return {}
            return {a.uid: a.weight / total for a in usable}

    def update(self, uid: str, changes: Dict[str, Any]) -> bool:
        with self._lock:
            account = self._accounts.get(uid)
            if account is None:
                return False
            for field in ("label", "model", "ai_poly", "jt_cas", "user_nid",
                          "friend_nid", "app_code"):
                if field in changes and changes[field] is not None:
                    setattr(account, field, str(changes[field]))
            if "weight" in changes and changes["weight"] is not None:
                account.weight = max(1, min(int(changes["weight"]), 100))
            if "enabled" in changes and changes["enabled"] is not None:
                account.enabled = bool(changes["enabled"])
            return True

    def add(self, account: Credential) -> bool:
        with self._lock:
            if account.uid in self._accounts:
                return False
            self._accounts[account.uid] = account
            return True

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [account.to_public() for account in self._accounts.values()]

    def get(self, uid: str) -> Optional[Credential]:
        with self._lock:
            return self._accounts.get(uid)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            accounts = list(self._accounts.values())
            return {
                "total": len(accounts),
                "enabled": sum(1 for a in accounts if a.enabled),
                "quarantined": sum(1 for uid, until in self._quarantine.items() if until > time.time()),
                "calls": sum(a.calls for a in accounts),
                "failures": sum(a.failures for a in accounts),
            }
