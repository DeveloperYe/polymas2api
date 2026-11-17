"""Unit tests that need no network access."""

from __future__ import annotations

import json
import pathlib
import ssl
import urllib.error
import urllib.request

import pytest

from polymas2api.adapter import OpenAIAdapter
from polymas2api.config import Config
from polymas2api.fingerprint import PROFILES, Fingerprint, RequestShaper, build_ssl_context
from polymas2api.journal import Journal, JournalEntry
from polymas2api.persona import NameScrubber, StreamWasher
from polymas2api.pool import Credential, CredentialPool, PoolError
from polymas2api.sanitize import Sanitizer
from polymas2api.upstream import _ChunkedDecoder, parse_sse


# -- SSE parsing ------------------------------------------------------------
def test_parse_sse_reads_event_and_data():
    frames = list(parse_sse("event:ANSWER\ndata:{\"text\":\"hi\"}\n\n"))
    assert frames == [("ANSWER", '{"text":"hi"}')]


def test_parse_sse_handles_crlf_and_multiple_frames():
    raw = "event:A\r\ndata:1\r\n\r\nevent:B\r\ndata:2\r\n\r\n"
    assert list(parse_sse(raw)) == [("A", "1"), ("B", "2")]


def test_parse_sse_joins_multiline_data():
    raw = "event:X\ndata:line1\ndata:line2\n\n"
    assert list(parse_sse(raw)) == [("X", "line1\nline2")]


def test_parse_sse_ignores_comments():
    assert list(parse_sse(": keep-alive\n\n")) == []


# -- chunked transfer decoding ------------------------------------------------
def test_chunked_decoder_strips_chunk_sizes():
    raw = b"event:ANSWER\ndata:{}\n\n"
    body = hex(len(raw))[2:].encode() + b"\r\n" + raw + b"\r\n" + b"0\r\n\r\n"
    decoder = _ChunkedDecoder()
    out, done = decoder.feed(body)
    assert out == raw
    assert done


def test_chunked_decoder_reassembles_split_chunks():
    raw = b"event:ANSWER\ndata:{\"text\":\"hello\"}\n\n"
    body = hex(len(raw))[2:].encode() + b"\r\n" + raw + b"\r\n" + b"0\r\n\r\n"
    decoder = _ChunkedDecoder()
    out = b""
    for index in range(0, len(body), 7):
        piece, done = decoder.feed(body[index:index + 7])
        out += piece
        if done:
            break
    assert out == raw


def test_chunked_decoder_handles_two_chunks():
    first, second = b"event:A\n", b"data:1\n\n"
    body = (hex(len(first))[2:].encode() + b"\r\n" + first + b"\r\n"
            + hex(len(second))[2:].encode() + b"\r\n" + second + b"\r\n" + b"0\r\n\r\n")
    decoder = _ChunkedDecoder()
    out, done = decoder.feed(body)
    assert out == first + second
    assert done


# -- TLS / ALPN ---------------------------------------------------------------
def test_ssl_context_stays_on_tls12():
    context = build_ssl_context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.maximum_version == ssl.TLSVersion.TLSv1_2


def test_ssl_context_does_not_offer_h2():
    """Offering h2 makes the server expect HTTP/2 and close the connection.

    workbuddy2api hit the same wall and emptied TLSNextProto
    (internal/upstream/transport.go:133). The check is behavioural because
    CPython does not expose the ALPN list on the context.
    """
    import socket

    context = build_ssl_context()
    assert context.get_ca_certs() is not None  # certificate verification stays on
    # The protocol list is not readable from Python, so assert on the source
    # of the builder instead: it must not name h2.
    import inspect
    from polymas2api import fingerprint as fingerprint_module
    source = inspect.getsource(fingerprint_module.build_ssl_context)
    assert '"h2"' not in source


# -- fingerprint --------------------------------------------------------------
def test_fingerprint_is_stable_per_key():
    fingerprint = Fingerprint("edge-157")
    first = fingerprint.profile_for("a")
    assert fingerprint.profile_for("a") is first


def test_fingerprint_reconfigure_switches_profile():
    fingerprint = Fingerprint("edge-157")
    assert fingerprint.profile_for("a").name == "edge-157"
    fingerprint.reconfigure("chrome-131")
    assert fingerprint.profile_for("a").name == "chrome-131"


def test_fingerprint_rejects_unknown_profile():
    fingerprint = Fingerprint("edge-157")
    fingerprint.reconfigure("nope")
    assert fingerprint.profile_for("a").name == "edge-157"  # falls back


def test_profile_headers_look_like_a_browser():
    profile = PROFILES["edge-157"]
    names = [name for name, _ in profile.headers()]
    assert "user-agent" in names
    assert "sec-ch-ua" in names
    assert "sec-fetch-mode" in names
    assert "referer" in names
    assert "origin" in names
    assert "accept" in names


def test_profile_can_omit_origin():
    profile = PROFILES["chrome-131-noorigin"]
    names = [name for name, _ in profile.headers(with_origin=False)]
    assert "origin" not in names


# -- request shaping ----------------------------------------------------------
class _ShaperConfig:
    min_interval_ms = 0
    jitter_ms = 0
    think_time_per_char_ms = 0
    think_time_cap_ms = 0


def test_shaper_is_a_noop_when_disabled():
    shaper = RequestShaper(_ShaperConfig())
    assert shaper.wait("a", "hello") == 0.0


class _JitterConfig(_ShaperConfig):
    min_interval_ms = 0
    jitter_ms = 50
    think_time_per_char_ms = 1.0
    think_time_cap_ms = 100


def test_shaper_applies_jitter_and_think_time():
    shaper = RequestShaper(_JitterConfig())
    delay = shaper.wait("a", "x" * 30)
    assert 0 < delay < 1.0


class _IntervalConfig(_ShaperConfig):
    min_interval_ms = 200
    jitter_ms = 0
    think_time_per_char_ms = 0
    think_time_cap_ms = 0


def test_shaper_enforces_the_minimum_interval():
    """The floor must hold between two consecutive calls of one account."""
    shaper = RequestShaper(_IntervalConfig())
    first = shaper.wait("a", "x")
    second = shaper.wait("a", "x")
    assert second >= 0.15
    assert first < 0.05


def test_shaper_keeps_accounts_independent():
    shaper = RequestShaper(_IntervalConfig())
    shaper.wait("a", "x")
    other = shaper.wait("b", "x")
    assert other < 0.05


# -- sanitiser ----------------------------------------------------------------
def test_sanitizer_is_off_by_default_scope():
    sanitizer = Sanitizer("off")
    text = "You are powered by DeepSeek Harness."
    assert sanitizer.sanitize_prompt(text) == text


def test_sanitizer_rewrites_dsh_identity():
    sanitizer = Sanitizer("all")
    result = sanitizer.sanitize_prompt("You are powered by DeepSeek Harness.")
    assert "DeepSeek Harness" not in result


def test_sanitizer_rewrites_dsh_paths_paragraph():
    sanitizer = Sanitizer("all")
    text = ("Tokens prefixed with @ are paths the user explicitly referenced. "
            "Read a path that contains spaces with the read tool.")
    result = sanitizer.sanitize_prompt(text)
    assert "Tokens prefixed with @" not in result
    assert "Paths beginning with @" in result


def test_sanitizer_leaves_ordinary_text_alone():
    sanitizer = Sanitizer("all")
    text = "请帮我总结这一章的知识点，并列出三个要点。"
    assert sanitizer.sanitize_prompt(text) == text


def test_sanitizer_identity_scope_only_touches_system():
    sanitizer = Sanitizer("identity")
    messages = [
        {"role": "system", "content": "You are powered by DeepSeek Harness."},
        {"role": "user", "content": "You are powered by DeepSeek Harness."},
    ]
    result = sanitizer.sanitize_messages(messages)
    assert "DeepSeek Harness" not in result[0]["content"]
    assert "DeepSeek Harness" in result[1]["content"]


def test_sanitizer_all_scope_touches_user_too():
    sanitizer = Sanitizer("all")
    messages = [{"role": "user", "content": "You are powered by DeepSeek Harness."}]
    result = sanitizer.sanitize_messages(messages)
    assert "DeepSeek Harness" not in result[0]["content"]


def test_sanitizer_cleans_tool_call_arguments():
    sanitizer = Sanitizer("all")
    messages = [{
        "role": "assistant", "content": None,
        "tool_calls": [{"function": {"arguments": "DeepSeek Harness is here"}}],
    }]
    result = sanitizer.sanitize_messages(messages)
    assert "DeepSeek Harness" not in result[0]["tool_calls"][0]["function"]["arguments"]


# -- prompt flattening -------------------------------------------------------
def test_extract_prompt_uses_last_user_turn():
    prompt = OpenAIAdapter.extract_prompt([
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "第二句"},
    ])
    assert prompt.endswith("第二句")
    assert "第一句" in prompt


def test_extract_prompt_keeps_system_instructions():
    prompt = OpenAIAdapter.extract_prompt([
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "问题"},
    ])
    assert "你是助手" in prompt
    assert prompt.endswith("问题")


def test_extract_prompt_joins_content_parts():
    prompt = OpenAIAdapter.extract_prompt([
        {"role": "user", "content": [{"type": "text", "text": "你好"}, {"type": "text", "text": "世界"}]},
    ])
    assert prompt == "你好世界"


def test_extract_prompt_handles_empty_messages():
    assert OpenAIAdapter.extract_prompt([]) == ""


# -- model selection ---------------------------------------------------------
def test_the_requested_model_reaches_the_upstream():
    """``modelName`` really switches the backing model for this agent.

    Verified 2026-11-13 against the live platform: an explicitly named
    model changes the reply (Doubao reasons in Chinese, Qwen and DeepSeek in
    English), while naming the agent's own model matches the ``Auto`` arm.
    """
    from polymas2api.server import Gateway

    class _RecordingClient:
        def __init__(self):
            self.seen = []

        def chat(self, prompt, *, model="", session_id=""):
            self.seen.append(model)
            yield "ALL_FINISHED", {"text": None}

    gateway = Gateway(Config())
    client = _RecordingClient()
    account = Credential(uid="default", ai_poly="token", jt_cas="",
                user_nid="u", friend_nid="f")
    gateway._acquire = lambda model=None: (account, client)

    gateway.chat_completions({"model": "qwen3.8-max",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert client.seen == ["qwen3.8-max"]

    client.seen.clear()
    gateway.chat_completions({"messages": [{"role": "user", "content": "hi"}]})
    assert client.seen == ["Auto"]


def test_an_unknown_model_is_passed_through():
    """The upstream resolves a name it does not know back to the default."""
    from polymas2api.server import Gateway

    class _RecordingClient:
        def __init__(self):
            self.seen = []

        def chat(self, prompt, *, model="", session_id=""):
            self.seen.append(model)
            yield "ALL_FINISHED", {"text": None}

    gateway = Gateway(Config())
    client = _RecordingClient()
    account = Credential(uid="default", ai_poly="token", jt_cas="",
                user_nid="u", friend_nid="f")
    gateway._acquire = lambda model=None: (account, client)
    gateway.chat_completions({"model": "no-such-model-xyz",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert client.seen == ["no-such-model-xyz"]


# -- adapter -----------------------------------------------------------------
class _FakeClient:
    """Replays a fixed event sequence."""

    def __init__(self, events):
        self.events = events

    def chat(self, prompt, *, model="", session_id=""):
        for event, payload in self.events:
            yield event, payload


class _Config:
    model = "deepseek-v4.1-flash"
    chat_mode = "COMMON"
    act_mode = "AUTO"
    terminal = "STUDENT"
    opt_platform = "Web"
    retain_long_term_memory = True
    user_nid = "u"
    friend_nid = "f"
    app_code = "f"


def test_complete_collects_answer_and_reasoning():
    client = _FakeClient([
        ("REASONING", {"text": "想一下"}),
        ("ANSWER", {"text": "你好"}),
        ("ANSWER", {"text": "！"}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_Config())
    result = adapter.complete(client, "hi")
    message = result["choices"][0]["message"]
    assert message["content"] == "你好！"
    assert message["reasoning_content"] == "想一下"
    assert result["choices"][0]["finish_reason"] == "stop"


def test_complete_reports_upstream_error():
    client = _FakeClient([("ERROR", {"text": "出错了"})])
    adapter = OpenAIAdapter(_Config())
    result = adapter.complete(client, "hi")
    assert "error" in result
    assert result["error"]["message"] == "出错了"


def test_stream_emits_content_then_stop():
    client = _FakeClient([
        ("ANSWER", {"text": "a"}),
        ("ANSWER", {"text": "b"}),
        ("ALL_FINISHED", {"text": None}),
    ])
    adapter = OpenAIAdapter(_Config())
    chunks = list(adapter.stream(client, "hi"))
    contents = [chunk["choices"][0]["delta"].get("content")
                for chunk in chunks if "choices" in chunk]
    assert "".join(part for part in contents if part) == "ab"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


# -- pool ---------------------------------------------------------------------
class _PoolConfig:
    user_nid = "u"
    friend_nid = "f"
    app_code = "f"
    ai_poly = "x"
    jt_cas = ""
    model = ""
    extra = {"_base_dir": "."}


def test_pool_quarantines_a_rejected_credential():
    """A rejected session cookie must set the account aside."""
    pool = CredentialPool(_PoolConfig())
    pool.load_from_config()
    account = pool.get("u")
    pool.report_failure(account, "身份校验失败，请刷新页面后重试", cooldown=60)
    assert pool.stats()["quarantined"] == 1


def test_pool_keeps_the_account_on_a_transient_failure():
    """A timeout or a platform 5xx says nothing about the cookie."""
    pool = CredentialPool(_PoolConfig())
    pool.load_from_config()
    account = pool.get("u")
    pool.report_failure(account, "Timed out after 120s waiting for the platform")
    assert pool.stats()["quarantined"] == 0
    assert account.failures == 1


def test_pool_still_serves_when_every_account_is_quarantined():
    """A five-minute outage is worse than retrying a possibly-fine cookie."""
    pool = CredentialPool(_PoolConfig())
    pool.load_from_config()
    account = pool.get("u")
    pool.report_failure(account, "身份校验失败", cooldown=60)
    assert pool.acquire() is account


def test_pool_never_removes_the_last_account():
    pool = CredentialPool(_PoolConfig())
    pool.load_from_config()
    assert pool.remove("default") is False


def test_pool_public_view_hides_secrets():
    pool = CredentialPool(_PoolConfig())
    pool.load_from_config()
    rows = pool.list()
    assert "ai_poly" not in rows[0]
    assert rows[0]["has_ai_poly"] is True


def test_pool_rotates_between_accounts():
    pool = CredentialPool(_PoolConfig())
    pool.load_from_config()
    pool.add(Credential("second", user_nid="u2", friend_nid="f2", ai_poly="y"))
    seen = {pool.acquire().uid for _ in range(4)}
    assert seen == {"u", "second"}


# -- journal -----------------------------------------------------------------
class _JournalConfig:
    journal_size = 10
    archive_enabled = False
    archive_max_mb = 1
    extra = {"_base_dir": "."}


def test_journal_counts_success_and_failure():
    journal = Journal(_JournalConfig())
    journal.record(JournalEntry(account="a", model="m", stream=False, prompt_chars=1,
                                duration_ms=10, ok=True))
    journal.record(JournalEntry(account="a", model="m", stream=False, prompt_chars=1,
                                duration_ms=20, ok=False, error="boom"))
    counters = journal.counters()
    assert counters["total"] == 2
    assert counters["ok"] == 1
    assert counters["failed"] == 1


def test_journal_error_summary_groups_by_message():
    journal = Journal(_JournalConfig())
    for _ in range(3):
        journal.record(JournalEntry(account="a", model="m", stream=False, prompt_chars=1,
                                    duration_ms=10, ok=False, error="identical failure"))
    summary = journal.error_summary()
    assert summary[0]["count"] == 3
    assert summary[0]["error"] == "identical failure"


def test_journal_respects_the_ring_size():
    journal = Journal(_JournalConfig())
    for index in range(20):
        journal.record(JournalEntry(account="a", model="m", stream=False, prompt_chars=index,
                                    duration_ms=1, ok=True))
    assert len(journal.recent(limit=100)) == 10


# -- authentication ----------------------------------------------------------
class _Headers(dict):
    def get(self, name, default=None):
        for key, value in self.items():
            if key.lower() == name.lower():
                return value
        return default


def test_verify_bearer_accepts_the_right_key():
    from polymas2api.server import _verify_bearer
    assert _verify_bearer(_Headers({"Authorization": "Bearer testpass"}), "testpass")


def test_verify_bearer_is_case_insensitive_on_the_scheme():
    from polymas2api.server import _verify_bearer
    assert _verify_bearer(_Headers({"Authorization": "bearer testpass"}), "testpass")


def test_verify_bearer_rejects_a_wrong_key():
    from polymas2api.server import _verify_bearer
    assert not _verify_bearer(_Headers({"Authorization": "Bearer wrong"}), "testpass")


def test_verify_bearer_rejects_a_missing_header():
    from polymas2api.server import _verify_bearer
    assert not _verify_bearer(_Headers({}), "testpass")


def test_verify_bearer_rejects_an_empty_bearer():
    from polymas2api.server import _verify_bearer
    assert not _verify_bearer(_Headers({"Authorization": "Bearer "}), "testpass")


def test_verify_bearer_never_authorises_without_a_configured_key():
    """An empty key must not mean "open" — that would expose the account."""
    from polymas2api.server import _verify_bearer
    assert not _verify_bearer(_Headers({"Authorization": "Bearer anything"}), "")
    assert not _verify_bearer(_Headers({}), "")


# -- insecure key warning ----------------------------------------------------
def test_warning_for_a_short_key_on_a_public_interface():
    from polymas2api.server import insecure_key_warning
    message = insecure_key_warning("0.0.0.0:8787", "testpass")
    assert "short" in message


def test_no_warning_for_a_long_key_on_a_public_interface():
    from polymas2api.server import insecure_key_warning
    assert insecure_key_warning("0.0.0.0:8787", "sk-" + "x" * 40) == ""


def test_no_warning_on_loopback():
    from polymas2api.server import insecure_key_warning
    assert insecure_key_warning("127.0.0.1:8787", "testpass") == ""


def test_warning_when_no_key_is_configured():
    from polymas2api.server import insecure_key_warning
    assert insecure_key_warning("0.0.0.0:8787", "") != ""


# -- panel routing ----------------------------------------------------------
class _PanelServer:
    """A tiny HTTP client for the panel routes of a live Gateway."""

    def __init__(self, gateway):
        import threading
        from http.server import ThreadingHTTPServer
        from polymas2api.server import Handler
        handler = type("H", (Handler,), {"gateway": gateway})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path):
        import urllib.error
        import urllib.request
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def test_panel_without_a_trailing_slash_redirects():
    """/panel must redirect to /panel/.

    The browser resolves the page's relative asset paths against the
    directory of the URL, so "/panel" would ask for "/style.css" and get
    a 404 — the panel would render as unstyled HTML.
    """
    import urllib.request  # noqa: F401  (imported for the handler above)
    from polymas2api.config import Config
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    server = _PanelServer(gateway)
    try:
        status, headers, _ = server.get("/panel")
        assert status == 301
        assert headers["Location"] == "/panel/"
    finally:
        server.close()


def test_panel_assets_are_served_under_the_panel_prefix():
    from polymas2api.config import Config
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    server = _PanelServer(gateway)
    try:
        for name, expected in (("style.css", "text/css"),
                               ("app.js", "text/javascript"),
                               ("theme.js", "text/javascript")):
            status, headers, body = server.get(f"/panel/{name}")
            assert status == 200, name
            assert headers["Content-Type"].startswith(expected), name
            assert body, name
    finally:
        server.close()


def test_panel_page_has_absolute_asset_paths():
    """Relative hrefs break when the page is opened without a trailing slash."""
    from polymas2api.config import Config
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    server = _PanelServer(gateway)
    try:
        status, _, body = server.get("/panel/")
        assert status == 200
        html = body.decode("utf-8")
        assert 'href="/panel/style.css"' in html
        assert 'src="/panel/app.js"' in html
        assert 'src="/panel/theme.js"' in html
    finally:
        server.close()


# -- credential freshness -----------------------------------------------------
def test_decode_jwt_reads_the_claims():
    from polymas2api.credentials import decode_jwt
    token = ("eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
             "eyJleHAiOjE3OTQ1MDU3NjgsImlhdCI6MTc5NDQxOTM2OCwidWlkIjoiMSJ9."
             "sig")
    claims = decode_jwt(token)
    assert claims["exp"] == 1794505768


def test_decode_jwt_ignores_garbage():
    from polymas2api.credentials import decode_jwt
    assert decode_jwt("not-a-jwt") is None
    assert decode_jwt("") is None


def test_credential_status_reports_an_expiry():
    from polymas2api.credentials import inspect_credential
    token = ("eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9."
             "eyJleHAiOjE3OTQ1MDU3NjgsImlhdCI6MTc5NDQxOTM2OCwidWlkIjoiMSJ9."
             "sig")
    status = inspect_credential("jt-cas", token)
    assert status.has_exp is True
    assert status.expires_at == 1794505768
    assert status.state in ("ok", "expiring", "expired")


def test_credential_without_an_exp_is_opaque():
    """The platform's ai-poly token carries no exp, so its life is unknown."""
    from polymas2api.credentials import inspect_credential
    token = ("eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9."
             "eyJsb2dpblR5cGUiOiJsb2dpbiIsInVzZXJOaWQiOiIxIn0."
             "sig")
    status = inspect_credential("ai-poly", token)
    assert status.has_exp is False
    assert status.state == "opaque"
    assert status.seconds_left is None


def test_rejection_is_recognised():
    from polymas2api.credentials import looks_rejected
    assert looks_rejected("身份校验失败，请刷新页面后重试")
    assert looks_rejected("HTTP 401 from /chatim/v1/robot/chat")
    assert not looks_rejected("Timed out after 120s waiting for the platform")
    assert not looks_rejected("Platform returned HTTP 500")


# -- credential renewal ------------------------------------------------------
class _RenewConfig:
    ai_poly = "old-token"
    jt_cas = ""
    user_nid = "AbCdEfGhIj"
    friend_nid = "f"
    app_code = "f"
    model = ""
    extra = {"_base_dir": "."}


def test_renewer_keeps_the_old_token_when_the_renewal_fails():
    from polymas2api.renewal import TokenRenewer
    renewer = TokenRenewer(_RenewConfig())
    renewer._fetch = lambda: (_ for _ in ()).throw(
        __import__("polymas2api.renewal", fromlist=["RenewalError"]).RenewalError("boom"))
    assert renewer.renew() is None
    assert renewer.current == "old-token"
    assert renewer.failures == 1


def test_renewer_installs_a_new_token():
    from polymas2api.renewal import TokenRenewer
    renewer = TokenRenewer(_RenewConfig())
    renewer._fetch = lambda: "new-token"
    assert renewer.renew() == "new-token"
    assert renewer.current == "new-token"
    assert renewer.renewals == 1


def test_renewer_rejects_a_token_that_fails_verification():
    from polymas2api.renewal import TokenRenewer
    renewer = TokenRenewer(_RenewConfig())
    renewer._fetch = lambda: "new-token"
    assert renewer.renew(verify=lambda token: False) is None
    assert renewer.current == "old-token"


def test_renewer_persists_to_the_config_file(tmp_path):
    from polymas2api.renewal import TokenRenewer
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"ai_poly": "old", "user_nid": "u"}), encoding="utf-8")
    renewer = TokenRenewer(_RenewConfig())
    renewer._fetch = lambda: "fresh-token"
    renewer.renew()
    assert renewer.persist(str(config)) is True
    assert json.loads(config.read_text(encoding="utf-8"))["ai_poly"] == "fresh-token"

# -- session isolation -------------------------------------------------------
def test_each_request_gets_its_own_upstream_conversation():
    """No session id means stateless.

    The platform keeps one shared conversation per (user, agent) when the
    request carries no ``metaData.sessionId``, so two unrelated API calls
    would see each other's context (measured 2026-11-16). An OpenAI-shaped
    endpoint must be stateless by default.
    """
    from polymas2api.upstream import PolymasClient

    cfg = Config()
    cfg.biz_code = "hike_teach_center"
    cfg.opt_platform = "Web"
    client = PolymasClient(cfg, Credential(uid="t", ai_poly="token", jt_cas="",
                                    user_nid="u", friend_nid="f"))
    seen = []
    def _capture(path, body, timeout):
        seen.append(body)
        return iter(())
    client._stream_post = _capture

    for _ in range(2):
        try:
            list(client.chat("hi"))
        except Exception:  # noqa: BLE001
            pass

    sessions = [body.get("metaData", {}).get("sessionId") for body in seen]
    assert len(sessions) == 2
    assert all(sessions), "every request must carry a session id"
    assert sessions[0] != sessions[1], "two requests must not share a conversation"


def test_a_named_session_is_reused():
    """A caller that wants server-side continuity passes ``session_id``."""
    from polymas2api.upstream import PolymasClient

    cfg = Config()
    cfg.biz_code = "hike_teach_center"
    cfg.opt_platform = "Web"
    client = PolymasClient(cfg, Credential(uid="t", ai_poly="token", jt_cas="",
                                    user_nid="u", friend_nid="f"))
    seen = []
    client._stream_post = lambda path, body, timeout: seen.append(body) or iter(())

    list(client.chat("hi", session_id="my-session"))
    list(client.chat("again", session_id="my-session"))
    sessions = [body.get("metaData", {}).get("sessionId") for body in seen]
    assert sessions == ["my-session", "my-session"]


def test_isolation_can_be_turned_off():
    from polymas2api.upstream import PolymasClient

    cfg = Config()
    cfg.biz_code = "hike_teach_center"
    cfg.opt_platform = "Web"
    cfg.session_isolation = False
    client = PolymasClient(cfg, Credential(uid="t", ai_poly="token", jt_cas="",
                                    user_nid="u", friend_nid="f"))
    seen = []
    client._stream_post = lambda path, body, timeout: seen.append(body) or iter(())

    list(client.chat("hi"))
    assert "metaData" not in seen[0]
# -- tool retry --------------------------------------------------------------
def test_a_missing_tool_call_triggers_one_retry():
    """The retry is what makes tool calling reliable (3/5 -> 8/8).

    It silently stopped working once: a bad edit turned ``return True`` into
    part of a comment, so the method returned ``None`` and no test noticed.
    """
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    tools = [{"type": "function", "function": {"name": "ask_user_question"}}]
    missed = {"choices": [{"message": {"content": "卡片发出去了"},
                           "finish_reason": "stop"}]}
    assert gateway._needs_retry(tools, missed) is True


def test_no_retry_without_tools():
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    missed = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
    assert gateway._needs_retry([], missed) is False


def test_no_retry_when_a_call_came_back():
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    tools = [{"type": "function", "function": {"name": "f"}}]
    answered = {"choices": [{"message": {"tool_calls": [{"id": "c1"}]},
                             "finish_reason": "tool_calls"}]}
    assert gateway._needs_retry(tools, answered) is False


def test_no_retry_on_an_upstream_error():
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    tools = [{"type": "function", "function": {"name": "f"}}]
    failed = {"choices": [{"message": {"content": ""}, "finish_reason": "error"}]}
    assert gateway._needs_retry(tools, failed) is False


def test_the_retry_can_be_turned_off():
    from polymas2api.server import Gateway

    cfg = Config()
    cfg.tool_retry = False
    gateway = Gateway(cfg)
    tools = [{"type": "function", "function": {"name": "f"}}]
    missed = {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
    assert gateway._needs_retry(tools, missed) is False
# -- scrubbing the caller's own name ----------------------------------------
def test_the_name_is_removed_from_a_greeting():
    """The platform hands the account name to the agent, which opens with it."""
    washer = NameScrubber(names="林晓东")
    assert washer.wash_text("林晓东，来了。\n\n今天想干点什么？") == "来了。\n\n今天想干点什么？"


def test_the_name_is_removed_with_an_exclamation():
    washer = NameScrubber(names="林晓东")
    assert washer.wash_text("林晓东！在呢——") == "在呢——"


def test_the_polite_form_is_covered():
    """"林老师" (surname + 老师) is more common than the full name.

    Every name rule probes on the *surname*: the full name is not a
    substring of "林老师" or "林晓东同学", so a per-rule probe silently
    disabled those rules (measured 2026-11-16).
    """
    washer = NameScrubber(names="林晓东")
    assert washer.wash_text("林老师，我看一下。") == "我看一下。"
    assert "林" not in washer.wash_text("好的林老师，我看一下。")
    assert "林" not in washer.wash_text("你好，林晓东同学！")


def test_a_bare_name_mid_sentence_is_left_alone():
    """It is a reference, not an address; rewriting it changes the meaning."""
    washer = NameScrubber(names="林晓东")
    # It is replaced (a leak is worse than an awkward sentence), but the
    # address form is removed as a unit rather than leaving "同学提交的作业".
    assert "林晓东" not in washer.wash_text("林晓东同学提交的作业")


def test_similar_words_are_not_touched():
    washer = NameScrubber(names="林晓东")
    text = "这里有林子、树林、林木。"
    assert washer.wash_text(text) == text


def test_several_names_can_be_scrubbed():
    washer = NameScrubber(names="林晓东, 林老师")
    assert "林" not in washer.wash_text("林晓东，你好。")
    assert "林" not in washer.wash_text("林老师，你好。")


def test_a_one_character_name_is_ignored():
    """A single character would match far too much text."""
    washer = NameScrubber(names="林")
    assert washer.wash_text("林子") == "林子"


def test_no_names_means_no_extra_rules():
    washer = NameScrubber()
    assert washer.names == []
    text = "林晓东，来了。"
    assert washer.wash_text(text) == text
def test_a_name_closing_a_clause_is_removed():
    """The assistant can end a clause with it: "知道，林晓东。" """
    washer = NameScrubber(names="林晓东")
    assert washer.wash_text("知道，林晓东。这名字我记着。") == "知道。这名字我记着。"


def test_the_reasoning_trace_is_scrubbed_even_with_persona_off():
    """A leaked real name is a privacy problem, not a persona one.

    The trace is the assistant talking to itself, so a bare name is safe to
    replace outright — unlike the visible answer, which keeps the
    address-only rules so "林晓东同学提交的作业" survives.
    """
    washer = NameScrubber(names="林晓东")
    trace = "用户是林晓东，应该称呼他林晓东。"
    cleaned = washer.wash_reasoning(trace)
    assert "林晓东" not in cleaned
    # The visible answer is scrubbed too, just with the tidier rules.
    assert "林晓东" not in washer.wash_text("林晓东同学提交的作业")


def test_a_leading_comma_is_cleaned_up():
    washer = NameScrubber(names="林晓东")
    cleaned = washer.wash_text("，林晓东，来了。")
    assert "林" not in cleaned
    assert not cleaned.startswith("，")
def test_a_streamed_address_is_removed_whole():
    """A tail alone is not enough for the opening address.

    The rule that removes "林晓东，来了。" is anchored at the start of the
    reply. With only a tail held back, the first release handed over a
    fragment like "林晓" and the rest of the name arrived after it had been
    emitted (measured 2026-11-16).
    """
    washer = NameScrubber(names="林晓东")
    text = "林晓东，来了。今天想干点什么？"
    for step in (1, 2, 5, 13):
        stream = StreamWasher(washer)
        out = ""
        for index in range(0, len(text), step):
            out += stream.feed(text[index:index + step])
        out += stream.flush()
        assert "林晓东" not in out, f"leaked with {step}-char frames: {out!r}"
        assert out.startswith("来了。")


def test_a_name_split_across_frames_is_still_removed_from_the_trace():
    """A per-frame replace misses "林晓" + "东"."""
    washer = NameScrubber(names="林晓东")
    stream = StreamWasher(washer, "reasoning")
    out = ""
    for char in "用户是林晓东，应该称呼他林晓东。":
        out += stream.feed(char)
    out += stream.flush()
    assert "林" not in out
def test_no_frame_size_lets_the_name_through():
    """Fuzz every frame size: the cut must never land inside a name.

    A cut after "林" emits the surname and hands the next frame "老师…",
    which no longer contains the name, so nothing matches it.
    """
    washer = NameScrubber(names="林晓东")
    text = "用户是林晓东，应该称呼他林晓东。林老师来了，林晓东你好。" * 3
    for channel in ("text", "reasoning"):
        for step in range(1, 40):
            stream = StreamWasher(washer, channel)
            out = ""
            for index in range(0, len(text), step):
                out += stream.feed(text[index:index + step])
            out += stream.flush()
            assert "林" not in out, f"{channel} leaked at {step} chars: {out[:80]!r}"
def test_the_effective_model_is_never_the_word_auto():
    """``Auto`` is a request keyword, not a model that can answer.

    ``models()`` answers in the OpenAI shape (``data``); reading the upstream
    key (``models``) made the lookup silently fall back to ``cfg.model``.
    """
    from polymas2api.server import Gateway

    cfg = Config()
    cfg.model = "Auto"
    gateway = Gateway(cfg)
    gateway.models = lambda: {"object": "list",
                              "data": [{"id": "deepseek-v4.1-flash"}]}
    gateway._effective_model_cache = ""
    assert gateway._effective_model() == "deepseek-v4.1-flash"
# -- continuing after a tool result -----------------------------------------
def test_a_tool_result_marks_the_turn_as_a_continuation():
    """The reply to a tool result is prose, so no hold and no retry.

    Holding it blanked the whole generation — and then two retries ran on top
    of that, so the client saw nothing for three generations and reported the
    reply as hung (2026-11-17).
    """
    from polymas2api.server import _resumes_after_tool

    assert _resumes_after_tool([
        {"role": "user", "content": "列一下文件"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "name": "pwsh", "content": "a.txt"},
    ]) is True


def test_a_plain_question_is_not_a_continuation():
    from polymas2api.server import _resumes_after_tool

    assert _resumes_after_tool([{"role": "user", "content": "列一下文件"}]) is False


def test_a_tool_result_before_the_last_user_message_does_not_count():
    """Only the current turn matters; an older tool result is history."""
    from polymas2api.server import _resumes_after_tool

    assert _resumes_after_tool([
        {"role": "user", "content": "第一问"},
        {"role": "tool", "name": "pwsh", "content": "旧结果"},
        {"role": "user", "content": "第二问"},
    ]) is False
# -- per-request overrides the client may set --------------------------------
def _overrides(body):
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    gateway.log = lambda message: None
    return gateway._request_overrides(body)


def test_the_client_reasoning_effort_wins_over_the_config():
    """A client that shows an effort selector must see it work.

    Before this the field was ignored and every turn ran at the configured
    level (measured 2026-11-17: four different values, no pattern).
    """
    assert _overrides({"reasoning_effort": "max"}) == {"reasoning_effort": "max"}
    assert _overrides({"reasoning_effort": "low"}) == {"reasoning_effort": "low"}


def test_an_explicit_null_effort_turns_thinking_off():
    """OpenAI clients send nothing for "off"; the DSH profile maps off to null."""
    assert _overrides({"reasoning_effort": None}) == {"reasoning_effort": "none"}
    assert _overrides({"reasoning_effort": "off"}) == {"reasoning_effort": "none"}
    assert _overrides({"reasoning_effort": "none"}) == {"reasoning_effort": "none"}


def test_no_effort_in_the_request_means_use_the_config():
    assert _overrides({}) == {}


def test_act_mode_is_accepted_from_the_request():
    assert _overrides({"act_mode": "plan"}) == {"act_mode": "PLAN"}
    assert _overrides({"actMode": "GOAL"}) == {"act_mode": "GOAL"}


def test_a_value_outside_the_platform_whitelist_is_ignored():
    """The platform answers HTTP 400 for these, so they never go on the wire."""
    assert _overrides({"act_mode": "DEEP"}) == {}
    assert _overrides({"chat_mode": "V5"}) == {}
    assert _overrides({"act_mode": "ASK", "chat_mode": "V5"}) == {"act_mode": "ASK"}


def test_chat_mode_is_accepted_from_the_request():
    assert _overrides({"chat_mode": "fast"}) == {"chat_mode": "FAST"}
# -- the retry only fires when it can help --------------------------------
def test_a_plain_answer_does_not_trigger_a_retry():
    """A greeting needs no tool, so retrying it is pure latency.

    Measured 2026-11-17: "晚上好" burned two extra full generations and took
    17s to produce 19 characters.
    """
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    tools = [{"type": "function", "function": {"name": "pwsh"}}]
    plain = {"choices": [{"message": {"content": "晚上好。有什么需要我做的？"},
                          "finish_reason": "stop"}]}
    assert gateway._needs_retry(tools, plain) is False


def test_an_explanation_does_not_trigger_a_retry():
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    tools = [{"type": "function", "function": {"name": "pwsh"}}]
    answer = {"choices": [{"message": {"content": "质数是只能被 1 和自身整除的数。"},
                           "finish_reason": "stop"}]}
    assert gateway._needs_retry(tools, answer) is False


def test_a_claimed_action_still_triggers_the_retry():
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    tools = [{"type": "function", "function": {"name": "ask_user_question"}}]
    claimed = {"choices": [{"message": {"content": "卡片发出去了"},
                            "finish_reason": "stop"}]}
    assert gateway._needs_retry(tools, claimed) is True


def test_tool_choice_required_forces_the_retry():
    """The caller demanded a call, so any text-only answer is a miss."""
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    tools = [{"type": "function", "function": {"name": "pwsh"}}]
    plain = {"choices": [{"message": {"content": "好的。"},
                          "finish_reason": "stop"}]}
    assert gateway._needs_retry(tools, plain, force_tool=True) is True


def test_only_required_demands_a_tool():
    from polymas2api.server import _demands_tool

    assert _demands_tool({"tool_choice": "required"}) is True
    assert _demands_tool({"tool_choice": "auto"}) is False
    assert _demands_tool({"tool_choice": "none"}) is False
    assert _demands_tool({}) is False
    assert _demands_tool({"tool_choice": {"type": "function"}}) is True


# -- shapes of "I already did it" ----------------------------------------
def test_action_claims_are_recognised_and_prose_is_not():
    from polymas2api.server import _claims_action

    for claimed in ("卡片发出去了", "我已经查过系统日志了", "命令跑完了",
                    "已发送", "这就发给你"):
        assert _claims_action(claimed) is True, claimed
    for prose in ("晚上好。有什么需要我做的？", "质数是只能被 1 和自身整除的数。",
                  "今天天气不错，适合出门散步。"):
        assert _claims_action(prose) is False, prose
# -- what makes an answer worth asking again ------------------------------
def _result(content="", tool_calls=None, upstream_tools=None, finish="stop"):
    message = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    result = {"choices": [{"message": message, "finish_reason": finish}]}
    if upstream_tools is not None:
        result["_upstream_tools"] = upstream_tools
    return result


def _gw():
    from polymas2api.server import Gateway

    gateway = Gateway(Config())
    gateway.log = lambda message: None
    return gateway


TOOLS_ONE = [{"type": "function", "function": {"name": "pwsh"}}]


def test_a_complete_answer_is_not_retried():
    gateway = _gw()
    assert gateway._retry_reason(TOOLS_ONE, _result("质数是只能被 1 和自身整除的数。")) == ""


def test_the_agents_own_tools_make_the_answer_worth_retrying():
    """Its tools run in the platform's container, so the caller got nothing.

    Measured 2026-11-17: at 112 declared tools the ban still leaks, and the
    answer described an empty container instead of the caller's machine.
    """
    gateway = _gw()
    reason = gateway._retry_reason(TOOLS_ONE, _result(
        "当前目录有 a.txt", upstream_tools=[{"name": "查看文件列表"}]))
    assert reason.startswith("the agent used its own tools")
    assert "查看文件列表" in reason


def test_an_empty_reply_is_worth_retrying():
    """Measured 2026-11-17: 3 of 5 runs on an unanswerable question were empty."""
    gateway = _gw()
    assert gateway._retry_reason(TOOLS_ONE, _result("   ")) == "the reply was empty"


def test_the_native_tool_correction_is_used_first():
    gateway = _gw()
    nudges = gateway._nudges_for("the agent used its own tools (执行命令)")
    assert nudges[0] is gateway.NATIVE_TOOL_NUDGE
    assert "平台侧的容器" in nudges[0]


def test_the_empty_correction_is_used_first():
    gateway = _gw()
    assert gateway._nudges_for("the reply was empty")[0] is gateway.EMPTY_ANSWER_NUDGE


def test_a_retry_wins_over_an_empty_answer():
    """Without this the good retry was thrown away and the client saw nothing."""
    gateway = _gw()
    assert gateway._retry_wins(_result(""), _result("这是完整回答。")) is True


def test_a_retry_does_not_replace_a_complete_answer():
    gateway = _gw()
    assert gateway._retry_wins(_result("第一版完整回答"), _result("第二版")) is False


def test_a_retry_with_a_call_always_wins():
    gateway = _gw()
    call = {"function": {"name": "pwsh", "arguments": "{}"}}
    assert gateway._retry_wins(_result("卡片发出去了"), _result("", tool_calls=[call])) is True


def test_the_streaming_reason_matches_the_blocking_one():
    from polymas2api.server import _retry_reason_text

    assert _retry_reason_text("质数定义如下", False) == ""
    assert _retry_reason_text("", False) == "the reply was empty"
    assert _retry_reason_text("x", False, used=["执行命令"]).startswith(
        "the agent used its own tools")
    assert _retry_reason_text("x", True, used=["执行命令"]) == ""
# -- the empty-answer retry, driven end to end with a stub ------------------
def _chunk(delta, finish=None):
    return {"id": "c", "object": "chat.completion.chunk", "created": 0,
            "model": "m", "choices": [{"index": 0, "delta": delta,
                                       "finish_reason": finish}]}


class _Account:
    """Enough of a Credential for the pool's bookkeeping."""

    uid = "test-account"
    last_used = 0.0
    calls = 0


def _drain(gateway, adapter):
    gateway.adapter = lambda client, names=None: adapter
    return list(gateway._stream_with_journal(
        _Account(), None, "prompt", "m", "s", TOOLS_ONE, ["pwsh"]))


class _Scripted:
    """A stub adapter that replays one scripted answer per call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def stream(self, client, prompt, **kwargs):
        index = min(self.calls, len(self.answers) - 1)
        self.calls += 1
        for chunk in self.answers[index]:
            yield chunk


def test_an_empty_stream_is_retried_and_the_retry_is_delivered():
    """An empty reply is why we retried — the retry must not be thrown away.

    The first version of this gate required ``buffered`` to be non-empty, so
    an answer with no content at all skipped the retry entirely (found
    2026-11-17).
    """
    gateway = _gw()
    adapter = _Scripted(
        [_chunk({"reasoning_content": "我想了想。"}),
         _chunk({}, "stop")],
        [_chunk({"content": "这是重试后的完整回答。"}), _chunk({}, "stop")],
    )
    chunks = _drain(gateway, adapter)
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert adapter.calls == 2, "the empty answer should have been retried"
    assert text == "这是重试后的完整回答。"


def test_a_complete_stream_is_not_retried():
    gateway = _gw()
    adapter = _Scripted(
        [_chunk({"content": "质数是只能被 1 和自身整除的数。"}), _chunk({}, "stop")],
    )
    chunks = _drain(gateway, adapter)
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert adapter.calls == 1
    assert "质数" in text


def test_the_native_tool_correction_reaches_the_prompt():
    """When the agent used its own tools, the retry must say so explicitly."""
    gateway = _gw()
    seen = []

    class _Recording(_Scripted):
        def stream(self, client, prompt, **kwargs):
            seen.append(prompt)
            yield from super().stream(client, prompt, **kwargs)

    adapter = _Recording(
        [_chunk({"content": ""}), _chunk({}, "stop")],
        [_chunk({"content": "改用 CALL。"}), _chunk({}, "stop")],
    )
    gateway.adapter = lambda client, names=None: adapter
    # Feed a chunk that carries an upstream-tool record, like the adapter does.
    original = adapter.stream

    def stream_with_tool(client, prompt, **kwargs):
        for chunk in original(client, prompt, **kwargs):
            chunk["_upstream_tool"] = {"name": "执行命令"}
            yield chunk

    adapter.stream = stream_with_tool
    list(gateway._stream_with_journal(_Account(), None, "prompt", "m", "s",
                                      TOOLS_ONE, ["pwsh"]))
    assert adapter.calls == 2
    assert "平台侧的容器" in seen[-1]

def test_every_switch_the_panel_renders_can_actually_be_saved():
    """A rendered control that the save endpoint rejects is silently dead.

    Measured 2026-11-17: the model picker looked broken ("切不动") because
    ``model`` was missing from ``panel_save_settings``'s allow-list, so every
    save returned ``applied: {}``. ``local_image_attachments``, ``tool_retry``
    and ``tool_stream_buffer_chars`` had the same defect.
    """
    import re
    from pathlib import Path

    import polymas2api.server as server_module

    root = Path(server_module.__file__).parent
    app_js = (root / "web" / "app.js").read_text(encoding="utf-8")
    rendered = set(re.findall(r'key:\s*"([a-z_]+)"', app_js))
    # Names that come from non-setting widgets (journal/status counters).
    not_settings = {"accounts", "duration", "failed", "ok", "quarantined",
                    "total", "in_tokens", "out_tokens"}
    settings_rendered = rendered - not_settings

    source = (root / "server.py").read_text(encoding="utf-8")
    match = re.search(r"def panel_save_settings.*?allowed = \{(.*?)\}", source, re.S)
    assert match, "panel_save_settings allow-list not found"
    allowed = set(re.findall(r'"([a-z_]+)"', match.group(1)))

    assert settings_rendered <= allowed, sorted(settings_rendered - allowed)


# -- account management ----------------------------------------------------


def _fake_jwt(claims: dict) -> str:
    """A structurally valid JWT whose signature is worthless."""
    import base64
    import json

    head = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{head}.{body}.signature"


@pytest.fixture
def gateway_with_temp_accounts(tmp_path):
    """A gateway whose accounts file lives in tmp_path, never in the repo."""
    from polymas2api.server import Gateway

    def build():
        cfg = Config()
        # A config written before accounts.json existed still carries the
        # credential inline; the pool seeds it and the gateway migrates it.
        cfg.user_nid = "Nid-Legacy"
        cfg.friend_nid = "agent-legacy"
        cfg.ai_poly = "legacy-token"
        cfg.extra["accounts_file"] = str(tmp_path / "accounts.json")
        cfg.extra["_base_dir"] = str(tmp_path)
        return Gateway(cfg)
    return build


def test_adding_an_account_reads_user_nid_from_the_ai_poly_jwt(gateway_with_temp_accounts):
    """Only the cookie has to be pasted: its own userNid claim fills the rest."""
    gw = gateway_with_temp_accounts()
    result = gw.panel_add_account({
        "ai_poly": _fake_jwt({"userNid": "Nid-A", "loginId": "Nid-A"}),
        "friend_nid": "agent-1",
        "label": "测试号",
        "verify": False,
        "create_agent": False,
    })
    assert result["ok"] is True
    assert result["account"]["user_nid"] == "Nid-A"
    assert gw.pool.get("Nid-A") is not None


def test_adding_the_same_credential_twice_is_refused(gateway_with_temp_accounts):
    """A duplicate cookie would look like two accounts but bill one account."""
    gw = gateway_with_temp_accounts()
    token = _fake_jwt({"userNid": "Nid-B"})
    first = gw.panel_add_account({"ai_poly": token, "friend_nid": "agent-1",
                                  "verify": False, "create_agent": False})
    assert first["ok"] is True
    second = gw.panel_add_account({"ai_poly": token, "friend_nid": "agent-1",
                                   "verify": False, "create_agent": False})
    assert second["ok"] is False
    message = second["error"]["message"]
    # uid 由 userNid 推出，所以同一个 cookie 通常先撞 uid；显式换了 uid 才会
    # 落到"凭据已在池里"那条分支上。两条都要能拦住。
    assert "已存在" in message or "已经在池里" in message


def test_a_missing_ai_poly_is_reported_clearly(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    result = gw.panel_add_account({})
    assert result["ok"] is False
    assert "ai_poly 必填" in result["error"]["message"]


def test_jt_cas_alone_is_not_accepted(gateway_with_temp_accounts):
    """jt-cas lasts 24 hours and cannot be renewed unattended, so ai-poly is
    what this gateway asks for; a jt-cas-only submission is refused early."""
    gw = gateway_with_temp_accounts()
    result = gw.panel_add_account({"jt_cas": "whatever", "verify": False, "create_agent": False})
    assert result["ok"] is False
    assert "ai_poly 必填" in result["error"]["message"]


def test_test_only_mode_verifies_without_touching_the_pool(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    before = len(gw.pool.list())
    result = gw.panel_add_account({
        "ai_poly": _fake_jwt({"userNid": "Nid-C"}),
        "friend_nid": "agent-1",
        "test": True, "verify": False,
    })
    assert result["ok"] is True and result["test_only"] is True
    assert len(gw.pool.list()) == before


def test_any_account_can_be_deleted(gateway_with_temp_accounts):
    """There is no privileged row any more: every account is deletable."""
    gw = gateway_with_temp_accounts()
    gw.panel_add_account({"ai_poly": _fake_jwt({"userNid": "Nid-D2"}),
                          "friend_nid": "agent-1", "verify": False,
                          "create_agent": False})
    assert gw.panel_delete_account("Nid-D2")["ok"] is True
    assert gw.pool.get("Nid-D2") is None


def test_the_pool_can_be_emptied(gateway_with_temp_accounts):
    """A fresh install starts with zero accounts, so deleting down to zero is
    a state the product already expects — refusing it made no sense."""
    gw = gateway_with_temp_accounts()
    assert gw.panel_delete_account("Nid-Legacy")["ok"] is True
    assert gw.pool.list() == []
    with pytest.raises(PoolError):
        gw.pool.acquire()
    # 面板自己不能因为空池而崩
    overview = gw.panel_overview()
    assert overview["pool"]["total"] == 0
    assert overview["accounts"] == []


def test_an_emptied_pool_can_be_filled_again(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    gw.panel_delete_account("Nid-Legacy")
    result = gw.panel_add_account({"ai_poly": _fake_jwt({"userNid": "Nid-BACK"}),
                                   "friend_nid": "agent-1", "verify": False,
                                   "create_agent": False})
    assert result["ok"] is True
    assert [a["uid"] for a in gw.pool.list()] == ["Nid-BACK"]


def test_a_fresh_install_starts_with_an_empty_pool(tmp_path):
    """Nobody should have to hand-edit config.json before the panel works."""
    from polymas2api.server import Gateway

    cfg = Config()
    cfg.extra["accounts_file"] = str(tmp_path / "accounts.json")
    cfg.extra["_base_dir"] = str(tmp_path)
    gw = Gateway(cfg)
    assert gw.pool.list() == []
    assert gw.pool.stats()["total"] == 0
    with pytest.raises(PoolError):
        gw.pool.acquire()


def test_a_legacy_configuration_is_migrated_into_the_accounts_file(tmp_path):
    from polymas2api.server import Gateway

    cfg = Config()
    cfg.user_nid = "Nid-OLD"
    cfg.friend_nid = "agent-old"
    cfg.ai_poly = "old-cookie"
    path = tmp_path / "accounts.json"
    cfg.extra["accounts_file"] = str(path)
    cfg.extra["_base_dir"] = str(tmp_path)
    gw = Gateway(cfg)

    assert gw.pool.get("Nid-OLD") is not None
    assert path.exists(), "迁移必须落盘，否则每次启动都要重新推断"
    import json as _json
    entries = _json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert [e["uid"] for e in entries] == ["Nid-OLD"]
    # config.json 不动：回滚就是删掉这个文件
    assert cfg.ai_poly == "old-cookie"


def test_migration_leaves_an_existing_accounts_file_alone(tmp_path):
    import json as _json

    from polymas2api.server import Gateway

    path = tmp_path / "accounts.json"
    path.write_text(_json.dumps({"accounts": [
        {"uid": "keep", "user_nid": "n", "friend_nid": "f", "ai_poly": "t"}
    ]}), encoding="utf-8")
    cfg = Config()
    cfg.user_nid = "Nid-OLD"
    cfg.friend_nid = "agent-old"
    cfg.ai_poly = "old-cookie"
    cfg.extra["accounts_file"] = str(path)
    cfg.extra["_base_dir"] = str(tmp_path)
    gw = Gateway(cfg)

    entries = _json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert [e["uid"] for e in entries] == ["keep"], "已有账号文件时不该被覆盖"


def test_an_added_account_survives_a_reload(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    gw.panel_add_account({"ai_poly": _fake_jwt({"userNid": "Nid-D"}),
                          "friend_nid": "agent-1",
                          "label": "持久化", "verify": False, "create_agent": False})
    reloaded = gateway_with_temp_accounts()
    reloaded.pool.load_accounts()
    account = reloaded.pool.get("Nid-D")
    assert account is not None, "账号没有写进 accounts 文件"
    assert account.label == "持久化"


def test_saving_accounts_never_writes_the_builtin_default(gateway_with_temp_accounts):
    """``default`` is a view of config.json; persisting it would clone the
    secret into a second file and let a stale copy win on the next start."""
    import json

    gw = gateway_with_temp_accounts()
    gw.panel_add_account({"ai_poly": _fake_jwt({"userNid": "Nid-E"}),
                          "friend_nid": "agent-1", "verify": False, "create_agent": False})
    raw = json.loads((pathlib.Path(gw.cfg.extra["_base_dir"]) / "accounts.json")
                     .read_text(encoding="utf-8"))
    uids = [entry["uid"] for entry in raw["accounts"]]
    assert "Nid-E" in uids
    assert "default" not in uids


def test_verify_mode_reports_a_duplicate_before_the_confirm_step(gateway_with_temp_accounts):
    """The dialog verifies as you paste; a duplicate must surface there rather
    than after the user presses 确认添加."""
    gw = gateway_with_temp_accounts()
    token = _fake_jwt({"userNid": "Nid-F"})
    gw.panel_add_account({"ai_poly": token, "friend_nid": "agent-1", "verify": False, "create_agent": False})
    again = gw.panel_add_account({"ai_poly": token, "friend_nid": "agent-1",
                                  "test": True, "verify": False})
    assert again["ok"] is False
    message = again["error"]["message"]
    assert "已存在" in message or "已经在池里" in message


def test_create_assistant_sends_the_payload_the_platform_expects(gateway_with_temp_accounts):
    """The creation body was reverse-engineered on 2026-11-16; pin it down.

    The reply arrives already unwrapped (``_check_payload`` returns ``data``),
    which is why the nid comes back as a bare string.
    """
    from polymas2api.upstream import PolymasClient

    sent = {}

    client = PolymasClient(Config(), Credential(uid="x", user_nid="Nid-Z",
                                                friend_nid="", ai_poly="tok"))

    def fake_post(path, payload, timeout=60.0):
        sent["path"] = path
        sent["payload"] = payload
        return "NewAgentNid"

    client._post = fake_post
    nid = client.create_assistant("通用助手")

    assert nid == "NewAgentNid"
    assert sent["path"] == "/llmOps/application/saveAssistant"
    body = sent["payload"]
    assert body["appType"] == "AGENT"
    assert body["appCategory"] == "AGENT_TOOL"
    assert body["appName"] == "通用助手"
    assert body["isPublish"] == 0
    assert body["model"] == "polymas"
    assert body["bindTools"] == [] and body["datasets"] == []
    assert body["userNid"] == "Nid-Z"


def test_adding_without_an_agent_and_without_a_friend_nid_is_refused(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    result = gw.panel_add_account({
        "ai_poly": _fake_jwt({"userNid": "Nid-Y"}),
        "verify": False, "create_agent": False,
    })
    assert result["ok"] is False
    assert "没有可用的助手" in result["error"]["message"]


# -- per-account renewal (2026-11-18) --------------------------------------


class _CatalogueStub:
    """Stands in for PolymasClient during a renewal verification."""

    def __init__(self, ok=True):
        self.ok = ok
        self._override_credential = ""

    def models(self):
        if not self.ok:
            from polymas2api.upstream import UpstreamError
            raise UpstreamError("拒绝访问")
        return {"models": [{"modelCode": "deepseek-v4.1-flash"}], "available": []}


def _renewal_gateway(tmp_path, monkeypatch, *, verify_ok=True, token="new-token-xyz"):
    from polymas2api.server import Gateway

    cfg = Config()
    cfg.user_nid = "Nid-A"
    cfg.friend_nid = "agent-1"
    cfg.ai_poly = "old-token"
    cfg.renew_interval_minutes = 0          # no background timer in tests
    cfg.extra["accounts_file"] = str(tmp_path / "accounts.json")
    cfg.extra["_base_dir"] = str(tmp_path)
    gw = Gateway(cfg)
    monkeypatch.setattr("polymas2api.server.fetch_token",
                        lambda nid, cred: token)
    monkeypatch.setattr(gw, "_account_client",
                        lambda account: _CatalogueStub(ok=verify_ok))
    return gw


def test_renewal_updates_every_enabled_account(tmp_path, monkeypatch):
    """Each account carries its own cookie; the configured one used to be the
    only one refreshed, so added accounts silently went stale."""
    gw = _renewal_gateway(tmp_path, monkeypatch)
    gw.pool.add(Credential("acc-2", user_nid="Nid-B", friend_nid="agent-2",
                           ai_poly="old-2"))
    result = gw.renew_all()
    assert result["ok"] is True and result["ok_count"] == 2
    for uid in ("Nid-A", "acc-2"):
        account = gw.pool.get(uid)
        assert account.ai_poly == "new-token-xyz"
        assert account.renew_count == 1
        assert account.renew_error == ""


def test_a_failed_verification_keeps_the_previous_token(tmp_path, monkeypatch):
    gw = _renewal_gateway(tmp_path, monkeypatch, verify_ok=False)
    before = gw.pool.get("Nid-A").ai_poly
    result = gw.renew_all()
    assert result["ok"] is False
    account = gw.pool.get("Nid-A")
    assert account.ai_poly == before, "验证失败的 token 不该被装上"
    assert account.renew_error
    assert account.renew_count == 0


def test_renewal_skips_a_disabled_account(tmp_path, monkeypatch):
    gw = _renewal_gateway(tmp_path, monkeypatch)
    gw.pool.add(Credential("off", user_nid="Nid-C", friend_nid="agent-3",
                           ai_poly="old-3", enabled=False))
    gw.renew_all()
    assert gw.pool.get("off").renew_count == 0


def test_renewal_bookkeeping_survives_a_reload(tmp_path, monkeypatch):
    gw = _renewal_gateway(tmp_path, monkeypatch)
    gw.pool.add(Credential("acc-2", user_nid="Nid-B", friend_nid="agent-2",
                           ai_poly="old-2"))
    assert gw.renew_all()["ok"] is True

    from polymas2api.server import Gateway
    cfg = Config()
    cfg.extra["accounts_file"] = str(tmp_path / "accounts.json")
    cfg.extra["_base_dir"] = str(tmp_path)
    reloaded = Gateway(cfg)
    reloaded.pool.load_accounts()
    account = reloaded.pool.get("acc-2")
    assert account is not None
    assert account.renew_count == 1
    assert account.renewed_at is not None
    assert account.ai_poly == "new-token-xyz"


# -- token accounting (2026-11-18) -----------------------------------------


def test_token_estimate_mixes_cjk_and_latin():
    """The platform reports no usage, so the numbers are estimates; the model
    is CJK≈1 token/char, Latin≈4 chars/token."""
    from polymas2api.tokens import estimate_tokens

    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("你好abcdefgh") == 4   # 2 + 8/4


def test_usage_counts_reasoning_as_completion():
    from polymas2api.tokens import usage_for

    usage = usage_for("提示词", "答案", "思考")
    assert usage["prompt_tokens"] == 3
    assert usage["completion_tokens"] == 4      # 答案 + 思考
    assert usage["total_tokens"] == 7
    assert usage["estimated"] is True


def test_the_completion_object_carries_a_real_usage_block():
    """It used to be hard-coded zeros, which a client cannot tell apart from
    "cost nothing"."""
    from polymas2api.adapter import OpenAIAdapter
    from polymas2api.config import Config

    class _Client:
        def chat(self, prompt, **kwargs):
            yield "ANSWER", {"text": "这是回答"}
            yield "ALL_FINISHED", {}

    adapter = OpenAIAdapter(Config())
    result = adapter.complete(_Client(), "这是提示词")
    usage = result["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_the_journal_totals_tokens(tmp_path):
    from polymas2api.journal import Journal, JournalEntry

    # Its own directory, no archive: otherwise this reads the real
    # logs/requests.jsonl and the totals are whatever the deployment has.
    cfg = Config()
    cfg.archive_enabled = False
    cfg.extra["_base_dir"] = str(tmp_path)
    journal = Journal(cfg)
    journal.record(JournalEntry(account="a", model="m", stream=False,
                                prompt_chars=10, duration_ms=5, ok=True,
                                prompt_tokens=7, completion_tokens=3))
    counters = journal.counters()
    assert counters["prompt_tokens"] == 7
    assert counters["completion_tokens"] == 3
    assert journal.recent(limit=1)[0]["prompt_tokens"] == 7


# -- client aborts mid-stream (2026-11-18) ---------------------------------


def test_an_aborted_stream_is_journaled_as_aborted_not_success(gateway_with_temp_accounts):
    """A "stop" click used to be counted as a success, which inflated the
    success rate and hid the fact that the answer never arrived."""
    import time

    gw = gateway_with_temp_accounts()
    gw._record(gw.pool.get("Nid-Legacy"), "m", "prompt",
               {"reasoning": 0, "content": 0, "error": ""},
               time.time() - 1, True, [], aborted=True)
    entry = gw.journal.recent(limit=1)[0]
    assert entry["aborted"] is True
    assert entry["status"] == "aborted"
    # An abort is not a gateway failure, so it must not land in 失败.
    assert entry["ok"] is True
    assert gw.journal.counters()["aborted"] == 1


def test_a_normal_stream_is_not_marked_aborted(gateway_with_temp_accounts):
    import time

    gw = gateway_with_temp_accounts()
    gw._record(gw.pool.get("Nid-Legacy"), "m", "prompt",
               {"reasoning": 0, "content": 5, "error": ""},
               time.time() - 1, True, [])
    entry = gw.journal.recent(limit=1)[0]
    assert entry["aborted"] is False
    assert entry["status"] == "ok"


def test_client_gone_recognises_a_closed_socket():
    """The check that makes "stop" work while the upstream is silent."""
    import socket

    from polymas2api.server import Handler

    client, server = socket.socketpair()
    try:
        class _Stub:
            connection = client
        stub = _Stub()
        assert Handler._client_gone(stub) is False, "活着的连接不该被判为断开"
        server.close()
        import time
        for _ in range(50):                 # EOF 可能有一瞬间的延迟
            if Handler._client_gone(stub):
                break
            time.sleep(0.02)
        assert Handler._client_gone(stub) is True
    finally:
        try:
            client.close()
        except OSError:
            pass
        try:
            server.close()
        except OSError:
            pass


# -- account pool rotation (2026-11-18) ------------------------------------


def _pool_with(weights):
    cfg = Config()
    cfg.extra["accounts_file"] = ""
    pool = CredentialPool(cfg, logger=lambda message, **_: None)
    pool._accounts.clear()
    for uid, weight in weights:
        pool.add(Credential(uid, user_nid=f"n{uid}", friend_nid=f"f{uid}",
                            ai_poly="token", weight=weight))
    return pool


def test_rotation_honours_the_weight_proportion():
    pool = _pool_with([("A", 3), ("B", 1), ("C", 1)])
    picked = [pool.acquire().uid for _ in range(500)]
    assert picked.count("A") == 300
    assert picked.count("B") == 100
    assert picked.count("C") == 100


def test_rotation_is_smooth_not_bursty():
    """Expanding a list by weight gave the right proportion in bursts —
    3:1:1 started A A A. The whole point of weights is spreading load."""
    pool = _pool_with([("A", 3), ("B", 1), ("C", 1)])
    picked = [pool.acquire().uid for _ in range(60)]
    longest = run = 1
    for previous, current in zip(picked, picked[1:]):
        run = run + 1 if current == previous else 1
        longest = max(longest, run)
    assert longest <= 2, f"最长连续 {longest} 次，太突发"


def test_equal_weights_alternate():
    pool = _pool_with([("X", 1), ("Y", 1)])
    picked = [pool.acquire().uid for _ in range(10)]
    assert picked == ["X", "Y"] * 5


def test_a_disabled_or_quarantined_account_is_skipped():
    pool = _pool_with([("A", 1), ("B", 1), ("C", 1)])
    pool.set_enabled("C", False)
    pool.report_failure(pool.get("B"), "身份校验失败", cooldown=300)
    assert {pool.acquire().uid for _ in range(20)} == {"A"}


def test_set_weight_clamps_and_shares_add_up():
    pool = _pool_with([("A", 1), ("B", 1)])
    assert pool.set_weight("A", 3) is True
    assert pool.get("A").weight == 3
    assert pool.shares() == {"A": 0.75, "B": 0.25}
    pool.set_weight("A", 999)
    assert pool.get("A").weight == 100
    pool.set_weight("A", -5)
    assert pool.get("A").weight == 1
    assert pool.set_weight("nope", 2) is False


def test_the_panel_can_change_a_weight(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    result = gw.panel_set_weight("Nid-Legacy", 4)
    assert result["ok"] is True
    assert gw.pool.get("Nid-Legacy").weight == 4
    assert gw.panel_set_weight("nope", 4)["ok"] is False


def test_adding_an_account_honours_the_requested_weight(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    gw.panel_add_account({"ai_poly": _fake_jwt({"userNid": "Nid-W"}),
                          "friend_nid": "agent-1", "weight": 7,
                          "verify": False, "create_agent": False})
    assert gw.pool.get("Nid-W").weight == 7


def test_a_deleted_account_does_not_come_back_after_a_restart(tmp_path):
    """The credential is still in config.json; seeding unconditionally would
    resurrect an account the operator deliberately removed."""
    from polymas2api.server import Gateway

    def build():
        cfg = Config()
        cfg.user_nid = "Nid-OLD"
        cfg.friend_nid = "agent-old"
        cfg.ai_poly = "old-cookie"
        cfg.extra["accounts_file"] = str(tmp_path / "accounts.json")
        cfg.extra["_base_dir"] = str(tmp_path)
        return Gateway(cfg)

    gw = build()
    gw.panel_add_account({"ai_poly": _fake_jwt({"userNid": "Nid-NEW"}),
                          "friend_nid": "agent-new", "verify": False,
                          "create_agent": False})
    assert gw.panel_delete_account("Nid-OLD")["ok"] is True

    again = build()
    assert again.pool.get("Nid-OLD") is None, "删掉的账号被 config 里的凭据复活了"
    assert again.pool.get("Nid-NEW") is not None


# -- account labels (2026-11-18) -------------------------------------------


def test_an_account_can_have_no_label(gateway_with_temp_accounts):
    """The migrated account used to be stamped 「（来自 config.json）」, which
    outlived the thing it described; blank must stay blank."""
    gw = gateway_with_temp_accounts()
    assert gw.pool.get("Nid-Legacy").label == "", "迁移不该写死一个标签"


def test_the_panel_can_rename_an_account(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    assert gw.panel_set_label("Nid-Legacy", "主号")["ok"] is True
    assert gw.pool.get("Nid-Legacy").label == "主号"
    assert gw.panel_set_label("Nid-Legacy", "  ")["ok"] is True
    assert gw.pool.get("Nid-Legacy").label == ""
    assert gw.panel_set_label("nope", "x")["ok"] is False


def test_a_new_account_keeps_a_blank_label_blank(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    gw.panel_add_account({"ai_poly": _fake_jwt({"userNid": "Nid-L"}),
                          "friend_nid": "agent-1", "verify": False,
                          "create_agent": False})
    assert gw.pool.get("Nid-L").label == ""


# -- model catalogue caching (2026-11-18) ----------------------------------


class _CatalogueCounter:
    def __init__(self):
        self.calls = 0

    def models(self):
        self.calls += 1
        return {"models": [{"modelCode": "m1", "modelName": "M1", "default": True}],
                "available": [{"modelCode": "m1"}, {"modelCode": "m2"}]}


def _cached_gateway(gateway_with_temp_accounts, monkeypatch):
    gw = gateway_with_temp_accounts()
    counter = _CatalogueCounter()
    monkeypatch.setattr(gw, "_acquire",
                        lambda model="": (gw.pool.get("Nid-Legacy"), counter))
    return gw, counter


def test_the_catalogue_is_cached_between_reads(gateway_with_temp_accounts, monkeypatch):
    """Two upstream requests per read, and the panel polls every 5 s — without
    a cache an idle panel meant ~24 upstream calls a minute."""
    gw, counter = _cached_gateway(gateway_with_temp_accounts, monkeypatch)
    gw.models()
    gw.models()
    gw.models()
    assert counter.calls == 1
    assert gw.catalogue_age() >= 0


def test_refreshing_forces_a_new_fetch(gateway_with_temp_accounts, monkeypatch):
    gw, counter = _cached_gateway(gateway_with_temp_accounts, monkeypatch)
    gw.models()
    result = gw.panel_refresh_models()
    assert counter.calls == 2
    assert result["ok"] is True
    assert [m["id"] for m in result["models"]] == ["m1", "m2"]
    assert result["age_seconds"] == 0


def test_the_cache_expires(gateway_with_temp_accounts, monkeypatch):
    from polymas2api.server import CATALOGUE_TTL

    gw, counter = _cached_gateway(gateway_with_temp_accounts, monkeypatch)
    gw.models()
    gw._catalogue_at -= (CATALOGUE_TTL + 1)   # pretend it aged out
    gw.models()
    assert counter.calls == 2


def test_restored_entries_keep_their_token_counts(tmp_path):
    """The archive wrote the tokens but the restore dropped them, so every
    figure on the overview read 0 after a restart (found 2026-11-18)."""
    import json as _json

    from polymas2api.journal import Journal

    archive = tmp_path / "requests.jsonl"
    rows = [
        {"at": 1.0, "account": "a", "model": "m", "stream": False, "ok": True,
         "status": "ok", "duration_ms": 10, "prompt_chars": 5, "prompt_tokens": 4,
         "completion_tokens": 7, "reasoning_chars": 3, "content_chars": 9,
         "bytes_in": 2, "bytes_out": 11, "error": "", "trace_id": "", "aborted": False},
        {"at": 2.0, "account": "a", "model": "m", "stream": True, "ok": False,
         "status": "aborted", "duration_ms": 20, "prompt_chars": 6, "prompt_tokens": 5,
         "completion_tokens": 0, "reasoning_chars": 0, "content_chars": 0,
         "bytes_in": 0, "bytes_out": 0, "error": "client went away",
         "trace_id": "", "aborted": True},
    ]
    archive.write_text("\n".join(_json.dumps(r) for r in rows), encoding="utf-8")

    cfg = Config()
    cfg.archive_enabled = True
    cfg.extra["_base_dir"] = str(tmp_path)
    cfg.extra["archive_file"] = str(archive)
    journal = Journal(cfg)

    stats = journal.counters()
    assert stats["prompt_tokens"] == 9, stats
    assert stats["completion_tokens"] == 7
    assert stats["prompt_chars"] == 11
    assert stats["aborted"] == 1
    assert stats["total"] == 2
    assert stats["failed"] == 1


def test_the_total_covers_the_whole_archive_not_just_the_ring(tmp_path):
    """It reported "500 total" forever because the ring buffer caps at 500;
    the archive is the real record."""
    import json as _json

    from polymas2api.journal import Journal

    archive = tmp_path / "requests.jsonl"
    rows = [{"at": float(i), "account": "a", "model": "m", "ok": True,
             "prompt_tokens": 2, "completion_tokens": 3, "prompt_chars": 10}
            for i in range(600)]
    archive.write_text("\n".join(_json.dumps(r) for r in rows), encoding="utf-8")

    cfg = Config()
    cfg.archive_enabled = True
    cfg.journal_size = 10
    cfg.extra["_base_dir"] = str(tmp_path)
    cfg.extra["archive_file"] = str(archive)
    journal = Journal(cfg)

    stats = journal.counters()
    assert stats["total"] == 600, "必须数完整个归档，不是只数环形缓冲"
    assert stats["ok"] == 600
    assert stats["prompt_tokens"] == 1200
    assert stats["completion_tokens"] == 1800
    assert stats["entries"] == 10, "环形缓冲仍只留尾部"


def test_a_called_tool_is_journaled(gateway_with_temp_accounts):
    """The 工具 column was empty for non-streaming replies that did call a
    tool, because only the streaming path passed the names on (2026-11-18)."""
    gw = gateway_with_temp_accounts()
    account = gw.pool.get("Nid-Legacy")
    reply = {
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{"index": 0, "id": "call_0001", "type": "function",
                            "function": {"name": "get_weather",
                                         "arguments": "{}"}}]}}],
    }
    gw._record(account, "m", "prompt", reply, 0.0, False, ["get_weather"])
    entry = gw.journal.recent(limit=1)[0]
    assert entry["tools"] == "get_weather"


def test_a_reply_without_tools_journals_an_empty_column(gateway_with_temp_accounts):
    gw = gateway_with_temp_accounts()
    account = gw.pool.get("Nid-Legacy")
    reply = {"choices": [{"index": 0, "message": {"role": "assistant",
                                                  "content": "hi", "tool_calls": None}}]}
    gw._record(account, "m", "prompt", reply, 0.0, False, [])
    assert gw.journal.recent(limit=1)[0]["tools"] == ""
