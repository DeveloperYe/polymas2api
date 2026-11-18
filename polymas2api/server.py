"""The HTTP gateway: OpenAI-compatible ``/v1/chat/completions`` plus a panel.

The panel mirrors workbuddy2api's shape — a static page plus ``/panel/api/*``
behind the same Bearer credential — but with the security headers that make
the page safe to expose on a LAN.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import select
import socket
import mimetypes
import threading
import time
import traceback
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import constants as C
from .adapter import OpenAIAdapter
from .credentials import (CredentialStatus, decode_jwt, inspect_credential,
                          looks_rejected)
from .fingerprint import PROFILES, Fingerprint, RequestShaper, build_ssl_context
from .journal import Journal, JournalEntry
from . import agent_persona
from .persona import NameScrubber
from .tokens import usage_for
from .toolbridge import build_tool_prompt, declared_names
from .pool import Credential, CredentialPool, PoolError
from .renewal import RenewalError, TokenRenewer, fetch_token
from .sanitize import Sanitizer
from .upstream import PolymasClient, UpstreamError

__all__ = ["Gateway", "serve"]

#: How long a model catalogue stays usable before it is fetched again.
#: The catalogue changes when the agent or the platform's model list changes —
#: rarely — so an hour is plenty; 「重新获取」 covers everything else.
CATALOGUE_TTL = 3600

WEB_DIR = Path(__file__).parent / "web"

#: Strict CSP, no inline script, so the panel cannot be turned into an XSS
#: gadget. Mirrors workbuddy2api's ``internal/panel/index.go``.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; img-src 'self' data:; form-action 'none'; "
       "frame-ancestors 'none'; base-uri 'none'")

#: Hard ceiling on a request body.
#:
#: Raised from 1 MiB on 2026-11-18, because 1 MiB made a client's own context
#: management unreachable rather than merely tight.
#:
#: A client compacts when its running token count reaches
#: ``thresholdRatio x contextWindow`` (0.8 by default) - 800,000 for a 1M-token
#: window. A real agent prompt is about 15% Chinese and 85% ASCII, measured by
#: solving this gateway's own estimate against a logged 68,438-character
#: request that came to 24,875 tokens, so 800,000 tokens is roughly 2,200,000
#: characters and a 2.7 MB body. 1 MiB carries only ~805,000 characters of
#: that mixture, 37% of the way: every request hit the body cap first and the
#: compaction never ran.
#:
#: 8 MiB carries ~2.7M characters of the same mixture, enough for a whole
#: 1M-token window (2.75M characters, ~3.4 MB), so the platform's own limit
#: stops a request and not ours. Still bounded on purpose: the body is read
#: into memory before parsing.
MAX_BODY_BYTES = 8 << 20


class Logger:
    """Timestamped file+console logger that never raises.

    The file rotates at a size cap so a long-running gateway cannot fill the
    disk; the previous file is kept under a numbered name.
    """

    def __init__(self, log_dir, name: str = "gateway.log", console: bool = True,
                 max_bytes: int = 8 * 1024 * 1024):
        self.path = Path(log_dir) / name
        self.console = console
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def __call__(self, message: str, *, console: Optional[bool] = None) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        if self.console if console is None else console:
            print(line, flush=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    index = 1
                    while True:
                        candidate = self.path.with_name(f"{self.path.stem}.{index}{self.path.suffix}")
                        if not candidate.exists():
                            break
                        index += 1
                        if index > 20:
                            candidate.unlink(missing_ok=True)
                            break
                    self.path.rename(candidate)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass


class LiveConfig:
    """A configuration snapshot that can be replaced atomically.

    The panel edits settings and they take effect without a restart. A
    request that is already running keeps the snapshot it started with,
    because the update builds a **new** object rather than mutating the
    live one.
    """

    def __init__(self, config):
        self._lock = threading.Lock()
        self._config = config

    def get(self):
        with self._lock:
            return self._config

    def update(self, changes: Dict[str, Any]):
        with self._lock:
            current = self._config
            updated = replace(current)
            for key, value in changes.items():
                if hasattr(updated, key):
                    setattr(updated, key, value)
            self._config = updated
            return self._config

    def replace(self, config):
        with self._lock:
            self._config = config


#: Phrases that mean "I already did it". This is the failure the retry exists
#: for: the assistant narrates an action instead of emitting a CALL line, so
#: the tool never runs and the user is told something happened that did not.
#:
#: Retrying on *every* tool-enabled turn was the earlier behaviour and it was
#: wrong: a greeting burned two extra full generations (measured 2026-11-17 —
#: "晚上好" took 17s and produced 19 characters).
_ACTION_CLAIM = (
    # past tense / completed
    "已经", "我已", "已发送", "已发出", "已执行", "已创建", "已删除", "已修改",
    "已更新", "已发给", "已为你", "已帮你", "已处理", "已完成",
    "发出去了", "发过去了", "发给你了", "执行了", "跑完了", "跑好了",
    "处理好了", "弄好了", "搞定了", "做完了", "生成好了", "查好了", "读完了",
    "建好了", "写好了", "改好了",
    # about to / in progress — also a claim the user will act on
    "这就发", "这就执行", "这就去", "马上执行", "正在为你", "正在执行",
)


def _demands_tool(body: Any) -> bool:
    """True when the caller asked for a tool call no matter what.

    ``tool_choice: "required"`` means a text-only answer is a protocol miss,
    so it is always worth one corrective retry.
    """
    if not isinstance(body, dict):
        return False
    choice = body.get("tool_choice", body.get("function_call"))
    if isinstance(choice, dict):
        return True          # a forced function name
    return str(choice or "").strip().lower() == "required"


def _retry_reason_text(text: str, has_call: bool, used: Any = None,
                       force_tool: bool = False) -> str:
    """The streaming twin of ``_retry_reason``: judge the finished text."""
    if has_call:
        return ""
    if force_tool:
        return "tool_choice=required but the answer carried no call"
    if used:
        return ("the agent used its own tools (%s) instead of emitting a CALL"
                % ", ".join(str(name) for name in used if name))
    if not (text or "").strip():
        return "the reply was empty"
    if _claims_action(text or ""):
        return "the answer claims an action it never called"
    return ""


def _claims_action(text: str) -> bool:
    """True when the answer says an action happened instead of calling one."""
    return any(marker in (text or "") for marker in _ACTION_CLAIM)


def _resumes_after_tool(messages: Any) -> bool:
    """True when the last turn is a tool result the client is feeding back.

    Such a request continues an existing exchange rather than starting one:
    the assistant has to read the result and answer, not call the tool again.
    """
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "tool":
            return True
        if role == "user":
            return False
    return False


class Gateway:
    """Holds the configuration and wires the client to the HTTP surface."""

    def __init__(self, cfg, log: Optional[Logger] = None):
        self.live = LiveConfig(cfg)
        self.log = log or Logger(cfg.path("log_dir", "logs"))
        self.pool = CredentialPool(cfg, logger=self.log)
        loaded = self.pool.load_accounts()
        # accounts.json wins. Only a fresh or emptied install falls back to the
        # credential left in config.json, and that one becomes permanent on the
        # spot so the next start reads it from the file like everything else.
        if not loaded or not self.pool.list():
            self.pool.load_from_config()
            self._migrate_legacy_account()
        self.journal = Journal(cfg, logger=self.log)
        self.fingerprint = Fingerprint(cfg.fingerprint)
        self.shaper = RequestShaper(cfg, logger=self.log)
        self.sanitizer = Sanitizer(cfg.sanitize_scope, enabled=cfg.sanitize_enabled)
        self.washer = NameScrubber(cfg.persona_scrub_names)
        self.renewer = TokenRenewer(cfg, logger=self.log)
        self.credentials = self._inspect_credentials()
        self._warn_expiring()
        self._effective_model_cache = ""
        self._effective_model_at = 0.0
        #: The model catalogue costs two upstream requests (the agent's own
        #: rows plus the platform's). The panel polls the overview every five
        #: seconds, so without a cache simply leaving the panel open would mean
        #: ~24 upstream calls a minute — needless load and a very bot-like
        #: traffic pattern. Cached, with a manual refresh.
        self._catalogue_cache: Optional[Dict[str, Any]] = None
        self._catalogue_at = 0.0
        self._renew_timer: Optional[threading.Timer] = None
        self._renew_stop = threading.Event()
        self._schedule_renewal()

    # -- credential renewal -------------------------------------------------
    def _schedule_renewal(self) -> None:
        """Start the background renewal timer.

        The platform token carries no expiry claim, so the interval is a
        precaution rather than a countdown: renewing well before the
        platform would refuse it means the gateway never sees an outage.
        """
        interval = int(getattr(self.cfg, "renew_interval_minutes", 0) or 0)
        if interval <= 0:
            return
        self._renew_stop.clear()

        def tick() -> None:
            if self._renew_stop.is_set():
                return
            try:
                self.renew_now()
            except Exception as error:  # noqa: BLE001
                self.log(f"the renewal tick failed: {error}")
            finally:
                self._renew_timer = threading.Timer(interval * 60, tick)
                self._renew_timer.daemon = True
                self._renew_timer.start()

        self._renew_timer = threading.Timer(interval * 60, tick)
        self._renew_timer.daemon = True
        self._renew_timer.start()
        self.log(f"credential renewal scheduled every {interval} minutes")

    def renew_account(self, account: Credential) -> Dict[str, Any]:
        """Renew one account's token and install it if it verifies.

        Every account has its own ``userNid``/``ai-poly`` pair, so each one
        renews separately: the endpoint is per-user, and a failure belongs to
        that account alone. The previous token is kept whenever the new one
        does not pass verification.
        """
        try:
            token = fetch_token(account.user_nid, account.ai_poly)
        except RenewalError as error:
            account.renew_error = str(error)
            self.log(f"renewal failed for {account.uid}: {error}")
            return {"ok": False, "uid": account.uid, "error": str(error)}

        # Verify against this account's own agent, not the configured one.
        probe = self._account_client(account)
        probe._override_credential = token
        verified = False
        try:
            catalogue = probe.models()
            verified = bool(catalogue.get("models") or catalogue.get("available"))
        except UpstreamError as error:
            account.renew_error = f"验证失败：{error}"
            self.log(f"renewed token for {account.uid} did not verify: {error}")
            return {"ok": False, "uid": account.uid, "error": account.renew_error}
        if not verified:
            account.renew_error = "验证失败：拉不到模型目录"
            return {"ok": False, "uid": account.uid, "error": account.renew_error}

        account.ai_poly = token
        account.app_code = account.app_code or account.friend_nid
        account.renewed_at = time.time()
        account.renew_count += 1
        account.renew_error = ""
        return {"ok": True, "uid": account.uid,
                "renewed_at": account.renewed_at}

    def renew_all(self) -> Dict[str, Any]:
        """Renew every enabled pooled account."""
        results: Dict[str, Dict[str, Any]] = {}
        for item in self.pool.list():
            account = self.pool.get(item["uid"])
            if account is None or not account.enabled:
                continue
            if not account.ai_poly:
                account.renew_error = "没有 ai-poly，无法续期"
                results[account.uid] = {"ok": False, "uid": account.uid,
                                        "error": account.renew_error}
                continue
            results[account.uid] = self.renew_account(account)
        ok = sum(1 for item in results.values() if item.get("ok"))
        failed = len(results) - ok
        if ok:
            # One file, one write: accounts.json is the only source of truth.
            self.pool.save_accounts()
        return {"ok": failed == 0, "ok_count": ok, "failures": failed,
                "accounts": results}

    def renew_now(self) -> bool:
        """Renew all accounts; ``True`` when none of them failed."""
        return bool(self.renew_all().get("ok"))

    def stop_renewal(self) -> None:
        self._renew_stop.set()
        if self._renew_timer is not None:
            self._renew_timer.cancel()
            self._renew_timer = None

    def _inspect_credentials(self) -> Dict[str, CredentialStatus]:
        """Read the expiry claims of the configured credentials."""
        cfg = self.cfg
        # jt-cas is gone: measured 2026-11-18, every path works with an empty
        # jt_cas (catalogue, chat, streaming, tool bridge and the getToken
        # renewal all pass), so there is nothing to report for it. An older
        # accounts file that still carries one is tolerated, it just does not
        # show up anywhere.
        # Report the pool, not config.json: tokens are renewed into the
        # accounts file now, so the config value would go stale the moment a
        # renewal ran.
        statuses: Dict[str, CredentialStatus] = {}
        accounts = [a for a in self.pool.list() if a.get("has_ai_poly")]
        sample = self.pool.get(accounts[0]["uid"]).ai_poly if accounts else (cfg.ai_poly or "")
        if cfg.jt_cas:
            statuses["jt-cas"] = inspect_credential("jt-cas", cfg.jt_cas)
        statuses["ai-poly"] = inspect_credential("ai-poly", sample)
        return statuses

    def _warn_expiring(self) -> None:
        for status in self.credentials.values():
            if status.state == "expired":
                self.log(f"WARNING: the {status.name} credential expired at "
                         f"{status.to_dict()['expires_at_local']} — replace it in config.json")
            elif status.state == "expiring":
                self.log(f"NOTE: the {status.name} credential expires in "
                         f"{status.seconds_left // 3600} h "
                         f"({status.to_dict()['expires_at_local']})")
            elif status.state == "opaque" and status.name == "ai-poly":
                self.log("NOTE: the ai-poly credential carries no expiry claim — "
                         "the platform decides its lifetime, so a rejection is the "
                         "only signal that it has expired")

    def _note_credential_rejection(self, error: str) -> None:
        """Record that the platform refused the credential."""
        if not looks_rejected(error):
            return
        for status in self.credentials.values():
            if not status.rejected:
                status.rejected = True
                status.rejected_at = int(time.time())
                status.last_error = error[:160]
                self.log(f"WARNING: the {status.name} credential was refused by the "
                         f"platform ({error[:60]}) — it has expired or been revoked; "
                         f"replace it in config.json")

    # -- helpers ------------------------------------------------------------
    @property
    def cfg(self):
        return self.live.get()

    def _client(self, account=None) -> PolymasClient:
        return PolymasClient(self.cfg, account, logger=self.log,
                             fingerprint=self.fingerprint, shaper=self.shaper)

    def _acquire(self, model: str = ""):
        """Pick an account and build a client for it.

        The single-account case still goes through ``acquire`` so that a
        quarantined account is honoured; otherwise a bad cookie would be
        retried on every request instead of being set aside.
        """
        cfg = self.cfg
        account = self.pool.acquire(model)
        if account is None:
            raise PoolError("no enabled account is available")
        return account, self._client(account)

    # -- OpenAI surface -----------------------------------------------------
    def models(self, *, refresh: bool = False) -> Dict[str, Any]:
        """``GET /v1/models`` — the agent's catalogue plus the platform's.

        Cached for :data:`CATALOGUE_TTL` seconds; pass ``refresh=True`` (the
        panel's 重新获取 button) to go upstream again immediately.
        """
        now = time.time()
        if (not refresh and self._catalogue_cache is not None
                and now - self._catalogue_at < CATALOGUE_TTL):
            return self._catalogue_cache
        result = self._fetch_catalogue()
        self._catalogue_cache = result
        self._catalogue_at = now
        return result

    def catalogue_age(self) -> int:
        """Seconds since the catalogue was last fetched."""
        if not self._catalogue_at:
            return -1
        return int(time.time() - self._catalogue_at)

    def panel_refresh_models(self) -> Dict[str, Any]:
        """Force a catalogue refresh and report what came back."""
        catalogue = self.models(refresh=True)
        # ``_effective_model`` has its own cache keyed off the same catalogue.
        self._effective_model_cache = ""
        self._effective_model_at = 0.0
        return {"ok": bool(catalogue.get("data")),
                "models": catalogue.get("data") or [],
                "agent_rows": catalogue.get("agent_rows") or [],
                "available": catalogue.get("available") or [],
                "fetched_at": self._catalogue_at,
                "age_seconds": self.catalogue_age()}

    def _fetch_catalogue(self) -> Dict[str, Any]:
        try:
            account, client = self._acquire()
            catalogue = client.models()
        except (UpstreamError, PoolError) as error:
            self.log(f"model catalogue unavailable: {error}")
            model = self.cfg.model or C.DEFAULT_MODEL
            catalogue = {"models": [{"modelCode": model, "modelName": "标准", "alias": "标准",
                                     "default": True, "reasoningEffort": None,
                                     "multimodal": False, "source": "fallback",
                                     "limitedFree": False}], "available": []}
        return {
            "object": "list",
            "data": [
                {
                    "id": item["modelCode"],
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "polymas",
                    # The platform sends no context length, so this is the
                    # declared figure from constants.MODEL_CONTEXT_LENGTHS (or
                    # the 1M fallback). Clients that size a context window from
                    # a model listing read this key — dsh-llm-pi-ai accepts
                    # context_length / context_window / max_input_tokens /
                    # limit.context, measured in its model discovery.
                    "context_length": item.get("contextLength") or C.DEFAULT_CONTEXT_LENGTH,
                    "polymas": {"alias": item.get("alias"), "default": item.get("default"),
                                "reasoningEffort": item.get("reasoningEffort"),
                                "multimodal": item.get("multimodal"),
                                "source": item.get("source"),
                                "limitedFree": item.get("limitedFree"),
                                "contextLength": item.get("contextLength")},
                }
                for item in (catalogue.get("available") or catalogue["models"])
            ],
            # The raw agent rows, so the panel can show the platform's own
            # field names without reshaping them.
            "agent_rows": catalogue["models"],
            "available": catalogue.get("available") or [],
            # The agent in use honours ``modelName`` (measured 2026-11-17:
            # qwen3.8-max thinks in English, Doubao-Seed-2.1-pro in Chinese),
            # so report what the upstream catalogue says instead of the old
            # hard-coded False left over from the pinned-agent era.
            "switchable": bool(catalogue.get("switchable")),
        }

    #: The values the platform accepts for ``actMode`` / ``chatMode``. They
    #: match the client bundle's whitelist (``rs = new Set([...])``); anything
    #: else makes the platform answer HTTP 400.
    ACT_MODES = ("AUTO", "PLAN", "ASK", "GOAL")
    CHAT_MODES = ("COMMON", "FAST")

    def _request_overrides(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Per-request knobs the caller may set, overriding the panel.

        A client that shows a reasoning-effort selector has to see it work:
        before this, ``reasoning_effort`` in the request was ignored and every
        turn ran at the configured level (measured 2026-11-17 — four different
        values produced 117-604 reasoning characters with no pattern).

        ``act_mode`` / ``chat_mode`` are the platform's own switches, accepted
        here so a caller can change how the agent behaves per conversation.
        Values outside the platform whitelist are ignored rather than passed
        on: the platform answers HTTP 400 for those.
        """
        overrides: Dict[str, Any] = {}
        if "reasoning_effort" in body:
            value = body.get("reasoning_effort")
            text = "" if value is None else str(value).strip()
            # An explicit null is how a client says "off"; OpenAI clients use
            # a level name, and ``none`` is the platform's own spelling.
            overrides["reasoning_effort"] = (
                "none" if text.lower() in ("", "none", "off") else text)
        for key, name in (("act_mode", "actMode"), ("actMode", "actMode"),
                          ("chat_mode", "chatMode"), ("chatMode", "chatMode")):
            if key not in body:
                continue
            value = str(body.get(key) or "").strip().upper()
            allowed = self.ACT_MODES if name == "actMode" else self.CHAT_MODES
            if value in allowed:
                overrides["act_mode" if name == "actMode" else "chat_mode"] = value
            else:
                self.log(f"ignoring {key}={value!r}: not one of {list(allowed)}")
        if overrides:
            self.log("request overrides: " + ", ".join(
                f"{k}={v}" for k, v in sorted(overrides.items())))
        return overrides

    def _model_for(self, body: Dict[str, Any]) -> str:
        """Pick the model name for one request.

        The upstream honours ``modelName`` for this agent: a modelCode in
        it really switches the backing model. Verified 2026-11-13 with
        isolated sessions and shuffled arms — the reply's code style changes
        with the model (DeepSeek writes ``wait = 300``, Qwen writes
        ``wait`` with no default).

        "Auto" is passed through unchanged: the platform resolves it to
        the agent's own model.
        """
        requested = str(body.get("model") or "").strip()
        if requested and requested.lower() != "auto":
            return requested
        return self.cfg.model or "Auto"

    def _effective_model(self) -> str:
        """The model the agent is actually configured with.

        The catalogue is fetched from the upstream, so the answer is cached
        for a few minutes: the agent's configuration changes rarely, and the
        value is only used for reporting.
        """
        now = time.time()
        if self._effective_model_cache and now - self._effective_model_at < 300:
            return self._effective_model_cache
        result = self.cfg.model or C.DEFAULT_MODEL
        try:
            catalogue = self.models()
            # ``models()`` answers in the OpenAI shape (``data``), not the
            # upstream one (``models``). Reading the wrong key meant this loop
            # never ran, so the answer silently fell back to ``cfg.model`` —
            # which is ``Auto``, and "Auto" was then reported as the model
            # that answered (found 2026-11-17).
            rows = catalogue.get("data") or catalogue.get("models") or []
            for item in rows:
                result = item.get("id") or item.get("modelCode") or result
                break
        except Exception as error:  # noqa: BLE001
            self.log(f"cannot read the effective model: {error}")
        self._effective_model_cache = result
        self._effective_model_at = now
        return result

    def chat_completions(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /v1/chat/completions`` — non-streaming."""
        tools = self._tools_for(body)
        prompt, aliases = self._prepare_prompt(body.get("messages") or [], tools, body)
        model = self._model_for(body)
        session_id = str(body.get("session_id") or "")
        emit = self._emit_names(tools, aliases)
        account, client = self._acquire(model)
        started = time.time()
        force_tool = _demands_tool(body)
        try:
            result = self.adapter(client, emit).complete(
                client, prompt, model=model, session_id=session_id, tools=tools,
                overrides=self._request_overrides(body))
        except UpstreamError as error:
            self.pool.report_failure(account, str(error))
            self._note_credential_rejection(str(error))
            raise
        for attempt in range(1, len(self.RETRY_NUDGES) + 1):
            reason = self._retry_reason(tools, result, force_tool)
            if not reason:
                break
            nudges = self._nudges_for(reason)
            nudge = nudges[min(attempt - 1, len(nudges) - 1)]
            self.log(f"retrying pass {attempt}: {reason}")
            try:
                retry = self.adapter(client, emit).complete(
                    client, prompt + nudge, model=model,
                    session_id=session_id, tools=tools,
                    overrides=self._request_overrides(body))
            except UpstreamError as error:  # keep the best answer so far
                self.log(f"retry {attempt} failed: {error}")
                break
            if self._retry_wins(result, retry):
                result = retry
                if (result.get("choices") or [{}])[0].get("message", {}).get("tool_calls"):
                    break
        if aliases:
            result.setdefault("polymas", {})["tool_name_map"] = aliases
        # Report the model that actually answered: the requested one when the
        # caller named it, otherwise the agent's own.
        effective = model if model and model.lower() != "auto" else self._effective_model()
        if effective and result.get("model"):
            result["model"] = effective
        # Pass the tools that were actually called: the streaming path already
        # does, and without it a non-streaming reply that used a tool showed an
        # empty 工具 column (found 2026-11-18).
        called: List[str] = []
        for choice in result.get("choices") or []:
            for call in (choice.get("message") or {}).get("tool_calls") or []:
                name = (call.get("function") or {}).get("name")
                if name:
                    called.append(name)
        self._record(account, effective, prompt, result, started,
                     body.get("stream"), called)
        return result

    def chat_completions_stream(self, body: Dict[str, Any],
                                client_gone: Optional[threading.Event] = None):
        """``POST /v1/chat/completions`` with ``stream: true``.

        ``client_gone`` is set by the HTTP layer when the caller disconnects;
        it only affects how the request is journaled.
        """
        tools = self._tools_for(body)
        prompt, aliases = self._prepare_prompt(body.get("messages") or [], tools, body)
        emit = self._emit_names(tools, aliases)
        model = self._model_for(body)
        session_id = str(body.get("session_id") or "")
        account, client = self._acquire(model)
        return self._stream_with_journal(
            account, client, prompt, model, session_id, tools, emit,
            continuation=_resumes_after_tool(body.get("messages")),
            overrides=self._request_overrides(body),
            force_tool=_demands_tool(body),
            client_gone=client_gone)

    @staticmethod
    def _emit_names(tools: Any, aliases: Dict[str, str]) -> List[str]:
        """The names the prompt asked for: the alias of a declared name."""
        reverse = {declared: alias for alias, declared in (aliases or {}).items()}
        return [reverse.get(name, name) for name in declared_names(tools)]

    def _tools_for(self, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The caller's tools, when tool calling is enabled.

        The platform will not accept tool definitions on the wire, so the
        tools are described in the prompt and the answer is parsed back into
        ``tool_calls`` (see :mod:`polymas2api.toolbridge`).
        """
        if not self.cfg.tool_bridge:
            return []
        tools = body.get("tools")
        if not isinstance(tools, list) or not tools:
            # OpenAI also allows the legacy ``functions`` key.
            tools = body.get("functions")
            if isinstance(tools, list) and tools:
                tools = [{"type": "function", "function": item}
                         for item in tools if isinstance(item, dict)]
            else:
                return []
        choice = body.get("tool_choice", body.get("function_call"))
        if isinstance(choice, str) and choice.lower() == "none":
            return []
        return [item for item in tools if isinstance(item, dict)]

    def _prepare_prompt(self, messages: Any, tools: Any = None,
                        body: Optional[Dict[str, Any]] = None) -> str:
        extra = ""
        aliases: Dict[str, str] = {}
        if tools:
            choice = (body or {}).get("tool_choice", (body or {}).get("function_call"))
            extra, aliases = build_tool_prompt(
                tools, choice, getattr(self.cfg, "tool_name_style", "verbatim"),
                int(getattr(self.cfg, "tool_schema_depth", 2) or 0))
        prompt = OpenAIAdapter.extract_prompt(
            messages, extra_system=extra,
            local_images=bool(getattr(self.cfg, "local_image_attachments", True)))
        return self.sanitizer.sanitize_prompt(prompt), aliases

    def adapter(self, client, tool_names=None) -> OpenAIAdapter:
        return OpenAIAdapter(self.cfg, logger=self.log, washer=self.washer,
                             tool_names=tool_names)

    def _stream_with_journal(self, account, client, prompt, model, session_id,
                             tools: Any = None,
                             emit_names: Any = None,
                             continuation: bool = False,
                             overrides: Optional[Dict[str, Any]] = None,
                             force_tool: bool = False,
                             client_gone: Optional[threading.Event] = None,
                             ) -> Iterator[Dict[str, Any]]:
        started = time.time()
        reasoning = 0
        content = 0
        # The character counters feed the panel; the texts feed the token
        # estimate, which needs the actual mixture of CJK and Latin.
        reasoning_text: List[str] = []
        content_text: List[str] = []
        error = ""
        used_tools: List[str] = []
        effective = model if model and model.lower() != "auto" else self._effective_model()

        # A tool-enabled answer can only be judged once it is complete: the
        # CALL line sits at the end. Streaming straight through would mean
        # discovering the miss after the client already got the text, with no
        # way to retry. So a short answer is buffered until we know, and a
        # long one is released as soon as it is clearly just prose — that
        # keeps ordinary chat streaming while giving tool turns a chance to
        # be corrected.
        # No holding when the turn is *continuing* after a tool result: the
        # answer there is prose about a result the caller already has, so a
        # retry would be pointless — and holding would blank the reply for
        # three whole generations while the retries run (reported 2026-11-17).
        buffered: List[Dict[str, Any]] = []
        hold_limit = (int(getattr(self.cfg, "tool_stream_buffer_chars", 240))
                      if tools and not continuation else 0)
        held = hold_limit > 0

        def _release() -> Iterator[Dict[str, Any]]:
            del buffered[:]
            return iter(())

        try:
            for chunk in self.adapter(client, emit_names).stream(
                    client, prompt, model=model, session_id=session_id, tools=tools,
                    overrides=overrides):
                if "error" in chunk:
                    error = str(chunk["error"].get("message", ""))
                    for pending in buffered:
                        yield pending
                    _release()
                    held = False
                    yield chunk
                    continue
                if effective:
                    chunk["model"] = effective
                self._audit_upstream_tool(chunk, used_tools)
                delta = chunk["choices"][0]["delta"]
                if delta.get("reasoning_content"):
                    reasoning_text.append(delta["reasoning_content"])
                    reasoning += len(delta["reasoning_content"])
                if delta.get("content"):
                    content_text.append(delta["content"])
                    content += len(delta["content"])
                if held:
                    # The reasoning stream can never carry a CALL line, so it
                    # has no reason to wait. Holding it meant the client saw a
                    # completely blank reply for the whole generation and then
                    # everything at once (reported 2026-11-17 as "卡死").
                    if delta.get("reasoning_content") and not delta.get("content"):
                        yield chunk
                        continue
                    buffered.append(chunk)
                    # Either condition settles the question this hold exists to
                    # answer. Prose past the limit cannot be a claim-without-call
                    # shape, and a parsed call means the retry can no longer
                    # fire at all. Holding a call back only delays the client —
                    # with the persona emitting the call on its first line, a
                    # short answer would otherwise stay invisible for the whole
                    # generation (reported 2026-11-19 as "nothing was output").
                    if delta.get("tool_calls") or (hold_limit and content > hold_limit):
                        for pending in buffered:
                            yield pending
                        _release()
                        held = False
                    continue
                yield chunk
        except UpstreamError as exc:
            error = str(exc)
            self.pool.report_failure(account, error)
            self._note_credential_rejection(error)
            for pending in buffered:
                yield pending
            _release()
            held = False
            yield {"error": {"message": error, "type": "upstream_error"}}
        finally:
            # ``buffered`` may be empty even though nothing was said: the
            # reasoning stream is released immediately and an answer with no
            # content never adds a chunk. Requiring ``buffered`` here skipped
            # the empty-answer retry entirely.
            if held and not error:
                has_call = any(chunk["choices"][0]["delta"].get("tool_calls")
                               for chunk in buffered)
                winner = buffered
                joined = "".join(chunk["choices"][0]["delta"].get("content") or ""
                                 for chunk in buffered)
                reason = ""
                if tools and getattr(self.cfg, "tool_retry", True):
                    reason = _retry_reason_text(joined, has_call, used_tools,
                                                force_tool)
                if reason:
                    self.log(f"stream retry: {reason}")
                    winner = self._retry_stream(client, prompt, model, session_id,
                                                tools, emit_names, effective,
                                                used_tools, buffered, overrides,
                                                self._nudges_for(reason))
                for pending in winner:
                    yield pending
                del buffered[:]
            self._record(account, model, prompt,
                         {"reasoning": reasoning, "content": content, "error": error,
                          "usage": usage_for(prompt, "".join(content_text),
                                             "".join(reasoning_text))},
                         started, True, used_tools,
                         aborted=bool(client_gone is not None and client_gone.is_set()))

    def _retry_stream(self, client, prompt, model, session_id, tools, emit_names,
                      effective, used_tools, buffered, overrides=None,
                      nudges: Any = None,
                      ) -> List[Dict[str, Any]]:
        """Ask again when the buffered answer went wrong.

        Returns the replacement chunks, or the original buffered ones when
        the retry does no better.
        """
        old_text = "".join(chunk["choices"][0]["delta"].get("content") or ""
                           for chunk in buffered)
        for attempt, nudge in enumerate(nudges or self.RETRY_NUDGES, start=1):
            self.log(f"retrying buffered stream ({attempt})")
            try:
                chunks = list(self.adapter(client, emit_names).stream(
                    client, prompt + nudge, model=model,
                    session_id=session_id, tools=tools,
                    overrides=overrides))
            except UpstreamError as error:
                self.log(f"stream retry {attempt} failed: {error}")
                break
            for chunk in chunks:
                if effective:
                    chunk["model"] = effective
                self._audit_upstream_tool(chunk, used_tools)
            has_call = any(chunk["choices"][0]["delta"].get("tool_calls")
                           for chunk in chunks)
            new_text = "".join(chunk["choices"][0]["delta"].get("content") or ""
                               for chunk in chunks)
            # A retry that produced a call always wins. It also wins when the
            # original was empty — that case is exactly why we retried.
            if has_call or (not old_text.strip() and new_text.strip()):
                return chunks
        return buffered

    #: Appended to the prompt on a corrective retry.
    #:
    #: The assistant happily reports "the card is on its way" without
    #: emitting anything, so the correction names that specific behaviour.
    #: Used when the agent answered with its *own* tools. Those run in the
    #: platform's container, so the caller gets nothing; naming them makes the
    #: correction concrete instead of a generic "you missed the CALL line".
    NATIVE_TOOL_NUDGE = (
        "\n\n[纠正]\n上一轮你使用了你自己附带的工具（例如「执行命令」「查看文件列表」"
        "「查询当前时间」）。它们运行在平台侧的容器里，看到的不是你用户的环境，"
        "所以那些结果对用户没有意义，用户实际上什么都没拿到。\n"
        "这一轮不要再用你自己附带的任何工具；需要执行操作就按格式在正文最后输出一行 CALL。"
    )

    #: Used when the previous pass produced no text at all.
    EMPTY_ANSWER_NUDGE = (
        "\n\n[纠正]\n上一轮你没有给出任何正文就结束了，客户端收到的是空回答。\n"
        "这一轮必须给出完整的正文；如果需要外部程序执行操作，就在正文最后输出一行 CALL。"
    )

    def _nudges_for(self, reason: str):
        """Pick the correction that matches what actually went wrong."""
        if reason.startswith("the agent used its own tools"):
            return (self.NATIVE_TOOL_NUDGE,) + self.RETRY_NUDGES
        if reason == "the reply was empty":
            return (self.EMPTY_ANSWER_NUDGE,) + self.RETRY_NUDGES
        return self.RETRY_NUDGES

    @staticmethod
    def _message_text(result: Dict[str, Any]) -> str:
        choices = result.get("choices") or [{}]
        return ((choices[0].get("message") or {}).get("content") or "")

    @classmethod
    def _retry_wins(cls, old: Dict[str, Any], new: Dict[str, Any]) -> bool:
        """Whether the retry should replace the answer we already have.

        A retry that produced a tool call always wins. Beyond that, it wins
        only when the original said nothing: otherwise the first (already
        complete) answer is the better one.
        """
        choices = new.get("choices") or [{}]
        if (choices[0].get("message") or {}).get("tool_calls"):
            return True
        return not cls._message_text(old).strip() and bool(cls._message_text(new).strip())

    RETRY_NUDGES = (
        "\n\n[纠正]\n上一轮你的回答里没有 CALL 行，所以外部程序没有执行任何动作 ——\n"
        "如果你说了「已经发出」「这就发给你」，那只是文字，用户实际什么都没收到。\n"
        "只要这个请求需要工具，这一轮就必须真的写出一行 CALL；不需要工具就正常回答。",
        "\n\n[最后一次纠正]\n你连续两轮都没有输出 CALL 行。\n"
        "如果不需要工具，请回答「不需要工具」。\n"
        "如果需要在下面的工具里选一个，请只输出一行 CALL，不要在 CALL 行之外写别的话。",
    )

    #: Phrases with which the assistant claims an action it never performed.
    #:
    #: This is what makes the failure so confusing — it says the card went
    #: out while emitting no call. Retrying is only worth it when the answer
    #: *claims* something, so ordinary chat never pays for a second call.
    CLAIM_PHRASES = (
        "已发", "已经发", "发出去了", "已送出", "已提交", "已执行", "已调用",
        "这就发", "马上发", "这就给", "马上给", "这就帮", "已经帮你", "已在",
        "已为", "已经完成", "执行完成", "已完成", "已生成", "已经打开", "已打开",
    )

    @classmethod
    def _claims_an_action(cls, text: str) -> bool:
        return any(phrase in text for phrase in cls.CLAIM_PHRASES)

    def _audit_upstream_tool(self, chunk: Dict[str, Any],
                             used: List[str]) -> None:
        """Record the agent's own tool call, and decide whether it goes out.

        The name is always recorded for the journal. The payload itself —
        the agent's raw ``input``/``output`` JSON — is only kept when the
        operator asked for it: a client that does not know the ``polymas``
        key may render such a blob as a stray code block inside the reply.
        """
        tool = chunk.pop("_upstream_tool", None)
        if tool is None:
            tool = (chunk.get("polymas") or {}).get("upstream_tool")
        if tool and tool.get("name"):
            used.append(tool["name"])
        if not getattr(self.cfg, "expose_upstream_tools", True):
            chunk.pop("polymas", None)
    def _retry_reason(self, tools: Any, result: Dict[str, Any],
                      force_tool: bool = False) -> str:
        """Why this answer is worth asking again, or "" when it is fine.

        The rules are observations, not guesses: something demonstrably went
        wrong, rather than "the text did not mention a tool". That distinction
        matters — an earlier version retried every tool-enabled turn and cost
        two extra generations on plain greetings (17s for "晚上好").
        """
        if not tools or not getattr(self.cfg, "tool_retry", True):
            return ""
        choices = result.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        if message.get("tool_calls"):
            return ""
        if choices[0].get("finish_reason") != "stop":
            return ""
        if force_tool:
            return "tool_choice=required but the answer carried no call"
        # The agent ran its *own* tools instead. Those live in the platform's
        # container, so whatever they saw is not the caller's machine: the
        # request plainly needed a tool and the wrong one answered.
        if result.get("_upstream_tools"):
            names = ", ".join(str(item.get("name") or "") for item in
                              result["_upstream_tools"] if item.get("name"))
            return f"the agent used its own tools ({names}) instead of emitting a CALL"
        content = message.get("content") or ""
        # Nothing was said at all. Measured 2026-11-17: 3 of 5 runs on a
        # question it could not answer ended with an empty reply.
        if not content.strip():
            return "the reply was empty"
        if _claims_action(content):
            return "the answer claims an action it never called"
        return ""

    def _needs_retry(self, tools: Any, result: Dict[str, Any],
                     force_tool: bool = False) -> bool:
        return bool(self._retry_reason(tools, result, force_tool))

    def _record(self, account, model, prompt, result, started, stream,
                tools: Any = None, aborted: bool = False) -> None:
        duration = int((time.time() - started) * 1000)
        # Two shapes reach this point: the OpenAI completion object (non
        # streaming) and the plain {"reasoning": n, "content": n} tally the
        # streaming path builds. Both have to be understood, otherwise the
        # counters stay at zero.
        reasoning = int(result.get("reasoning") or 0)
        content = int(result.get("content") or 0)
        usage = result.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if not reasoning and not content:
            choices = result.get("choices") or []
            if choices:
                message = choices[0].get("message") or {}
                content = len(message.get("content") or "")
                reasoning = len(message.get("reasoning_content") or "")
        # The agent's own tool use is an audit signal: it has a shell.
        used = ""
        if not tools:
            upstream = result.pop("_upstream_tools", None)
            if upstream is None:
                upstream = (result.get("polymas") or {}).get("upstream_tools") or []
            tools = [item.get("name", "") for item in upstream if item.get("name")]
        # The internal key must never reach the client; this runs before the
        # caller returns the reply, so popping here is enough.
        result.pop("_upstream_tools", None)
        if tools:
            used = ",".join(dict.fromkeys(str(name) for name in tools if name))
        error = ""
        raw_error = result.get("error")
        if raw_error:
            error = (raw_error.get("message") if isinstance(raw_error, dict)
                     else str(raw_error))
        entry = JournalEntry(
            account=getattr(account, "uid", "default"),
            model=model,
            stream=bool(stream),
            prompt_chars=len(prompt or ""),
            duration_ms=duration,
            ok=not error,
            status=("aborted" if aborted and not error
                    else ("ok" if not error else "error")),
            bytes_in=reasoning + content,
            bytes_out=len(prompt or ""),
            error=error,
            reasoning_chars=reasoning,
            content_chars=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tools=used,
            aborted=aborted,
        )
        self.journal.record(entry)
        if not error:
            self.pool.report_success(account)

    # -- panel --------------------------------------------------------------
    def _agent_name(self) -> str:
        """The assistant's display name, as the platform reports it."""
        cached = getattr(self, "_agent_name_cache", None)
        if cached is not None:
            return cached
        name = ""
        try:
            # Ask one specific account. ``_acquire`` rotates the pool, so this
            # used to return a different agent's name on every call (measured
            # 2026-11-18: two accounts alternated between 「通用助手」 and
            # 「通用助手测试」).
            account = next((self.pool.get(a["uid"]) for a in self.pool.list()
                            if a.get("enabled")), None)
            if account is not None:
                name = self._client(account).agent_info().get("name") or ""
        except Exception as error:  # noqa: BLE001
            self.log(f"cannot read the agent name: {error}")
        self._agent_name_cache = name
        return name

    def _effective_app_code(self) -> str:
        """The agent id to show, taken from the pool rather than config.json.

        The credential fields in the config are a migration source only; a
        fresh install has none, so reading them here would show "app_code —"
        even though the pool is full.
        """
        for item in self.pool.list():
            if item.get("enabled"):
                account = self.pool.get(item["uid"])
                if account is not None:
                    return account.app_code or account.friend_nid or ""
        return self.cfg.app_code or ""

    def _pool_agents(self) -> Dict[str, Any]:
        """Which agents the pool actually uses.

        The header's ``app_code`` is the configured one, but with several
        accounts each carries its own agent and requests rotate between them
        (measured 2026-11-18), so the panel has to say so.
        """
        seen: Dict[str, Dict[str, Any]] = {}
        for item in self.pool.list():
            account = self.pool.get(item["uid"])
            if account is None:
                continue
            key = account.friend_nid or "(未设置)"
            entry = seen.setdefault(key, {"nid": key, "accounts": [], "enabled": False})
            entry["accounts"].append(account.uid)
            entry["enabled"] = entry["enabled"] or bool(account.enabled)
        return {"distinct": len(seen), "items": list(seen.values())}

    def panel_overview(self) -> Dict[str, Any]:
        cfg = self.cfg
        counters = self.journal.counters()
        durations = [entry["duration_ms"] for entry in self.journal.recent(limit=200)]
        counters["total_duration_ms"] = sum(durations)
        catalogue = self.models()
        effective = self._effective_model()
        accounts = self.pool.list()
        shares = self.pool.shares()
        for account in accounts:
            account["share"] = shares.get(account["uid"])
        return {
            "stats": counters,
            "pool": self.pool.stats(),
            "fingerprint": {
                "profile": self.fingerprint.profile_for("default").name,
                "available": sorted(PROFILES),
                "app_code": self._effective_app_code(),
                "model": effective,
                "send_origin": cfg.send_origin,
                "tls": "TLS 1.2（上游拒绝 1.3）；ALPN http/1.1",
                "min_interval_ms": cfg.min_interval_ms,
                "jitter_ms": cfg.jitter_ms,
                "think_time_per_char_ms": cfg.think_time_per_char_ms,
                "think_time_cap_ms": cfg.think_time_cap_ms,
                "sanitize_scope": cfg.sanitize_scope,
                "sanitize_enabled": cfg.sanitize_enabled,
                "pool_enabled": cfg.pool_enabled,
            },
            "models": catalogue["data"],
            "models_age_seconds": self.catalogue_age(),
            "agent_rows": catalogue.get("agent_rows") or [],
            "available": catalogue.get("available") or [],
            "accounts": accounts,
            "recent": self.journal.recent(limit=6),
            "agent": {"nid": self._effective_app_code(), "name": self._agent_name(),
                      "pool": self._pool_agents()},
            "credentials": {name: status.to_dict()
                            for name, status in self.credentials.items()},
            "renewal": {
                # Aggregated over the pool: each account renews separately, so
                # the renewer's own counters only ever describe the configured
                # one and would read as 0 here.
                "renewals": sum(int(a.get("renew_count") or 0) for a in accounts),
                "failures": sum(1 for a in accounts if a.get("renew_error")),
                "renewed_at": max([a.get("renewed_at") or 0 for a in accounts] or [0]) or None,
                "renewed_at_local": (time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(max(a.get("renewed_at") or 0 for a in accounts)))
                    if any(a.get("renewed_at") for a in accounts) else ""),
                "last_error": next((a.get("renew_error") or "" for a in accounts
                                    if a.get("renew_error")), ""),
                "interval_minutes": self.cfg.renew_interval_minutes,
                "enabled": self.cfg.renew_interval_minutes > 0,
            },
        }

    def panel_settings(self) -> Dict[str, Any]:
        cfg = self.cfg
        catalogue = self.models()
        options = ["Auto"]
        for item in catalogue.get("available") or catalogue["data"]:
            code = item.get("modelCode") or item.get("id")
            if code and code not in options:
                options.append(code)
        return {
            "settings": {
                "model": cfg.model or "Auto",
                "fingerprint": cfg.fingerprint,
                "send_origin": cfg.send_origin,
                "min_interval_ms": cfg.min_interval_ms,
                "jitter_ms": cfg.jitter_ms,
                "think_time_per_char_ms": cfg.think_time_per_char_ms,
                "think_time_cap_ms": cfg.think_time_cap_ms,
                "sanitize_scope": cfg.sanitize_scope,
                "sanitize_enabled": cfg.sanitize_enabled,
                "pool_enabled": cfg.pool_enabled,
                "archive_enabled": cfg.archive_enabled,
                "journal_size": cfg.journal_size,
                "persona_scrub_names": cfg.persona_scrub_names,
                "tool_bridge": cfg.tool_bridge,
                "expose_upstream_tools": cfg.expose_upstream_tools,
                "session_isolation": cfg.session_isolation,
                "tool_name_style": cfg.tool_name_style,
                "tool_schema_depth": cfg.tool_schema_depth,
                "tool_retry": cfg.tool_retry,
                "tool_stream_buffer_chars": cfg.tool_stream_buffer_chars,
                "chat_mode": cfg.chat_mode,
                "act_mode": cfg.act_mode,
                "terminal": cfg.terminal,
                "reasoning_effort": cfg.reasoning_effort,
                "use_agent": cfg.use_agent,
                "web_retrieve": cfg.web_retrieve,
                "local_image_attachments": cfg.local_image_attachments,
                "retain_long_term_memory": cfg.retain_long_term_memory,
            },
            "model_options": options,
        }

    # -- account management -------------------------------------------------
    def _migrate_legacy_account(self) -> None:
        """Write credentials left inline in config.json into accounts.json.

        ``accounts.json`` is the only source of truth; ``config.json`` is for
        settings. A configuration from before that split still carries
        ``ai_poly``/``user_nid``, and the pool seeds itself from them so it
        keeps working — this makes that account permanent, so from then on it
        behaves like any other (deletable, renamable, renewed with the rest).

        config.json is deliberately left untouched: rolling back is just
        deleting accounts.json and restarting.
        """
        path = self.pool.accounts_path
        if path is None:
            return
        try:
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                entries = existing.get("accounts") if isinstance(existing, dict) else existing
                if entries:
                    return
        except (OSError, json.JSONDecodeError):
            pass
        seeded = [a for a in self.pool.list() if a.get("has_ai_poly") or a.get("has_jt_cas")]
        if not seeded:
            return
        if self.pool.save_accounts():
            self.log(f"migrated {len(seeded)} credential(s) from config.json "
                     f"into {path.name}")


    def _account_client(self, account: Credential) -> PolymasClient:
        """A client bound to one account, without the renewer's override."""
        return PolymasClient(self.cfg, account, logger=self.log,
                             fingerprint=self.fingerprint, shaper=self.shaper)

    def _find_duplicate(self, credential: str) -> Optional[str]:
        """The uid already holding this cookie, if any."""
        if not credential:
            return None
        candidates = [self.pool.get(item["uid"]) for item in self.pool.list()]
        for existing in candidates:
            if existing is not None and existing.credential == credential:
                return existing.uid
        return None

    def panel_verify_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Check one ai-poly before anything is created.

        This deliberately asks for nothing but the account's own signature
        (``/chatim-user/v1/query/user-sig``), which needs only a valid cookie —
        no agent, no model catalogue. Creating the agent is the expensive and
        hard-to-undo step, so it waits for confirmation.
        """
        parsed = self._parse_account_payload(payload)
        if "error" in parsed:
            return {"ok": False, "error": parsed["error"]}
        account = parsed["account"]
        try:
            self._account_client(account).user_sig()
        except UpstreamError as error:
            return {"ok": False, "error": {
                "message": f"凭据校验失败：{error}"}}
        return {"ok": True, "verified": True, "test_only": True,
                "account": account.to_public()}

    def _parse_account_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Shared validation: turn one pasted cookie into a Credential."""
        ai_poly = str(payload.get("ai_poly") or "").strip()
        if not ai_poly:
            return {"error": {"message": "ai_poly 必填：只贴 ai-poly 就够"}}
        claims = decode_jwt(ai_poly) or {}
        user_nid = str(payload.get("user_nid") or claims.get("userNid")
                       or claims.get("loginId") or "").strip()
        if not user_nid:
            return {"error": {"message": "无法从 ai_poly 里读出 userNid"}}
        uid = str(payload.get("uid") or user_nid).strip()
        account = Credential(
            uid,
            user_nid=user_nid,
            friend_nid=str(payload.get("friend_nid") or "").strip(),
            ai_poly=ai_poly,
            app_code=str(payload.get("app_code") or "").strip(),
            label=str(payload.get("label") or "").strip(),
            weight=_weight_of(payload.get("weight")),
        )
        return {"account": account}

    def panel_add_account(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Add one account, creating a fresh generic agent for it.

        Only ``ai_poly`` has to be supplied. The gateway then:

        1. checks the cookie against the account's own signature endpoint;
        2. creates a generic agent under that account
           (``POST /llmOps/application/saveAssistant``, measured 2026-11-16 —
           a STUDENT account may do this, and the reply carries the new nid);
        3. writes the tuned persona into that agent
           (``PUT /ai-agent/persona/{nid}``), which is the only level where a
           client can compete with the platform's built-in toolbelt;
        4. pulls that agent's model catalogue, which proves the pairing really
           works before anything is written to disk;
        5. stores the account with the new nid as ``friend_nid``/``app_code``.

        Why create an agent at all: the course-representative agents ship a
        built-in 「询问用户」 tool that steals tool calls, and they carry a
        course knowledge base this gateway has no use for. A self-built shell
        has neither — but it still gets the platform's sandbox tools, which is
        what the persona is for.
        """
        parsed = self._parse_account_payload(payload)
        if "error" in parsed:
            return {"ok": False, "error": parsed["error"]}
        account = parsed["account"]

        if self.pool.get(account.uid) is not None:
            return {"ok": False, "error": {"message": f"账号 {account.uid} 已存在"}}
        duplicate = self._find_duplicate(account.credential)
        if duplicate:
            return {"ok": False, "error": {
                "message": f"这份凭据已经在池里了（uid={duplicate}），不需要重复添加"}}

        client = self._account_client(account)

        if payload.get("verify", True):
            try:
                client.user_sig()
            except UpstreamError as error:
                return {"ok": False, "error": {
                    "message": f"凭据校验失败：{error}（确认 ai-poly 没复制错，"
                               f"或勾选『跳过校验』）"}}

        if payload.get("test"):
            return {"ok": True, "verified": True, "test_only": True,
                    "account": account.to_public()}

        # -- the agent that will actually answer ---------------------------
        friend_nid = account.friend_nid
        created = ""
        persona_result: Dict[str, Any] = {"applied": False, "error": "未新建助手"}
        if payload.get("create_agent", True):
            try:
                created = client.create_assistant(
                    str(payload.get("app_name") or "通用助手"))
            except UpstreamError as error:
                return {"ok": False, "error": {
                    "message": f"新建通用助手失败：{error}"}}
            account.friend_nid = created
            account.app_code = created
            friend_nid = created
            persona_result = self._write_persona(account, created)
        elif not friend_nid:
            return {"ok": False, "error": {
                "message": "没有可用的助手 id，也没有要求新建助手"}}

        # The pairing is only real if the agent's catalogue comes back; a nid
        # from another account would answer empty here. Skipped when the caller
        # asked for no verification at all.
        if payload.get("verify", True):
            try:
                catalogue = self._account_client(account).models()
            except UpstreamError as error:
                return {"ok": False, "error": {
                    "message": f"助手 {friend_nid} 校验失败：{error}"
                               + (f"（已创建 agent {created}，但未能使用）" if created else "")}}
            if not (catalogue.get("models") or catalogue.get("available")):
                return {"ok": False, "error": {
                    "message": f"助手 {friend_nid} 拉不到模型目录，凭据与助手不匹配"}}

        if not self.pool.add(account):
            return {"ok": False, "error": {"message": f"账号 {account.uid} 已在池中"}}
        persisted = self.pool.save_accounts()
        self.log(f"account added: {account.uid} (agent {friend_nid})")
        public = account.to_public()
        public["created_agent"] = created
        return {"ok": True, "account": public, "persisted": persisted,
                "persona": persona_result,
                "note": "" if persisted else "accounts_file 未配置，重启后会丢失"}

    def _write_persona(self, account: Any, agent_nid: str) -> Dict[str, Any]:
        """Put the tuned persona on one agent; never fail the caller for it.

        An agent without the persona still answers — it just keeps using the
        platform's own sandbox tools on some prompts — so a rejection here is
        reported to the panel rather than rolled back.
        """
        try:
            client = self._account_client(account)
            client.set_persona(agent_nid, agent_persona.RULES)
        except (UpstreamError, OSError) as error:
            self.log(f"persona not written for {agent_nid}: {error}")
            return {"applied": False, "error": str(error)}
        self.log(f"persona written for {agent_nid}")
        return {"applied": True, "error": ""}

    def panel_apply_persona(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """(Re)write the tuned persona onto existing accounts' agents.

        Accounts added before the persona existed carry an empty one, which is
        why this is a panel action and not just a step of ``accounts``.
        """
        uid = str(payload.get("uid") or "")
        targets = ([self.pool.get(uid)] if uid else list(self.pool.accounts()))
        targets = [account for account in targets if account is not None]
        if uid and not targets:
            return {"ok": False, "error": {"message": f"账号 {uid} 不存在"}}
        results = []
        for account in targets:
            nid = account.app_code or account.friend_nid
            if not nid:
                results.append({"uid": account.uid, "applied": False,
                                "error": "账号没有助手 id"})
                continue
            outcome = self._write_persona(account, nid)
            results.append({"uid": account.uid, "agent": nid, **outcome})
        applied = [item for item in results if item.get("applied")]
        return {"ok": bool(applied) or not results, "results": results,
                "applied": len(applied), "total": len(results)}

    def panel_set_label(self, uid: str, label: Any) -> Dict[str, Any]:
        """Rename the note shown in the accounts table."""
        if not self.pool.set_label(uid, str(label or "")):
            return {"ok": False, "error": {"message": f"账号 {uid} 不存在"}}
        self.pool.save_accounts()
        account = self.pool.get(uid)
        return {"ok": True, "account": account.to_public() if account else {}}

    def panel_set_weight(self, uid: str, weight: Any) -> Dict[str, Any]:
        """Change one account's share of the traffic, live and persisted.

        Weight is a share, not a rate: weights 3:1 mean the first account
        answers three quarters of the requests. Rotating is smooth, so the
        three-quarters are interleaved rather than three in a row.
        """
        if not self.pool.set_weight(uid, _weight_of(weight)):
            return {"ok": False, "error": {"message": f"账号 {uid} 不存在"}}
        account = self.pool.get(uid)
        persisted = self.pool.save_accounts()
        self.log(f"account weight: {uid} -> {account.weight if account else '?'}")
        return {"ok": True, "persisted": persisted,
                "shares": self.pool.shares(),
                "account": account.to_public() if account else {}}

    def panel_delete_account(self, uid: str) -> Dict[str, Any]:
        """Remove one account. The built-in ``default`` account cannot go."""
        if self.pool.get(uid) is None:
            return {"ok": False, "error": {"message": f"账号 {uid} 不存在"}}
        # No "last account" guard on purpose: a fresh install has zero
        # accounts, so deleting down to zero is a state the product already
        # expects. Requests answer 503 no_account until one is added back.
        if not self.pool.remove(uid):
            return {"ok": False, "error": {"message": f"账号 {uid} 删除失败"}}
        persisted = self.pool.save_accounts()
        self.log(f"account removed: {uid}")
        return {"ok": True, "persisted": persisted}

    def panel_save_settings(self, changes: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "fingerprint", "send_origin", "min_interval_ms", "jitter_ms",
            "think_time_per_char_ms", "think_time_cap_ms", "sanitize_scope",
            "sanitize_enabled", "pool_enabled", "archive_enabled", "journal_size",
            "persona_scrub_names",
            "chat_mode", "act_mode", "terminal", "reasoning_effort",
            "use_agent", "web_retrieve", "retain_long_term_memory",
            "tool_bridge", "expose_upstream_tools",
            "session_isolation", "tool_name_style", "tool_schema_depth",
            # These four were rendered by the panel but missing here, so every
            # click on them was silently discarded (the reply still said ok,
            # with an empty "applied"). Measured 2026-11-17: the model picker
            # looked dead for exactly this reason.
            "model", "local_image_attachments", "tool_retry",
            "tool_stream_buffer_chars",
        }
        applied = {key: value for key, value in changes.items() if key in allowed}
        if "fingerprint" in applied and applied["fingerprint"] not in PROFILES:
            return {"error": {"message": f"unknown fingerprint profile: {applied['fingerprint']}"}}
        if "sanitize_scope" in applied and applied["sanitize_scope"] not in ("all", "identity", "off"):
            return {"error": {"message": "sanitize_scope must be all, identity or off"}}
        # Numeric settings are bounded: a think time of 1e9 would stall every
        # request for a very long time.
        bounds = {
            "min_interval_ms": (0, 60000),
            "jitter_ms": (0, 60000),
            "think_time_per_char_ms": (0.0, 1000.0),
            "think_time_cap_ms": (0, 600000),
            "journal_size": (1, 100000),
            "tool_stream_buffer_chars": (0, 100000),
        }
        for key, (low, high) in bounds.items():
            if key not in applied:
                continue
            try:
                value = float(applied[key])
            except (TypeError, ValueError):
                return {"error": {"message": f"{key} must be a number"}}
            if not low <= value <= high:
                return {"error": {"message": f"{key} must be between {low} and {high}"}}
            applied[key] = int(value) if isinstance(applied[key], int) or key == "journal_size" else value
        cfg = self.live.update(applied)
        self.sanitizer = Sanitizer(cfg.sanitize_scope, enabled=cfg.sanitize_enabled)
        self.washer = NameScrubber(cfg.persona_scrub_names)
        self.shaper = RequestShaper(cfg, logger=self.log)
        self.journal = Journal(cfg, logger=self.log)
        if "fingerprint" in applied:
            self.fingerprint.reconfigure(cfg.fingerprint)
        written = self._persist(applied)
        self.log(f"settings applied: {applied} (persisted={written})")
        return {"ok": True, "applied": applied, "persisted": written}

    def _persist(self, changes: Dict[str, Any]) -> bool:
        """Write the changed settings back to the config file."""
        path = self.cfg.extra.get("_config_path")
        if not path:
            return False
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(raw, dict):
            return False
        raw.update(changes)
        try:
            Path(path).write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8")
            return True
        except OSError:
            return False

    def panel_check_upstream(self) -> Dict[str, Any]:
        account, client = self._acquire()
        try:
            catalogue = client.models()["models"]
        except (UpstreamError, PoolError) as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "account": getattr(account, "uid", "default"),
                "models": catalogue}

    def session_new(self) -> Dict[str, Any]:
        """Clear the assistant's memory window."""
        account, client = self._acquire()
        client.new_session("新会话")
        return {"object": "session", "cleared": True,
                "app_code": self._effective_app_code(),
                "account": getattr(account, "uid", "")}


class Handler(BaseHTTPRequestHandler):
    """One request handler; ``gateway`` is injected by :func:`serve`."""

    protocol_version = "HTTP/1.1"
    #: The default header is "BaseHTTP/0.6 Python/3.14.9", which hands an
    #: attacker the exact interpreter version for free. The gateway's name is
    #: enough; no version. (Found in the 2026-11-18 review.)
    server_version = "polymas2api"
    sys_version = ""

    def version_string(self) -> str:
        """Just the name — the default joins ``sys_version`` with a space."""
        return self.server_version
    gateway: Gateway = None  # type: ignore[assignment]

    #: Socket timeout. Without it a client that opens a connection and
    #: never sends anything holds a thread until the OS gives up, which is
    #: the slowloris shape.
    timeout = 60

    # -- helpers ------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        self.gateway.log("http %s" % (fmt % args))

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # COOP is only honoured on a trustworthy (https) origin; sending it
        # over plain http only produces a console warning, so it is left off
        # unless the panel is actually served over TLS.
        if self.headers.get("X-Forwarded-Proto") == "https":
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _send_json(self, payload: Dict[str, Any], status: int = 200,
                   *, close: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close:
            # A rejected request may still have an unread body; leaving the
            # connection open would let those bytes be read as the next
            # request line.
            self.send_header("Connection", "close")
            self.close_connection = True
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        """Constant-time Bearer check, shared by /v1 and /panel/api.

        The key is hashed before comparison so the timing does not depend on
        the key length, and an absent header still performs one comparison so
        the timing shape does not depend on whether a header was sent.
        """
        return _verify_bearer(self.headers, self.gateway.cfg.api_key)

    def _panel_authorised(self) -> bool:
        """The panel accepts the panel password, or the API key.

        workbuddy2api runs on one credential for both surfaces, and so does
        this gateway: a client that talks to /v1 can open the panel with the
        same key. The panel password is only useful when you want the panel
        to have a different secret from the API.
        """
        cfg = self.gateway.cfg
        if cfg.panel_password and _verify_bearer(self.headers, cfg.panel_password):
            return True
        return _verify_bearer(self.headers, cfg.api_key)

    def _read_body(self) -> Dict[str, Any]:
        """Read and parse the request body, with a hard size ceiling.

        Without a ceiling a single request could allocate an arbitrary
        amount of memory; a negative ``Content-Length`` would make
        ``read()`` block until the client closes the connection, pinning a
        thread for as long as it likes.
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            # Chunked bodies are not supported; an empty body is treated as
            # an empty object, but a request that claims to have a body and
            # does not send a length is rejected.
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("chunked request bodies are not supported")
            return {}
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValueError("Content-Length is not a number") from error
        if length < 0:
            raise ValueError("Content-Length must not be negative")
        if length > MAX_BODY_BYTES:
            raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"request body is not valid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        raw_path = self.path.split("?", 1)[0]
        # The trailing slash is significant: it is what the browser uses as
        # the base for the page's relative asset paths, so "/panel" and
        # "/panel/" must not be collapsed into one another.
        path = raw_path.rstrip("/") or "/"
        if path in ("/health", "/"):
            self._send_json({"status": "ok",
                             "app_code": self.gateway._effective_app_code()})
            return
        if path == "/v1/models":
            if not self._authorised():
                self._send_json({"error": {"message": "invalid api key", "type": "invalid_request_error"}}, 401, close=True)
                return
            self._send_json(self.gateway.models())
            return
        if raw_path == "/panel":
            # Without the trailing slash the browser resolves the page's
            # asset paths against "/", so "/panel" would ask for "/style.css"
            # and get a 404. Redirect to the canonical form.
            self.send_response(301)
            self.send_header("Location", "/panel/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if raw_path == "/panel/":
            self._send_bytes(self._panel_asset("index.html"), "text/html; charset=utf-8")
            return
        if raw_path.startswith("/panel/"):
            name = raw_path[len("/panel/"):]
            if name in ("app.js", "style.css", "theme.js"):
                content_type = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
                self._send_bytes(self._panel_asset(name), content_type)
                return
        if path.startswith("/panel/api/"):
            if not self._panel_authorised():
                self._send_json({"error": {"message": "invalid api key", "type": "invalid_request_error"}}, 401, close=True)
                return
            self._panel_get(path[len("/panel/api/"):])
            return
        self._send_json({"error": {"message": f"unknown path {path}", "type": "invalid_request_error"}}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path.startswith("/panel/api/"):
            if not self._panel_authorised():
                self._send_json({"error": {"message": "invalid api key", "type": "invalid_request_error"}}, 401, close=True)
                return
            self._panel_post(path[len("/panel/api/"):])
            return
        if not self._authorised():
            self._send_json({"error": {"message": "invalid api key", "type": "invalid_request_error"}}, 401, close=True)
            return
        try:
            body = self._read_body()
        except ValueError as error:
            self._send_json({"error": {"message": str(error), "type": "invalid_request_error"}}, 400)
            return
        try:
            if path == "/v1/chat/completions":
                if body.get("stream"):
                    self._stream(body)
                else:
                    self._send_json(self.gateway.chat_completions(body))
                return
            if path == "/v1/session/new":
                self._send_json(self.gateway.session_new())
                return
        except UpstreamError as error:
            self.gateway.log(f"upstream error on {path}: {error}")
            self._send_json({"error": {"message": str(error), "type": "upstream_error",
                                       "code": error.code, "trace_id": error.trace_id}}, 502)
            return
        except PoolError as error:
            self._send_json({"error": {"message": str(error), "type": "no_account"}}, 503)
            return
        except Exception as error:  # noqa: BLE001
            # The traceback stays in the log; the client only learns that
            # something went wrong. An exception message can carry a file
            # path or, worse, part of a request body.
            self.gateway.log(f"unhandled error on {path}:\n{traceback.format_exc()}")
            self._send_json({"error": {"message": "internal error", "type": "internal_error"}}, 500)
            return
        self._send_json({"error": {"message": f"unknown path {path}", "type": "invalid_request_error"}}, 404)

    # -- panel plumbing -----------------------------------------------------
    #: The only files the panel may serve. The check lives here rather than at
    #: the call site so a path can never escape the web directory.
    PANEL_ASSETS = ("index.html", "app.js", "style.css", "theme.js")

    def _panel_asset(self, name: str) -> bytes:
        if name not in self.PANEL_ASSETS:
            return b"/* not found */"
        path = WEB_DIR / name
        try:
            return path.read_bytes()
        except OSError:
            return b"/* asset missing */"

    def _panel_get(self, route: str) -> None:
        gateway = self.gateway
        try:
            if route == "overview":
                self._send_json(gateway.panel_overview())
            elif route == "accounts":
                accounts = gateway.pool.list()
                shares = gateway.pool.shares()
                for account in accounts:
                    account["share"] = shares.get(account["uid"])
                self._send_json({"accounts": accounts,
                                 "pool": gateway.pool.stats()})
            elif route == "journal":
                self._send_json({"entries": gateway.journal.recent(limit=200),
                                 "stats": gateway.journal.counters()})
            elif route == "errors":
                self._send_json({"errors": gateway.journal.error_summary()})
            elif route == "settings":
                self._send_json(gateway.panel_settings())
            else:
                self._send_json({"error": {"message": f"unknown panel route {route}",
                                           "type": "invalid_request_error"}}, 404)
        except Exception as error:  # noqa: BLE001
            gateway.log(f"panel error on {route}:\n{traceback.format_exc()}")
            self._send_json({"error": {"message": "internal error", "type": "internal_error"}}, 500)

    def _panel_post(self, route: str) -> None:
        gateway = self.gateway
        try:
            body = self._read_body()
        except ValueError as error:
            self._send_json({"error": {"message": str(error), "type": "invalid_request_error"}}, 400)
            return
        try:
            if route == "settings":
                result = gateway.panel_save_settings(body)
                if "error" in result:
                    self._send_json(result, 400)
                else:
                    self._send_json(result)
                return
            if route == "models/refresh":
                result = gateway.panel_refresh_models()
                self._send_json(result, 200 if result.get("ok") else 502)
                return
            if route == "check-upstream":
                result = gateway.panel_check_upstream()
                self._send_json(result, 200 if result.get("ok") else 502)
                return
            if route == "renew":
                result = gateway.renew_all()
                self._send_json(result, 200 if result.get("ok") else 502)
                return
            if route == "accounts/verify":
                result = gateway.panel_verify_account(body)
                self._send_json(result, 200 if result.get("ok") else 400)
                return
            if route == "accounts":
                result = gateway.panel_add_account(body)
                self._send_json(result, 200 if result.get("ok") else 400)
                return
            if route == "persona/apply":
                result = gateway.panel_apply_persona(body)
                self._send_json(result, 200 if result.get("ok") else 400)
                return
            if route.startswith("accounts/") and route.endswith("/label"):
                uid = route[len("accounts/"):-len("/label")]
                result = gateway.panel_set_label(uid, body.get("label"))
                self._send_json(result, 200 if result.get("ok") else 400)
                return
            if route.startswith("accounts/") and route.endswith("/weight"):
                uid = route[len("accounts/"):-len("/weight")]
                result = gateway.panel_set_weight(uid, body.get("weight"))
                self._send_json(result, 200 if result.get("ok") else 400)
                return
            if route.startswith("accounts/") and route.endswith("/delete"):
                uid = route[len("accounts/"):-len("/delete")]
                result = gateway.panel_delete_account(uid)
                self._send_json(result, 200 if result.get("ok") else 404)
                return
            if route.startswith("accounts/") and route.endswith("/enabled"):
                uid = route[len("accounts/"):-len("/enabled")]
                ok = gateway.pool.set_enabled(uid, bool(body.get("enabled")))
                self._send_json({"ok": ok}, 200 if ok else 404)
                return
            self._send_json({"error": {"message": f"unknown panel route {route}",
                                       "type": "invalid_request_error"}}, 404)
        except Exception as error:  # noqa: BLE001
            gateway.log(f"panel error on {route}:\n{traceback.format_exc()}")
            self._send_json({"error": {"message": "internal error", "type": "internal_error"}}, 500)

    @staticmethod
    def _drain(box: "queue.Queue[Any]") -> None:
        """Empty a full queue so a blocked producer can notice ``stop``."""
        while True:
            try:
                box.get_nowait()
            except queue.Empty:
                return

    def _client_gone(self) -> bool:
        """Whether the client has closed its side of the connection.

        ``Connection: close`` is always sent, so the only thing this socket can
        become readable for is an abort — a byte of pipelined request data is
        impossible after the body has been read. Peeking therefore answers the
        question without consuming anything.
        """
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
        except (OSError, ValueError):
            return True
        if not readable:
            return False
        try:
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True

    def _stream(self, body: Dict[str, Any]) -> None:
        """Stream SSE frames to the client.

        The upstream is pulled on a worker thread so this loop can notice a
        client abort **while waiting** for the next frame. Without that, a
        caller who pressed "stop" during a long upstream pause saw nothing
        happen until the next frame arrived — measured 2026-11-18: 71 seconds
        of silence, then WinError 10053 surfaced as an unhandled 500.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()

        gone = threading.Event()
        upstream = self.gateway.chat_completions_stream(body, gone)
        chunks: "queue.Queue[Any]" = queue.Queue(maxsize=64)
        stop = threading.Event()
        finished = object()

        def offer(item: Any) -> bool:
            """Hand something to the reader, giving up if it stopped reading.

            The reader returns as soon as the client disconnects, so a plain
            blocking ``put`` would park this thread forever once the queue
            fills — a thread (and an upstream socket) leaked per abort.
            """
            while not stop.is_set():
                try:
                    chunks.put(item, timeout=0.25)
                    return True
                except queue.Full:
                    continue
            return False

        def pump() -> None:
            try:
                for chunk in upstream:
                    if stop.is_set() or not offer(chunk):
                        break
            except BaseException as error:      # noqa: BLE001 - forwarded below
                if not stop.is_set():
                    offer(error)
            finally:
                # Closing here (same thread that drove the generator) is what
                # tears down the upstream socket promptly.
                try:
                    upstream.close()
                except (RuntimeError, ValueError):
                    pass
                offer(finished)

        worker = threading.Thread(target=pump, daemon=True)
        worker.start()

        last_write = time.time()
        try:
            while True:
                try:
                    item = chunks.get(timeout=1.0)
                except queue.Empty:
                    if self._client_gone():
                        stop.set()
                        gone.set()
                        self._drain(chunks)
                        self.gateway.log("client disconnected mid-stream")
                        return
                    # A caller with a long upstream pause still deserves to see
                    # the connection is alive; SSE comments are ignored by
                    # clients that do not understand them.
                    if time.time() - last_write >= 15:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        last_write = time.time()
                    continue
                if item is finished:
                    break
                if isinstance(item, BaseException):
                    raise item
                if "error" in item:
                    frame = f"data: {json.dumps(item['error'], ensure_ascii=False)}\n\n"
                else:
                    frame = f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
                last_write = time.time()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            stop.set()
            gone.set()
            self._drain(chunks)
            self.gateway.log("client disconnected mid-stream")
        except UpstreamError as error:
            stop.set()
            self.gateway.log(f"upstream error mid-stream: {error}")
            frame = {"error": {"message": str(error), "type": "upstream_error"}}
            try:
                self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except OSError:
                pass
        except OSError as error:
            # Windows reports a client abort as ConnectionAbortedError, but a
            # plain OSError can surface on other paths; neither is a server
            # fault and neither should become a 500.
            stop.set()
            gone.set()
            self.gateway.log(f"stream write failed, client likely gone: {error}")


def _weight_of(value: Any) -> int:
    """Clamp a weight into the 1..100 range the pool understands."""
    try:
        return max(1, min(int(value), 100))
    except (TypeError, ValueError):
        return 1


def _verify_bearer(headers, expected: str) -> bool:
    """Check an ``Authorization: Bearer`` header in constant time.

    An empty expected key means "no credential configured" and never
    authorises anything — an unauthenticated gateway would let anyone on
    the network spend the account's quota.
    """
    if not expected:
        return False
    header = headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    expected_digest = hashlib.sha256(expected.encode("utf-8")).digest()
    token_digest = hashlib.sha256(token.encode("utf-8")).digest()
    return hmac.compare_digest(expected_digest, token_digest)


def insecure_key_warning(listen: str, api_key: str, panel_password: str = "") -> str:
    """Warn when the gateway listens beyond loopback with a guessable key.

    The key protects the platform session cookie, so a guessable one on a
    shared network is as bad as no key at all. Mirrors workbuddy2api's
    ``cmd/server/main.go`` ``insecureKeyWarning``: it never blocks the
    start, it only says so loudly.
    """
    host = (listen or "").split(":")[0]
    loopback = host in ("", "127.0.0.1", "localhost", "::1", "[::1]")
    if loopback:
        return ""
    secrets = [key for key in (api_key, panel_password) if key]
    if not secrets:
        return ("no api_key is configured while listening on a public interface — "
                "anyone who can reach this port can use the account")
    weak = [key for key in secrets if len(key) < 16 or key.lower() in ("test_key", "changeme")]
    if weak:
        return (f"the key is short or a sample value ({', '.join(sorted(set(weak)))}) "
                f"while listening on {host} — anyone who can reach this port can use "
                f"the account")
    return ""


class BoundedHTTPServer(ThreadingHTTPServer):
    """A threading server that refuses to start more than N connections.

    ``ThreadingHTTPServer`` spawns a thread per connection with no ceiling,
    which turns a burst of connections into a thread exhaustion. The counter
    is a plain integer behind a lock: the accept path is not hot enough for
    the contention to matter, and a real limit is worth more than a lock-free
    approximation here.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, max_connections: int = 64, **kwargs):
        self._max_connections = max(4, int(max_connections))
        self._active = 0
        self._counter_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        with self._counter_lock:
            if self._active >= self._max_connections:
                self.shutdown_request(request)
                return
            self._active += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._counter_lock:
                self._active -= 1
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            # ``close_request`` runs after the handler is done; decrement
            # there instead so the count tracks live connections.
            pass

    def close_request(self, request):
        try:
            super().close_request(request)
        finally:
            with self._counter_lock:
                self._active = max(0, self._active - 1)


def serve(cfg, *, log: Optional[Logger] = None) -> None:
    """Run the gateway until interrupted."""
    gateway = Gateway(cfg, log)
    handler = type("BoundHandler", (Handler,), {"gateway": gateway})
    max_connections = int(getattr(cfg, "max_connections", 64) or 64)
    server = BoundedHTTPServer((cfg.host, cfg.port), handler, max_connections=max_connections)
    # config.json no longer holds credentials — accounts.json does. Those
    # fields survive only as a migration source, so an empty config beside a
    # populated account file is the normal state, not a problem.
    if not gateway.pool.list():
        gateway.log("no account configured yet — open the panel and add one "
                    "(贴一串 ai-poly 即可)")
    warning = insecure_key_warning(cfg.host, cfg.api_key, cfg.panel_password)
    if warning:
        gateway.log(f"WARNING: {warning}")
    gateway.log(f"polymas2api listening on http://{cfg.host}:{cfg.port}")
    gateway.log(f"panel: http://{cfg.host}:{cfg.port}/panel/")
    gateway.log(f"assistant app_code={gateway._effective_app_code() or '(未配置)'} "
                f"model={cfg.model or C.DEFAULT_MODEL}")
    gateway.log(f"outbound profile={cfg.fingerprint} origin={cfg.send_origin} "
               f"interval>={cfg.min_interval_ms}ms jitter={cfg.jitter_ms}ms")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        gateway.log("shutting down")
    finally:
        server.server_close()
