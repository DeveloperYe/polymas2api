"""HTTP client for the polymas platform: SSE streaming plus small REST calls.

Only the standard library is used. Streaming is done with ``urllib`` against a
raw socket so that SSE frames arrive incrementally instead of being buffered
until the response ends.

The outbound side is shaped by :mod:`polymas2api.fingerprint` (client
headers, TLS settings) and paced by :mod:`polymas2api.fingerprint.RequestShaper`
so the traffic does not have the shape of a script.
"""

from __future__ import annotations

import json
import socket
import ssl
import time
import urllib.error
import uuid
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import constants as C
from .fingerprint import Fingerprint, RequestShaper, build_ssl_context

__all__ = ["UpstreamError", "PolymasClient", "parse_sse"]


class UpstreamError(Exception):
    """The platform answered with an error object instead of a stream."""

    def __init__(self, message: str, *, code: Optional[int] = None, trace_id: str = "",
                 account: str = ""):
        super().__init__(message)
        self.code = code
        self.trace_id = trace_id
        self.account = account


def parse_sse(raw: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(event, data)`` pairs from a chunk of SSE text.

    Handles multi-line ``data:`` fields and ignores comment lines. The platform
    uses one ``event:`` line followed by one ``data:`` line per frame, but a
    tolerant parser costs nothing.
    """
    event = ""
    data: list = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        if not line:
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
        else:
            data.append(line)
    if data:
        yield event or "message", "\n".join(data)


def summarise_error(raw: bytes) -> str:
    """Reduce an upstream error body to a short, secret-free summary.

    The raw body can echo request headers, so it is parsed for the fields
    the platform actually uses (``msg`` / ``code`` / ``traceId``) and
    everything else is dropped rather than truncated-and-logged.
    """
    text = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # An HTML error page (nginx, a gateway): keep only the status line.
        first = text.strip().splitlines()
        return (first[0][:120] if first else "non-JSON error body")
    if isinstance(payload, dict):
        parts = []
        for field in ("code", "msg", "message", "traceId"):
            value = payload.get(field)
            if value not in (None, ""):
                parts.append(f"{field}={str(value)[:120]}")
        if parts:
            return "; ".join(parts)
    return "error body carried no recognisable fields"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects.

    ``urllib`` keeps the ``Authorization`` and ``Cookie`` headers across a
    redirect, so a 302 from the platform to a third-party host would hand
    the session cookies to that host. The platform never redirects, so
    refusing is safe.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: An opener that never follows a redirect and ignores the proxy
#: environment variables, so the outbound request always goes to the
#: platform and never through an intercepting proxy.
_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))


class _ChunkedDecoder:
    """Strip HTTP/1.1 chunked transfer framing from a byte stream.

    The platform streams the assistant answer with ``Transfer-Encoding:
    chunked``; the SSE frames only become visible once the chunk sizes are
    removed. Chunk extensions (``;name=value``) and trailers are tolerated.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self._done = False

    def feed(self, data: bytes) -> Tuple[bytes, bool]:
        """Return ``(decoded_bytes, finished)``."""
        if self._done:
            return b"", True
        self._buffer += data
        out = bytearray()
        while True:
            marker = self._buffer.find(b"\r\n")
            if marker < 0:
                break
            line = self._buffer[:marker].split(b";", 1)[0].strip()
            if not line:
                self._buffer = self._buffer[marker + 2:]
                continue
            try:
                size = int(line, 16)
            except ValueError:
                out += self._buffer
                self._buffer = b""
                self._done = True
                break
            if size == 0:
                self._done = True
                self._buffer = b""
                break
            start = marker + 2
            end = start + size
            if len(self._buffer) < end + 2:
                break
            out += self._buffer[start:end]
            self._buffer = self._buffer[end + 2:]
        return bytes(out), self._done


def _limited_free(ext: Any) -> bool:
    """Whether a catalogue entry is marked as free-of-charge."""
    if not ext:
        return False
    if isinstance(ext, dict):
        return bool(ext.get("limitedFree"))
    try:
        return bool(json.loads(ext).get("limitedFree"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


class PolymasClient:
    """Talks to ``cloudapi.polymas.com`` on behalf of one platform account."""

    def __init__(self, config, account=None, *, logger=None,
                 fingerprint: Optional[Fingerprint] = None,
                 shaper: Optional[RequestShaper] = None):
        self.cfg = config
        self.account = account
        self.log = logger or (lambda message, **_: None)
        self.fingerprint = fingerprint or Fingerprint(getattr(config, "fingerprint", "edge-157"))
        self.shaper = shaper or RequestShaper(config, logger=self.log)
        #: Set to a renewed platform token to override the configured one.
        self._override_credential = ""

    # -- identity -----------------------------------------------------------
    @property
    def _key(self) -> str:
        """The key the pool rotates on."""
        if self.account is not None:
            return self.account.uid
        return self.cfg.user_nid or "default"

    def _identity(self) -> Dict[str, str]:
        """Who this request speaks as.

        Always an account: falling back to credentials inline in config.json
        would silently use a possibly-stale cookie whenever the pool was empty,
        which is exactly the kind of failure that hides itself.
        """
        account = self.account
        if account is None:
            raise UpstreamError("no account is bound to this client")
        ai_poly = account.ai_poly
        if self._override_credential:
            ai_poly = self._override_credential
        return {
            "user_nid": account.user_nid,
            "friend_nid": account.friend_nid,
            "app_code": account.app_code or account.friend_nid,
            "ai_poly": ai_poly,
            "jt_cas": account.jt_cas if account else self.cfg.jt_cas,
            "model": (account.model if account else self.cfg.model),
        }

    def _credential(self) -> str:
        identity = self._identity()
        return identity["ai_poly"] or identity["jt_cas"]

    # -- headers ------------------------------------------------------------
    def _headers(self, *, extra: Optional[Dict[str, str]] = None,
                 accept: str = "text/event-stream") -> List[Tuple[str, str]]:
        identity = self._identity()
        profile = self.fingerprint.profile_for(self._key)
        credential = identity["ai_poly"] or identity["jt_cas"]

        # A browser sends the cookie last and the accept header once. The
        # profile supplies its own accept line, so the two must be merged
        # rather than duplicated.
        pairs: List[Tuple[str, str]] = [
            ("Content-Type", "application/json;charset=UTF-8"),
            ("Authorization", credential),
            ("route", "/stu-hike/chat"),
        ]
        for name, value in profile.headers(with_origin=getattr(self.cfg, "send_origin", True)):
            if name == "accept":
                continue
            pairs.append((name.title(), value))
        pairs.append(("Accept", accept))
        if extra:
            pairs.extend((name.title(), value) for name, value in extra.items())
        # The browser sends the cookie last.
        pairs.append(("Cookie", f"ai-poly={identity['ai_poly']}; jt-cas={identity['jt_cas']}"))
        return pairs

    # -- plumbing -----------------------------------------------------------
    def _stream_post(self, path: str, body: Dict[str, Any], timeout: float) -> Iterator[Tuple[str, str]]:
        """POST JSON and yield SSE ``(event, data)`` frames as they arrive."""
        host = C.CLOUD_API[len("https://"):]
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        lines = [
            f"POST {path} HTTP/1.1",
            f"Host: {host}",
            f"Content-Length: {len(payload)}",
            "Connection: close",
        ]
        lines += [f"{name}: {value}" for name, value in self._headers()]
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

        context = build_ssl_context()
        raw = socket.create_connection((host, 443), timeout=timeout)
        try:
            raw = context.wrap_socket(raw, server_hostname=host)
            raw.sendall(request + payload)

            buffer = b""
            header_end = -1
            while header_end < 0:
                chunk = raw.recv(65536)
                if not chunk:
                    raise UpstreamError("Connection closed before the response headers arrived")
                buffer += chunk
                header_end = buffer.find(b"\r\n\r\n")
                if header_end < 0 and len(buffer) > 65536:
                    raise UpstreamError("Response headers are unreasonably large")

            head = buffer[:header_end].decode("latin-1", "replace")
            parts = head.split("\r\n", 1)[0].split()
            status = int(parts[1]) if len(parts) > 1 else 0
            if status >= 400:
                raw_body = buffer[header_end + 4:]
                try:
                    raw_body += raw.recv(65536)
                except OSError:
                    pass
                raise UpstreamError(
                    f"Platform returned HTTP {status}: {summarise_error(raw_body)}",
                    code=status, account=self._key,
                )

            # The platform answers with `Transfer-Encoding: chunked`; the chunk
            # framing has to be removed before the SSE frames can be parsed.
            chunked = "chunked" in head.lower() and "transfer-encoding" in head.lower()

            raw_pending = buffer[header_end + 4:]
            decoder = _ChunkedDecoder() if chunked else None
            frames = bytearray()
            while True:
                if decoder is not None:
                    decoded, done = decoder.feed(raw_pending)
                    raw_pending = b""
                    frames += decoded
                    if done:
                        break
                else:
                    frames += raw_pending
                    raw_pending = b""
                while b"\n\n" in frames:
                    frame, _, rest = frames.partition(b"\n\n")
                    text = frame.decode("utf-8", "replace")
                    frames = bytearray(rest)
                    if text.strip():
                        for event, data in parse_sse(text):
                            yield event, data
                chunk = raw.recv(65536)
                if not chunk:
                    break
                raw_pending += chunk
            if frames.strip():
                for event, data in parse_sse(bytes(frames).decode("utf-8", "replace")):
                    yield event, data
        except socket.timeout as error:
            raise UpstreamError(f"Timed out after {timeout}s waiting for the platform",
                               account=self._key) from error
        except ssl.SSLError as error:
            raise UpstreamError(f"TLS error talking to the platform: {error}",
                               account=self._key) from error
        finally:
            try:
                raw.close()
            except OSError:
                pass

    @staticmethod
    def _check_payload(payload: Any) -> Any:
        """Raise when the platform answers with its usual ``{"code":..}`` envelope."""
        if isinstance(payload, dict) and "code" in payload and "data" in payload:
            if payload.get("code") not in (200, "200"):
                message = payload.get("msg") or payload.get("message") or "unknown error"
                raise UpstreamError(str(message), code=payload.get("code"),
                                   trace_id=str(payload.get("traceId", "")))
            return payload.get("data")
        return payload

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             timeout: float = 30.0) -> Any:
        url = C.CLOUD_API + path
        if params:
            query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
            if query:
                url += "?" + query
        request = urllib.request.Request(url, method="GET")
        for name, value in self._headers(accept="application/json"):
            request.add_header(name, value)
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            raise UpstreamError(f"HTTP {error.code} from {path}", code=error.code,
                               account=self._key) from error
        except urllib.error.URLError as error:
            raise UpstreamError(f"Cannot reach the platform: {error.reason}",
                               account=self._key) from error
        try:
            return self._check_payload(json.loads(body))
        except json.JSONDecodeError as error:
            raise UpstreamError(f"Platform returned non-JSON from {path}: {body[:200]}",
                               account=self._key) from error

    # -- public API ---------------------------------------------------------
    def agent_info(self) -> Dict[str, Any]:
        """The assistant's own configuration.

        ``appName`` is what the platform shows in the chat header, and
        ``appSimpleParam.datasets`` names the knowledge base the agent
        answers from.
        """
        identity = self._identity()
        # _get already unwraps the {"code":..,"data":..} envelope.
        info = self._get(
            "/llmOps/application/getAssistantBaseInfo",
            {"appNid": identity["app_code"], "nid": identity["app_code"],
             "agentNid": identity["app_code"]},
        ) or {}
        if not isinstance(info, dict):
            return {"nid": identity["app_code"], "name": ""}
        simple = info.get("appSimpleParam") or {}
        datasets = simple.get("datasets") or []
        return {
            "nid": info.get("nid") or identity["app_code"],
            "name": info.get("appName") or "",
            "category": info.get("appCategory") or "",
            "avatar": info.get("avatar") or "",
            "owner_nid": info.get("userNid") or "",
            "knowledge_base": datasets[0].get("name") if datasets else "",
        }

    def _post(self, path: str, payload: Dict[str, Any],
              timeout: float = 60.0) -> Any:
        """POST one JSON body and unwrap the platform envelope."""
        url = C.CLOUD_API + path
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST")
        headers = list(self._headers(accept="application/json"))
        for name, value in headers:
            request.add_header(name, value)
        if not any(name.lower() == "content-type" for name, _ in headers):
            request.add_header("Content-Type", "application/json")
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            raise UpstreamError(f"HTTP {error.code} from {path}", code=error.code,
                                account=self._key) from error
        except urllib.error.URLError as error:
            raise UpstreamError(f"Cannot reach the platform: {error.reason}",
                                account=self._key) from error
        try:
            return self._check_payload(json.loads(body))
        except json.JSONDecodeError as error:
            raise UpstreamError(f"{path} did not return JSON", account=self._key) from error

    def create_assistant(self, name: str = "通用助手",
                         app_category: str = "AGENT_TOOL") -> str:
        """Create one generic agent under this account and return its nid.

        The payload mirrors what the platform's own creation page sends, worked
        out on 2026-11-16: ``appType``/``appCategory`` pick the agent shell,
        ``model: "polymas"`` the platform default, and ``userNid`` must be the
        account that owns the agent. The reply's ``data`` is the nid.

        The agent comes back unpublished and without any built-in tools — that
        is the point: no 「询问用户」 tool to steal tool_calls, and no course
        knowledge base dragging a persona along.
        """
        identity = self._identity()
        suffix = uuid.uuid4().hex[:8]
        payload = {
            "appType": "AGENT",
            "appCategory": app_category,
            "appName": name,
            "appEnName": f"api_direct_{suffix}",
            "isPublish": 0,
            "model": "polymas",
            "bindTools": [],
            "datasets": [],
            "userNid": identity["user_nid"],
        }
        result = self._post(C.SAVE_ASSISTANT_PATH, payload)
        # ``_check_payload`` already unwraps the platform envelope, so ``data``
        # usually arrives here as a bare nid string; a dict is tolerated too in
        # case the envelope shape ever changes.
        if isinstance(result, dict):
            nid = result.get("data") or result.get("nid") or ""
        else:
            nid = result or ""
        if not nid:
            raise UpstreamError(
                f"agent creation returned no nid: {str(result)[:200]}",
                account=self._key)
        return str(nid)

    def models(self) -> Dict[str, Any]:
        """The assistant's model catalogue.

        ``version=v5`` returns the agent's own row (source ``DB``); without it
        the platform answers from the shared NACOS catalogue.

        Unlike 小真's agent, this agent *does* honour ``modelName``: a
        modelCode in that field really switches the backing model.
        Verified 2026-11-13 with isolated sessions and shuffled arms —
        the reply's code style changes with the model (DeepSeek writes
        ``wait = 300``, Qwen writes ``wait`` with no default), naming the
        default model explicitly matches the ``Auto`` arm, and every other
        model separates from it.
        """
        identity = self._identity()
        app_code = identity["app_code"]

        agent_models = self._get(
            C.MODEL_PROPERTIES_PATH,
            {"version": "v5", "appCode": app_code},
        )
        agent_list = (agent_models or {}).get("models") or []

        platform_list = []
        try:
            platform = self._get(C.MODEL_PROPERTIES_PATH, {"appCode": app_code})
            platform_list = (platform or {}).get("models") or []
        except UpstreamError as error:
            self.log(f"the platform catalogue is unavailable: {error}")

        def describe(item: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "modelCode": item.get("modelCode"),
                "modelName": item.get("modelName"),
                "alias": item.get("alias"),
                "default": bool(item.get("modelDefault")),
                "reasoningEffort": item.get("reasoningEffort"),
                "multimodal": bool(item.get("multimodal")),
                "source": item.get("source"),
                "limitedFree": bool(_limited_free(item.get("ext"))),
            }

        agent_described = [describe(item) for item in agent_list if item.get("modelCode")]
        described = [describe(item) for item in platform_list if item.get("modelCode")]
        # The agent's own row comes first: it is the default.
        order = {item["modelCode"]: index for index, item in enumerate(agent_described)}
        described.sort(key=lambda item: (order.get(item["modelCode"], 99),
                                         item["modelCode"] or ""))
        return {
            "models": agent_described,
            "available": described,
            "switchable": True,
        }

    def user_sig(self) -> str:
        """The IM login signature (only needed for the websocket transport)."""
        request = urllib.request.Request(C.CLOUD_API + C.USER_SIG_PATH, data=b"{}", method="POST")
        for name, value in self._headers(accept="application/json"):
            request.add_header(name, value)
        try:
            with _OPENER.open(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            raise UpstreamError(f"HTTP {error.code} from user-sig", code=error.code,
                               account=self._key) from error
        data = self._check_payload(payload) or {}
        return data.get("userSig", "")

    def new_session(self, text: str = "") -> bool:
        """Clear the assistant's memory window (the frontend calls this ``chat/new``)."""
        identity = self._identity()
        body = {"toNid": identity["friend_nid"], "fromNid": identity["user_nid"],
                "msgText": text, "appNid": identity["app_code"]}
        request = urllib.request.Request(
            C.CLOUD_API + C.CHAT_NEW_PATH, data=json.dumps(body).encode("utf-8"), method="POST",
        )
        for name, value in self._headers(accept="application/json"):
            request.add_header(name, value)
        try:
            with _OPENER.open(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            raise UpstreamError(f"HTTP {error.code} from chat/new", code=error.code,
                               account=self._key) from error
        self._check_payload(payload)
        return True

    def chat(self, prompt: str, *, model: str = "", session_id: str = "",
             trace_id: str = "",
             reasoning_effort: Optional[str] = None,
             act_mode: Optional[str] = None,
             chat_mode: Optional[str] = None) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Stream one assistant turn.

        Yields ``(event, payload)`` where ``event`` is the platform's own event
        name (``ANSWER`` / ``REASONING`` / ``ALL_FINISHED`` / ``ERROR``) and
        ``payload`` is the decoded JSON object.

        The three keyword overrides are the *caller's* choice for this one
        request; ``None`` means "use the configured value". The client is the
        right place to decide how much the model should think — a client that
        shows a reasoning-effort selector must be able to see it work.
        """
        identity = self._identity()
        model = model or identity["model"] or ""
        trace = trace_id or str(int(time.time() * 1000))
        act = act_mode if act_mode is not None else self.cfg.act_mode
        chat = chat_mode if chat_mode is not None else self.cfg.chat_mode
        body: Dict[str, Any] = {
            "fromNid": identity["user_nid"],
            "userInput": prompt,
            "toNid": identity["friend_nid"],
            "modelName": model or "Auto",
            "traceId": trace,
            "chatMode": chat,
            "actMode": act,
            "terminal": self.cfg.terminal,
        }
        # Reasoning effort. "none" switches the reasoning channel off entirely
        # (measured 0/4 REASONING events); an empty value sends no field at
        # all. A caller-supplied value wins over the configured one.
        effort = (self.cfg.reasoning_effort if reasoning_effort is None
                  else reasoning_effort)
        if effort:
            body["reasoningEffort"] = effort
        # configMsg carries the switches the agent reads for its own
        # behaviour: useAgent=0 bypasses the agent (no skills, no knowledge
        # base, no reasoning channel), webRetrieve=0 turns web search off.
        body["configMsg"] = {
            "model": model or "Auto",
            "chatMode": chat,
            "actMode": act,
            "terminal": self.cfg.terminal,
            "useAgent": 1 if self.cfg.use_agent else 0,
            "webRetrieve": 1 if self.cfg.web_retrieve else 0,
        }
        # The platform keys the conversation by ``metaData.sessionId`` and
        # falls back to one shared conversation when it is absent — verified
        # 2026-11-16: with no session id, a later request can recall what an
        # earlier, unrelated one said. An OpenAI-compatible endpoint must be
        # stateless by default (the caller sends the whole history), so a
        # fresh id is generated per request unless the caller names one.
        session = session_id.strip() if isinstance(session_id, str) else ""
        isolated = not session and getattr(self.cfg, "session_isolation", True)
        if isolated:
            session = uuid.uuid4().hex
        if session:
            # A fresh session alone is not enough: the platform also keeps a
            # long-term memory of past turns, and the assistant will happily
            # guess "the most recent one" from it, which looks exactly like a
            # leak (measured 2026-11-16 — it named a tag stored by an earlier
            # unrelated request). Stateless means memory off too; a caller
            # that names a session keeps whatever the config asks for.
            retain = (False if isolated
                      else self.cfg.retain_long_term_memory)
            body["metaData"] = {"sessionId": session, "traceId": trace,
                                "bizCode": self.cfg.biz_code,
                                "optPlatform": self.cfg.opt_platform,
                                "retainLongTermMemory": retain}
        # Pace the call so the traffic does not look machine-generated.
        self.shaper.wait(self._key, prompt)
        for event, data in self._stream_post(C.CHAT_STREAM_PATH, body, self.cfg.request_timeout):
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                self.log(f"skipping unparsable SSE data: {data[:120]!r}")
                continue
            if isinstance(payload, dict) and payload.get("code") not in (None, 200, "200"):
                raise UpstreamError(str(payload.get("msg") or payload.get("message") or "stream error"),
                                   code=payload.get("code"),
                                   trace_id=str(payload.get("traceId", "")),
                                   account=self._key)
            yield event, payload
