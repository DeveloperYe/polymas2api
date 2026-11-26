"""Configuration: one JSON file, no environment surprises.

Credentials are the two session cookies the platform hands out at login:

``ai-poly``
    HS256 JWT issued by the teaching centre. Payload::

        {"loginType":"login","loginId":"<userNid>","rnStr":"<random>",
         "type":"ZHS","userNid":"<userNid>"}

``jt-cas``
    ES256 JWT issued by the CAS service. Payload::

        {"iss":"com.zhihuishu","aud":"CAS","sub":"<name>",
         "iat":<epoch>,"exp":<epoch>,"uid":<uid>,"nid":"<userNid>"}

Either one alone authenticates every endpoint this gateway touches. Both are
accepted as ``Cookie`` *and* as a bare ``Authorization`` header (the platform
does not use the ``Bearer`` scheme).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

__all__ = ["Config", "ConfigError", "load_config", "write_example"]


class ConfigError(Exception):
    """Raised when the configuration file is missing or unusable."""


@dataclass
class Config:
    """Everything the gateway needs to talk to the platform."""

    # -- credentials (the single-account fallback) --------------------------
    ai_poly: str = ""
    jt_cas: str = ""
    user_nid: str = ""
    friend_nid: str = ""
    app_code: str = ""
    #: The model sent upstream when a request does not name one.
    #:
    #: "Auto" lets the platform resolve the agent's own row, which for 小真
    #: is deepseek-v4.1-flash. Naming a different modelCode really does
    #: switch the backing model (verified 2026-11-13: the assistant then
    #: introduces itself as a Qwen or a Doubao model), but it also drops
    #: the platform's persona guardrails, so the default stays "Auto".
    model: str = "Auto"
    biz_code: str = "hike_teach_center"
    model: str = ""

    # -- conversation behaviour --------------------------------------------
    chat_mode: str = "COMMON"
    act_mode: str = "AUTO"
    terminal: str = "STUDENT"
    opt_platform: str = "Web"
    #: Off by default: on, every conversation goes into the platform's
    #: long-term memory and can be recalled in a later session. A release
    #: handed to somebody else should not opt them into that (2026-11-18).
    retain_long_term_memory: bool = False

    #: Reasoning effort sent upstream. Empty means "do not send the field".
    #:
    #: Measured on the 电子工艺实习 agent (2026-11-13): "none" is honoured
    #: and switches the reasoning channel off completely — the REASONING
    #: SSE events stop. The other levels are accepted but their effect is
    #: not separable from the agent's own system prompt, which asks for the
    #: derivation to be written into the answer body.
    reasoning_effort: str = ""

    #: Whether to ask the platform to use the agent (its skills, knowledge
    #: base and course operations).
    #:
    #: Measured 2026-11-13: with ``useAgent: 0`` the reply collapses to a
    #: bare sentence and the reasoning channel stops — the agent's tools and
    #: persona are bypassed. It is on by default.
    use_agent: bool = True

    #: Whether the agent may retrieve from the web.
    web_retrieve: bool = True

    # -- server --------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8787
    api_key: str = ""
    panel_password: str = ""
    request_timeout: float = 120.0
    log_console: bool = True

    # -- anti-detection ------------------------------------------------------
    #: Outbound client profile: "edge-157", "chrome-131",
    #: "chrome-131-noorigin" or "random".
    fingerprint: str = "edge-157"
    #: Send an ``Origin`` header. The platform reflects it back, so sending
    #: one makes the request look like it came from the teaching centre.
    send_origin: bool = True
    #: Floor on the gap between two upstream calls, in milliseconds.
    min_interval_ms: int = 800
    #: Extra random delay added to every call, in milliseconds.
    jitter_ms: int = 700
    #: Delay proportional to the prompt length (ms per character).
    think_time_per_char_ms: float = 4.0
    #: Cap for the think-time delay, in milliseconds.
    think_time_cap_ms: int = 2500
    #: Names to scrub out of a reply, comma separated — usually the account
    #: holder's own name.
    #:
    #: The assistant does not *remember* the name: the platform looks the
    #: account up (``getLoginUserInfo`` → ``userName``) and hands it to the
    #: agent, which then opens with "林晓东，来了。". That value is not the
    #: client's to change, and it is a real name, so it is removed on the way
    #: out instead.
    #:
    #: Idle by default: the production account's ``userName`` is "用户", so
    #: there is nothing to remove. The capability stays for accounts whose
    #: display name is a real name.
    persona_scrub_names: str = ""

    #: Bridge the caller's OpenAI ``tools`` onto the assistant.
    #:
    #: The platform will not accept tool definitions on the wire — an agent's
    #: tools are bound server-side (``bindTools`` / ``ability-config``) — so
    #: the tools are described in the prompt and the answer is parsed back
    #: into ``tool_calls``. See :mod:`polymas2api.toolbridge`.
    #: Give every request its own upstream conversation.
    #:
    #: The platform keeps one shared conversation per (user, agent) when the
    #: request carries no ``metaData.sessionId``, so unrelated API calls can
    #: see each other's context (verified 2026-11-16). An OpenAI-compatible
    #: endpoint has to be stateless: the caller sends the whole history, and
    #: a fresh id per request keeps that promise. A caller that wants
    #: server-side continuity passes ``session_id``.
    session_isolation: bool = True
    #: Bridge the caller's OpenAI ``tools`` onto the assistant.
    tool_bridge: bool = True
    #: What name the model is asked to emit for a tool call.
    #:
    #: ``"verbatim"`` uses the caller's name unchanged — an English name
    #: comes back English, a Chinese one comes back Chinese. ``"ascii"``
    #: replaces a non-ASCII name with an ASCII stand-in (``执行命令`` →
    #: ``run_command``) so the wire format stays valid for strict OpenAI
    #: clients, which require ``^[a-zA-Z0-9_-]{1,64}$``.
    #:
    #: Measured 2026-11-16: the model echoes whatever name the schema gives
    #: it, it does not translate. So the simplest way to get English output
    #: is to declare English names; this switch is for a caller that cannot
    #: rename them.
    #: How many levels of nested tool parameters to show the model.
    #: Measured 2026-11-17: depth 2 costs +7% over flat and is complete for
    #: DSH's 112 tools; depth 0 hid every inner field. Raised to 3 on
    #: 2026-11-19: a tool nesting four deep lost its leaf fields at 2 (1/10
    #: filled correctly) and kept them at 3 (10/10), for +28 characters.
    tool_schema_depth: int = 3
    #: Write the caller's images to a local file and tell the model the path
    #: instead of dropping them. The platform cannot carry an image, and the
    #: agent's own tools run on this machine.
    local_image_attachments: bool = True
    tool_name_style: str = "verbatim"
    #: Retry once when a tool-enabled request comes back without a call.
    #:
    #: The assistant is a course representative: asked for something it
    #: "can do", it tends to answer "I'll send you a card" and never emit the
    #: CALL line. Measured 2026-11-16 with a realistic 112-tool client: about
    #: half the turns narrated instead of calling. A single corrective retry
    #: is far more reliable than any wording of the instruction.
    tool_retry: bool = True
    #: How many characters a tool-enabled stream may buffer before it is
    #: treated as ordinary prose and released.
    #:
    #: Below the limit the answer is held to the end, so a missing CALL line
    #: can still be corrected; above it the client gets tokens as usual.
    #: Gap between the SSE frames that carry a tool call, in milliseconds.
    #: Measured 2026-11-20: the tool_calls frames and the finish frame went out
    #: in the *same* millisecond, which is the shape a sibling gateway found the
    #: client drops — it lost a frame and ended up with an empty id or half a
    #: JSON argument. Eight milliseconds is what that project settled on.
    tool_frame_gap_ms: float = 8.0

    #: How long a stream may hold its HTTP headers while waiting for the first
    #: frame. A failure that arrives inside this window becomes a proper
    #: 502/503 instead of an error frame the client cannot see; anything slower
    #: falls back to headers-first streaming (measured 2026-11-26: the first
    #: frame arrives at p50 4.0 s, p95 5.5 s, so five seconds is free for
    #: ordinary turns and still catches immediate failures).
    header_delay_seconds: float = 5.0

    #: ``auto`` counts with the vendors' vocabularies when they are installed
    #: and present, ``estimate`` never does. See :mod:`polymas2api.tokencount`.
    token_counting: str = "auto"

    #: Where those vocabularies live (``deepseek.json`` / ``qwen.json`` /
    #: ``doubao.json``), relative to the working directory.
    tokenizers_dir: str = "models/tokenizers"
    tool_stream_buffer_chars: int = 240

    #: How long a held answer may stay invisible before it is released anyway.
    #:
    #: The hold exists so a "claimed an action, never called one" answer can be
    #: retried; keeping it costs the client every token until the generation
    #: ends. Measured 2026-11-19: a 57-second answer that had nothing to stream
    #: showed the user a blank panel, and 72% of that day's answers were short
    #: enough to be held. Past this deadline the answer streams and the retry
    #: is given up - a slow answer is better seen than retried unseen.
    tool_stream_hold_seconds: float = 8.0
    #: Report the agent's *own* tool calls (``TOOL_REQUEST`` / ``TOOL_RESPONSE``)
    #: in a ``polymas.upstream_tools`` field.
    #:
    #: Those tools already ran on the platform, so they are never turned into
    #: ``tool_calls``: a client must not be asked to execute a tool it has
    #: never heard of.
    expose_upstream_tools: bool = True

    #: Body fingerprint scope: "all", "identity" or "off".
    #:
    #: Measured against the live platform (2026-11-12): the upstream does
    #: **not** block verbatim third-party client signatures. A request
    #: carrying "You are powered by DeepSeek Harness", the harness's @-path
    #: paragraph, the Claude Code identity sentence or a bare "11-128" all
    #: answered 200 with normal content. The rule set exists because
    #: workbuddy2api needs it, not because polymas does, so the default is
    #: "off" — switching it on is harmless but pointless.
    sanitize_scope: str = "off"
    sanitize_enabled: bool = False

    #: Minutes between automatic platform-token renewals. 0 disables it.
    #:
    #: The platform token carries no expiry claim, so this is a precaution
    #: rather than a countdown. Renewing well before the platform would
    #: refuse the credential is what keeps the gateway alive.
    renew_interval_minutes: int = 360

    # -- pool ----------------------------------------------------------------
    #: Weighted rotation across accounts.
    pool_enabled: bool = True

    # -- journal / archive ---------------------------------------------------
    journal_size: int = 500
    archive_enabled: bool = False
    archive_max_mb: int = 20

    extra: Dict[str, Any] = field(default_factory=dict)

    # -- derived -------------------------------------------------------------
    @property
    def credential(self) -> str:
        """The credential sent to the platform: ``ai-poly`` wins, then ``jt-cas``."""
        return self.ai_poly or self.jt_cas

    def path(self, key: str, default: str) -> Path:
        """Resolve a path relative to the config file's directory."""
        base = Path(self.extra.get("_base_dir", "."))
        value = self.extra.get(key, default)
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (base / candidate)

    @property
    def archive_file(self) -> str:
        return str(self.extra.get("archive_file", "logs/requests.jsonl"))

    @property
    def accounts_file(self) -> str:
        return str(self.extra.get("accounts_file", "accounts.json"))

    @property
    def log_dir(self) -> str:
        return str(self.extra.get("log_dir", "logs"))


def load_config(path: str) -> Config:
    """Read a JSON config file into a :class:`Config`.

    Keys are snake_case and mirror the attribute names. Unknown keys are kept
    in ``extra`` so that a config file can also carry paths.

    When neither ``api_key`` nor ``panel_password`` is set, a random key is
    generated and written back to the file. An unauthenticated gateway on a
    shared network would let anyone spend the account's quota; this mirrors
    workbuddy2api's ``cmd/server/config.go`` first-run behaviour.
    """
    file = Path(path)
    if not file.exists():
        raise ConfigError(f"Config file not found: {file}")
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"Config file is not valid JSON: {file}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file must contain a JSON object: {file}")
    # A starter config documents each field with a <placeholder>. Taking those
    # literally locked the panel behind a key that was the placeholder text
    # itself (measured 2026-11-18), so anything shaped like one is dropped and
    # the field keeps its default.
    for key in [k for k, v in raw.items()
                if isinstance(v, str) and v.startswith("<") and v.endswith(">")]:
        raw.pop(key)

    known = {
        "ai_poly", "jt_cas", "user_nid", "friend_nid", "app_code", "biz_code",
        "model", "chat_mode", "act_mode", "terminal", "opt_platform",
        "retain_long_term_memory", "reasoning_effort", "use_agent",
        "web_retrieve", "host", "port", "api_key", "panel_password",
        "request_timeout", "log_console", "fingerprint", "send_origin",
        "min_interval_ms", "jitter_ms", "think_time_per_char_ms",
        "think_time_cap_ms", "sanitize_scope", "sanitize_enabled",
        "pool_enabled", "journal_size", "archive_enabled", "archive_max_mb",
        
    "persona_scrub_names",
    "tool_bridge", "expose_upstream_tools", "session_isolation",
    "header_delay_seconds", "token_counting", "tokenizers_dir", "tool_frame_gap_ms", "tool_name_style", "tool_schema_depth", "tool_retry",
        "tool_stream_buffer_chars",
        "tool_stream_hold_seconds",
        "renew_interval_minutes",
    }
    kwargs = {key: raw[key] for key in known if key in raw}
    extra = {key: value for key, value in raw.items() if key not in known}
    extra["_base_dir"] = str(file.parent.resolve())

    config = Config(**kwargs)
    config.extra.update(extra)
    config.extra["_config_path"] = str(file.resolve())
    if not config.app_code:
        config.app_code = config.friend_nid

    if not config.api_key:
        # An unauthenticated /v1/* would let anyone on the network spend the
        # account's quota, so a key is generated even when a panel password
        # is configured.
        generated = _generate_key()
        config.api_key = generated
        raw["api_key"] = generated
        try:
            file.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _restrict(file)
        except OSError:
            pass
        config.extra["_generated_key"] = True

    # Credentials are in this file; tighten its permissions on every start,
    # not only when a key happens to be generated.
    _restrict(file)
    accounts = config.extra.get("accounts_file")
    if accounts:
        candidate = Path(accounts)
        if not candidate.is_absolute():
            candidate = file.parent / candidate
        if candidate.exists():
            _restrict(candidate)
    return config


def _generate_key() -> str:
    """A random Bearer key, prefixed so it is recognisable in a config file."""
    import secrets
    return "sk-" + secrets.token_urlsafe(24)


def _restrict(path: Path) -> None:
    """Best-effort permission tightening for a file that holds secrets.

    On Windows the file gets an explicit ACL: the owning user plus
    SYSTEM and the local Administrators group. Dropping SYSTEM and
    Administrators would leave the file unreadable to any tooling that
    runs elevated, so they are granted read access explicitly instead of
    being removed with the inherited entries.
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    try:
        import subprocess
        account = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        full = f"{domain}\\{account}" if domain else account
        subprocess.run(
            ["icacls", str(path), "/inheritance:r",
             "/grant:r", f"{full}:(R,W)",
             "/grant:r", "SYSTEM:(R)",
             "/grant:r", "BUILTIN\\Administrators:(R)"],
            capture_output=True, check=False,
        )
    except Exception:  # noqa: BLE001
        pass


def write_example(path: str) -> None:
    """Write a starter config.

    Settings only. Credentials live in ``accounts.json`` and are added from the
    panel, so a fresh install has nothing secret in this file — which is what a
    release handed to somebody else should look like.
    """
    example = {
        "model": "<defaults to the agent's configured model>",
        "host": "127.0.0.1",
        "port": 8787,
        "api_key": "<optional; clients must send it when set>",
        "panel_password": "<optional; without it the panel is only reachable from localhost>",
        "fingerprint": "edge-157",
        "send_origin": True,
        "min_interval_ms": 800,
        "jitter_ms": 700,
        "sanitize_scope": "all",
        "accounts_file": "accounts.json",
        "archive_file": "logs/requests.jsonl",
        "log_dir": "logs",
    }
    Path(path).write_text(json.dumps(example, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
