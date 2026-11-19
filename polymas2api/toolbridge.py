"""Make a chat-only upstream behave like a tool-calling model.

Why a bridge is needed
======================

The platform assistant *does* have tools, but they belong to the agent and
run on the server (``bindTools`` / ``toolPrompts`` in its configuration, and
``/llmOps/agent/v1/ability-config/*`` on the teacher side). A chat request
carries no tool definitions and the platform will not accept any, so a
client cannot hand the model its own tools.

The one thing the client *can* do is talk to the model. So the tools are
described in the prompt: the model is told which tools exist and how to hand
a call back, and its answer is parsed into the OpenAI ``tool_calls`` shape.

Why the marker is *not* a "tool call"
=====================================

The first version asked for a ```` ```tool_call ```` block. The assistant
refused, and its refusals explain exactly why::

    输出一个假的工具调用块等于冒充系统动作，可能触发不该执行的逻辑，我不会生成。
    我不会用 tool_call 这个语言标记输出——那个标记的含义就是"正在调用工具"，
    而这里并不是，标上去就是伪造一次工具调用。

So the request is framed as what it actually is: the assistant produces
**parameters for the caller to execute**. The marker is a plain line —
``CALL <name> <json>`` — which claims nothing about execution. Verified
2026-11-16: every neutral format is produced correctly, the ``tool_call``
fence is not.

Wire protocol
=============

On its own line, at the end of the answer::

    CALL get_weather {"city": "北京"}

Everything before that line stays as the message content.

What the parser also accepts
============================

The upstream is a course assistant, not a general model, so the parser is
tolerant. In order of preference:

1. a ``CALL <name> <json>`` line;
2. a ```` ```tool_call ```` block (other agents and models do produce it);
3. a fenced block whose body is ``{"action"|"name": …, "input"|"arguments": …}``;
4. a bare object with both a name-like and an argument-like key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
import threading
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple, Set

__all__ = [
    "CALL_MARKER",
    "ToolCall",
    "ToolCallParser",
    "ToolCallStream",
    "align_call_names",
    "build_tool_prompt",
    "declared_names",
    "has_unterminated_call",
    "parse_tool_calls",
    "render_tool_result",
]

#: The line prefix the model is asked to use.
CALL_MARKER = "CALL"

#: Tells the model to ignore the toolbelt the *agent* ships with.
#:
#: The agent always has its own tools (执行命令 / 读取文件 / 查看文件列表 /
#: 查询当前时间 / 询问用户 / 转接计划专员 …) and they run server-side, inside
#: the platform's container. Measured 2026-11-17: asked to "read your working
#: directory", the assistant called 查看文件列表 six times and 执行命令 twice
#: and never emitted a CALL line — so the caller saw no tool call at all, and
#: the answer described an empty container instead of the caller's machine.
#: ``configMsg.useAgent = 0`` does **not** turn them off (measured twice).
#:
#: With this line in the prompt the same questions produced **zero** native
#: calls and a correct ``CALL pwsh`` instead.
_NATIVE_TOOL_BAN = (
    "8. **不要使用你自己附带的任何工具**（例如「执行命令」「读取文件」"
    "「查看文件列表」「查询当前时间」「询问用户」「转接计划专员」"
    "「内容搜索」「联网搜索」「网页搜索」等）。"
    "它们运行在平台侧容器里，看不到用户的本机环境，结果对用户没有意义。"
    "需要执行任何操作时，只能按上面的格式输出 CALL 行，交给外部程序去做。"
)

#: Names/descriptions that mean "this caller tool searches the web".
_SEARCH_TOOL_RE = re.compile(
    r"web[_-]?search|search[_-]?web|searxng|tavily|brave|serp|duckduckgo|bing|google"
    r"|web[_-]?fetch|fetch[_-]?web|url[_-]?fetch|fetch[_-]?url|read[_-]?url",
    re.I)


def _search_tool_names(specs: Sequence[Dict[str, Any]]) -> List[str]:
    """The caller's web-search tools, by the name the model should emit.

    Matched on the declared name or on a 搜索/检索 description, so a client
    that names it ``mcp__searxng__search`` or 「联网搜索」 is still found.
    """
    names: List[str] = []
    for spec in specs:
        name = spec["name"]
        description = spec["description"] or ""
        if (_SEARCH_TOOL_RE.search(name)
                or "搜索" in description or "检索" in description):
            names.append(spec["emit_name"])
    return names


#: Names other implementations (and other agents) use for the same thing.
_NAME_KEYS = ("name", "action", "tool", "function", "tool_name", "function_name")
_ARG_KEYS = ("arguments", "args", "input", "parameters", "params", "argument")

#: A tool name: anything but whitespace, braces, brackets, quotes or the
#: punctuation that could end it.
#:
#: It must **not** be ASCII-only. This agent's own tools are Chinese words
#: (``执行命令``, ``读取文件``, ``查询当前时间``) and a caller that declares a
#: tool under such a name gets it echoed back verbatim — an ASCII pattern
#: silently leaves the CALL line in the answer, the client sees no
#: ``tool_calls``, and nothing runs. Caught 2026-11-16.
_NAME_CHARS = r'[^\s{}()\[\]"' + "'" + r'，,：:、。！？]+'

#: ``CALL get_weather {"city":"北京"}`` — the documented form.
#:
#: Group 2 is everything after the name, arguments *and* whatever the model
#: glued on behind them; :func:`_split_call_arguments` delimits the object.
#: Anchoring the object to the end of the line used to drop the whole line when
#: the model wrote ``CALL pwsh {...}已下发本机命令：…`` with no separator, and
#: the call leaked into the answer as plain text (seen 2026-11-18).
_CALL_LINE_RE = re.compile(
    r"^[ \t>*\-]*(?:" + CALL_MARKER + r"[ \t]*[:：]?[ \t]*)"
    r"(" + _NAME_CHARS + r")(.*)$",
    re.MULTILINE,
)
#: A quoted name: ``CALL <执行命令> {...}`` or ``CALL 「执行命令」 {...}``.
_CALL_LINE_QUOTED_RE = re.compile(
    r"^[ \t>*\-]*(?:" + CALL_MARKER + r"[ \t]*[:：]?[ \t]*)"
    r"[<「【\[]([^\s<>\[\]「」【】]+)[>」】\]](.*)$",
    re.MULTILINE,
)


def _object_end(text: str) -> int:
    """Index just past the object's closing brace, or ``-1`` if it never closes.

    Brace depth decides, honouring strings and escapes so that a ``}`` inside a
    command cannot cut the object short.
    """
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def _split_call_arguments(rest: str) -> Tuple[str, str]:
    """Split ``  {"a":1}tail`` into the JSON object and the text after it.

    Returns ``("", "")`` when the line carries no object at all, which is how a
    bare ``CALL name`` is told apart from a real call.
    """
    text = rest.lstrip()
    if not text.startswith("{"):
        return "", ""
    end = _object_end(text)
    if end < 0:
        return "", ""
    return text[:end], text[end:].strip()


#: ``CALL name {`` — the opening brace, without requiring a closing one.
_CALL_LINE_OPEN_RE = re.compile(
    r"^[ \t>*\-]*(?:" + CALL_MARKER + r"[ \t]*[:：]?[ \t]*)"
    r"(?:" + _NAME_CHARS + r")[ \t]*(\{)",
    re.MULTILINE,
)


def has_unterminated_call(text: str) -> bool:
    """True when the answer stops in the middle of a ``CALL``'s argument object.

    A truncated answer leaves the command half-written: nothing can run, and
    the client cannot tell that from "the assistant finished without asking for
    a tool". Reporting such an answer as a normal stop is what lets an agent
    believe the work was done and repeat it next round (seen 2026-11-18: a
    12k-token answer cut off in the middle of a C++ file inside the command).
    The caller turns this into ``finish_reason="length"``.
    """
    if not text:
        return False
    for match in _CALL_LINE_OPEN_RE.finditer(text):
        if _object_end(text[match.start(1):]) < 0:
            return True
    return False


_FENCE_RE = re.compile(r"```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


@dataclass
class ToolCall:
    """One call the model asked for."""

    name: str
    arguments: str  # JSON text, as OpenAI wants it
    id: str = ""
    raw_arguments: Dict[str, Any] = field(default_factory=dict)

    def to_openai(self, index: int = 0) -> Dict[str, Any]:
        """OpenAI shape, with an id the client can key on.

        The id must be unique across the whole conversation. Leaving it empty
        let the client invent one per turn — ``call_0001``, ``call_0002``, then
        ``call_0001`` again next turn — and DSH keys conversation nodes by that
        id, so a later turn's call collided with an earlier node and the event
        feed threw "received an update before its start". The whole conversation
        then stopped rendering: the user saw their new message vanish from both
        the conversation and the trajectory (reported 2026-11-19).
        """
        if not self.id:
            self.id = f"call_{uuid.uuid4().hex[:22]}"
        return {
            "index": index,
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


def _arguments_text(value: Any) -> str:
    """OpenAI sends ``arguments`` as a JSON *string*, not an object."""
    if isinstance(value, str):
        try:
            json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"value": value}, ensure_ascii=False)
        return value
    if value is None:
        return "{}"
    return json.dumps(value, ensure_ascii=False)


#: A backslash that does not begin a legal JSON escape. The model writes regex
#: and Windows paths with a single backslash (``\.exe``, ``C:\Users``) while
#: JSON only allows ``\" \\ \/ \b \f \n \r \t \uXXXX``; a strict parse then
#: rejects the whole call and the line leaks into the answer as plain text
#: (seen 2026-11-18: a PowerShell regex killed the call).
_LEGAL_ESCAPES = frozenset('"\\/bfnrt')
_HEX_DIGITS = frozenset('0123456789abcdefABCDEF')


def _repair_escapes(text: str) -> str:
    """Double every backslash that JSON does not accept as an escape.

    Scanned left to right so a legal ``\\\\`` is consumed as one escape: a
    regex substitution cannot tell the second backslash of ``\\\\`` from a
    stray one and would corrupt it.
    """
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        following = text[index + 1] if index + 1 < length else ""
        if following in _LEGAL_ESCAPES and following:
            out.append(text[index:index + 2])
            index += 2
            continue
        if following == "u" and len(text[index + 2:index + 6]) == 4 \
                and all(digit in _HEX_DIGITS for digit in text[index + 2:index + 6]):
            out.append(text[index:index + 6])
            index += 6
            continue
        out.append("\\\\")  # what the model meant: an escaped backslash
        index += 1
    return "".join(out)


#: Control characters, i.e. what a *legal* JSON escape decodes to.
_CONTROL_RE = re.compile(r"[\x00-\x1f]")

#: A value that starts like a Windows or UNC path.
_PATH_VALUE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\])")

#: Argument names that hold a location rather than prose.
_PATH_KEY_RE = re.compile(
    r"(path|file|dir|folder|workdir|cwd|root|src|source|dest|dst|target)", re.I)


def _doubles_legal_escapes(text: str) -> str:
    r"""Double ``\b \f \n \r \t`` so a path keeps the backslashes it needs.

    ``C:\new\test.txt`` is *legal* JSON: ``\n`` is a newline and ``\t`` a tab,
    so a strict parse succeeds and hands the caller ``C:<LF>ew<HT>est.txt``
    without an error. A left-to-right scan avoids the trap that breaks ``\\``
    (see :func:`_repair_escapes`).
    """
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        following = text[index + 1] if index + 1 < length else ""
        if following and following in "bfnrt":
            out.append("\\\\" + following)   # -> \\n in the JSON text
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _looks_like_a_location(key: Any, value: str) -> bool:
    """Whether a string is a filesystem location rather than prose."""
    return bool(_PATH_VALUE_RE.match(value) or _PATH_KEY_RE.search(str(key or "")))


def _repair_mangled_paths(value: Any, text: str) -> Any:
    r"""Give back the backslashes a legal escape stole from a path argument.

    Reparses with every ``\b \f \n \r \t`` doubled and takes the result **only
    for the arguments that lost one**, so prose that really does carry a
    newline (``content`` written as a multi-line file) is left alone.

    Measured 2026-11-19: four of five Windows paths lost characters this way
    (``C:\new`` -> newline, ``C:\bin`` -> backspace, ``C:\robots`` -> two
    carriage returns) and nothing reported an error.
    """
    if not isinstance(value, dict):
        return value
    mangled = [key for key, item in value.items()
               if isinstance(item, str) and _CONTROL_RE.search(item)]
    if not mangled:
        return value
    try:
        repaired = json.loads(_doubles_legal_escapes(text.strip()))
    except (json.JSONDecodeError, TypeError, AttributeError):
        return value
    if not isinstance(repaired, dict):
        return value
    merged = dict(value)
    for key in mangled:
        candidate = repaired.get(key)
        if not isinstance(candidate, str) or _CONTROL_RE.search(candidate):
            continue
        # Take the repair when the argument reads as a location, or when the
        # repair is what reveals one (a path buried inside a command string).
        if _looks_like_a_location(key, value[key]) \
                or _PATH_VALUE_RE.search(candidate):
            merged[key] = candidate
    return merged


def _loads(text: str) -> Any:
    r"""Decode a call's arguments, tolerating a stray backslash.

    Strict first: a body that is already valid JSON is never rewritten, so
    nothing that parses today can change meaning. Only when that fails is each
    illegal escape doubled — which is what the model meant — so ``\.exe``
    reaches the command as ``\\.exe`` instead of dropping the call.

    A *successful* parse is still not the end of the story: ``\n`` inside a
    Windows path is legal and therefore silently wrong. :func:`_repair_mangled_paths`
    checks the decoded values for the control characters that only a path could
    have lost and takes the double-escaped reading for those arguments.
    """
    stripped = text.strip()
    try:
        return _repair_mangled_paths(json.loads(stripped), stripped)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    try:
        repaired = _repair_escapes(stripped)
        # Hand the *repaired* text on: a path can carry both an illegal escape
        # and a legal-but-wrong one (``C:\temp\a.log`` has ``\a`` and ``\t``),
        # and the guard has to see the text with the illegal ones already fixed.
        return _repair_mangled_paths(json.loads(repaired), repaired)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


def _from_object(payload: Any) -> Optional[ToolCall]:
    """Build a call from a decoded object, if it looks like one.

    A name-like key **and** an argument-like key are both required, so an
    ordinary data answer such as ``{"name": "张三", "score": 90}`` is not
    mistaken for a call.
    """
    if not isinstance(payload, dict):
        return None
    name = None
    for key in _NAME_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
        if isinstance(value, dict):  # {"function": {"name":..,"arguments":..}}
            nested = value.get("name")
            if isinstance(nested, str) and nested.strip():
                name = nested.strip()
                return ToolCall(
                    name=name,
                    arguments=_arguments_text(value.get("arguments")),
                    id=str(payload.get("id") or ""),
                    raw_arguments=value.get("arguments")
                    if isinstance(value.get("arguments"), dict) else {},
                )
    if not name:
        return None
    for key in _ARG_KEYS:
        if key in payload:
            arguments = payload[key]
            return ToolCall(
                name=name,
                arguments=_arguments_text(arguments),
                id=str(payload.get("id") or ""),
                raw_arguments=arguments if isinstance(arguments, dict) else {},
            )
    return None


def _json_objects(text: str) -> List[Any]:
    found: List[Any] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        found.append(value)
        index = end
    return found


#: A fence that has become empty after its CALL line was removed —
#: ```` ``` ````, ```` ```json ````, or the same across two lines.
_EMPTY_FENCE_RE = re.compile(r"```[A-Za-z0-9_+.\-]*[ \t]*\n?[ \t]*\n?```")

#: A CALL line the model wrapped in single backticks. Unwrapped before
#: matching so it is not left sitting in the answer as inline code.
_INLINE_CODE_CALL_RE = re.compile(r"`(CALL\s[^`\n]+)`")


def parse_tool_calls(text: str) -> Tuple[str, List[ToolCall]]:
    """Split an answer into ``(content, calls)``."""
    if not text:
        return "", []

    text = _INLINE_CODE_CALL_RE.sub(r"\1", text)

    # 1. The documented ``CALL name {json}`` line.
    calls: List[ToolCall] = []
    # Quoted first: ``<名称>`` and ``「名称」`` must not keep their brackets.
    for regex in (_CALL_LINE_QUOTED_RE, _CALL_LINE_RE):
        for match in regex.finditer(text):
            arguments, _tail = _split_call_arguments(match.group(2))
            if not arguments:
                continue
            payload = _loads(arguments)
            if payload is None:
                continue
            calls.append(ToolCall(
                name=match.group(1),
                arguments=_arguments_text(payload),
                raw_arguments=payload if isinstance(payload, dict) else {},
            ))
        if calls:
            def drop_call_line(match: "re.Match[str]") -> str:
                """Remove the call, keep any prose the model glued behind it."""
                arguments, tail = _split_call_arguments(match.group(2))
                if not arguments or _loads(arguments) is None:
                    return match.group(0)  # not a call after all: leave the line
                return tail

            remaining = text
            for regex in (_CALL_LINE_QUOTED_RE, _CALL_LINE_RE):
                remaining = regex.sub(drop_call_line, remaining)
            # The model is told not to wrap the line in a fence (rule 3) but
            # sometimes does anyway. Removing the line then leaves an empty
            # pair of fences behind, and the client renders that as a stray
            # code block in the middle of the answer (reported 2026-11-17).
            remaining = _EMPTY_FENCE_RE.sub("", remaining)
            return _tidy(remaining), _assign_ids(calls)

    # 2. A fenced block holding a call, whatever the fence tag says.
    for match in _FENCE_RE.finditer(text):
        call = _from_object(_loads(match.group(1)))
        if call is not None:
            calls.append(call)
    if calls:
        return _tidy(_FENCE_RE.sub("", text)), _assign_ids(calls)

    # 3. A bare object, only when it is essentially the whole answer.
    stripped = text.strip()
    for candidate in _json_objects(stripped):
        call = _from_object(candidate)
        if call is not None:
            spans = json.dumps(candidate, ensure_ascii=False)
            remaining = stripped.replace(spans, "")
            if len(remaining) > len(stripped) * 0.5:
                continue  # too much other text: this is data, not a call
            return _tidy(remaining), _assign_ids([call])
    return _tidy(text), []


def _tidy(text: str) -> str:
    """Collapse the blank space a removed line leaves behind."""
    lines = [line.rstrip() for line in text.split("\n")]
    cleaned: List[str] = []
    for line in lines:
        if not line and cleaned and not cleaned[-1]:
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _assign_ids(calls: Sequence[ToolCall]) -> List[ToolCall]:
    """Give every call an id that is unique for the life of the conversation.

    This used to be ``call_0001``, ``call_0002`` … restarting with every
    response. OpenAI only requires an id, but clients treat it as a key: DSH
    builds one conversation node per ``callId``, so the second turn's
    ``call_0001`` arrived as an update to a node whose start belonged to the
    first turn, the event feed threw "received an update before its start", and
    the whole conversation stopped rendering — the user's new message vanished
    from both the conversation and the trajectory (reported 2026-11-19).

    Random rather than a counter: the gateway is stateless across restarts, so
    a counter would collide again the moment the process came back.
    """
    for call in calls:
        if not call.id:
            call.id = new_call_id()
    return list(calls)


#: Ids already handed out by this process, so a repeated random draw can never
#: reach a client. 20 hex characters is 80 bits of randomness — a million calls
#: in one conversation collide with probability 4e-13 — but "practically never"
#: is not "never", and the failure mode is a client that stops rendering.
_ISSUED_IDS: Set[str] = set()
_ISSUED_LOCK = threading.Lock()
_ISSUED_CAP = 200_000


def new_call_id() -> str:
    """A tool call id that is unique, at least within this process.

    Randomness alone is the guarantee across restarts: the gateway keeps no
    state, so a counter would restart at one and collide with the previous
    run's ids — the exact bug this replaced. The set turns "the same 80-bit
    draw happened twice" from unlikely into impossible, at the cost of a few
    bytes per call.
    """
    global _ISSUED_IDS
    with _ISSUED_LOCK:
        if len(_ISSUED_IDS) > _ISSUED_CAP:
            # Bounded memory; a collision against a forgotten id is back to the
            # 80-bit probability, which is fine.
            _ISSUED_IDS = set()
        # Bump rather than redraw: a redraw loop would never end if the random
        # source ever repeated itself, which is exactly the case worth surviving.
        base = f"call_{uuid.uuid4().hex[:20]}"
        candidate = base
        bump = 0
        while candidate in _ISSUED_IDS:
            bump += 1
            candidate = f"{base}x{bump}"
        _ISSUED_IDS.add(candidate)
        return candidate


class ToolCallParser:
    """Stateful driver, so a stream can be parsed as it arrives.

    A ``CALL`` line can straddle two upstream frames, so the tail is held
    back until it can no longer be the start of one.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        """Take a fragment, return the part that is definitely plain text."""
        self._buffer += chunk
        # Hold back from the last line start that could still become a CALL.
        index = self._call_start()
        if index < 0:
            keep = len(CALL_MARKER) + 1
            if len(self._buffer) <= keep:
                return ""
            emit = self._buffer[:-keep]
            self._buffer = self._buffer[-keep:]
            return emit
        emit = self._buffer[:index]
        self._buffer = self._buffer[index:]
        return emit

    def _call_start(self) -> int:
        """Index of a complete ``CALL`` marker at the start of a line."""
        # No ``\b`` here: between "CALL" and a Chinese name both sides are
        # word characters, so ``\b`` never matches.
        pattern = re.compile(r"(?:^|\n)[ \t>*\-]*" + CALL_MARKER + r"(?=[\s:：])")
        match = pattern.search(self._buffer)
        if match:
            return match.start() + (1 if self._buffer[match.start()] in "\r\n" else 0)
        return -1

    def flush(self) -> Tuple[str, List[ToolCall]]:
        """Release what is left and parse any call in it."""
        rest = self._buffer
        self._buffer = ""
        if not rest.strip():
            return "", []
        return parse_tool_calls(rest)


class ToolCallStream:
    """Wraps a text stream so call lines never leak into the content."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._parser = ToolCallParser() if enabled else None

    def feed(self, text: str) -> str:
        if not self.enabled or self._parser is None:
            return text
        return self._parser.feed(text)

    def flush(self) -> Tuple[str, List[ToolCall]]:
        if not self.enabled or self._parser is None:
            return "", []
        return self._parser.flush()


#: A tool name that a strict OpenAI client will accept.
_ASCII_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


#: How much room the tool block may take before the schemas are compacted.
#:
#: A real agent client sends ~112 tools; rendering their schemas verbatim
#: produced a 109,634-character instruction block, and with that in the
#: prompt the assistant would *say* "I will send you a card" and never emit
#: the call (measured 2026-11-16). Compacting keeps the instructions close
#: enough to the question to be obeyed.
TOOL_BLOCK_BUDGET = 8000


def _one_line(text: str, limit: int = 100) -> str:
    """First sentence of a description, on one line, capped."""
    flat = " ".join(str(text or "").split())
    for stop in ("。", ". ", "；", "; "):
        cut = flat.find(stop)
        if 0 < cut < limit:
            flat = flat[:cut + 1]
            break
    if len(flat) > limit:
        flat = flat[:limit].rstrip() + "…"
    return flat


#: How many levels of nested parameters to spell out.
#:
#: Measured 2026-11-17 against DSH's 112 declared tools:
#:
#:     depth 0 (flat)   17,186 chars      — the old behaviour
#:     depth 1          18,232 chars  (+6%)
#:     depth 2          18,423 chars  (+7%)
#:     depth 3 / full   18,423 chars  (+7%)
#:
#: Nothing in that set nests deeper than two levels, so depth 2 already shows
#: everything at 7% of the cost. Depth 0 was the wrong default for a different
#: reason than size: a tool like ``ask_user_question`` rendered as
#: ``questions:array<object>`` hides ``id``/``question`` entirely, leaving the
#: model to guess the inner shape.
#: Two levels of *expansion* already name three levels of fields, which is why
#: depth 2 measured complete for DSH's 112 tools. It is not complete for a tool
#: that nests four deep: measured 2026-11-19 on a `plan.strategy.rolling.batch`
#: signature, depth 2 filled the leaf fields 1/10 times, depth 3 filled them
#: 10/10 — and depth 3 costs 28 characters on an 83-tool block (+0.4%), nothing
#: at all on the DSH set. Depth 1 loses the third level outright (5/25 full
#: credit against 15/25 at depth 2), so the cheap insurance is worth taking.
SCHEMA_DEPTH = 3


def _compact_parameters(schema: Any, depth: int = SCHEMA_DEPTH,
                        level: int = 0) -> str:
    """A ``name:type`` signature, nested up to ``depth`` levels.

    Deep enough to show the fields a caller actually has to fill in, shallow
    enough that the whole block stays around 18K characters for 112 tools —
    rendering every subschema in full is what made it unusable (113K).
    """
    if not isinstance(schema, dict):
        return ""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "无参数"
    required = set(schema.get("required") or [])
    parts: List[str] = []
    for key, spec in properties.items():
        kind = ""
        inner_block = ""
        if isinstance(spec, dict):
            kind = str(spec.get("type") or ("object" if "properties" in spec else ""))
            if kind == "array":
                items = spec.get("items")
                inner = ""
                if isinstance(items, dict):
                    inner = str(items.get("type") or (
                        "object" if "properties" in items else ""))
                    # The interesting case: an array of records. Show the
                    # record's own fields rather than a bare ``array<object>``.
                    if level < depth and isinstance(items.get("properties"), dict):
                        inner_block = _compact_parameters(items, depth, level + 1)
                kind = f"array<{inner}>" if inner else "array"
                if inner_block == "无参数":
                    inner_block = ""
            elif level < depth and isinstance(spec.get("properties"), dict):
                inner_block = _compact_parameters(spec, depth, level + 1)
                if inner_block == "无参数":
                    inner_block = ""
        entry = f"{key}:{kind or 'any'}" + ("*" if key in required else "")
        entry += _enum_hint(spec)
        if inner_block:
            entry += "{" + inner_block + "}"
        parts.append(entry)
    return " ".join(parts)


#: How wide an enum hint may get before it abbreviates. **Width, not count**:
#: six one-character values cost twelve characters and are worth naming in full
#: (`[0|1|2|3|4|5|6]`), while three long values can already be noise. Counting
#: values was the first cut and it truncated a 13-character set to `[0|1|2…]`
#: (reported 2026-11-19 from a sibling project).
_ENUM_WIDTH = 60

#: A hard cap, so a list of many short values cannot eat the block.
_ENUM_SHOWN = 12


def _scalar_text(value: Any) -> str:
    """Render an enum member the way JSON writes it.

    ``enum: [true, false]`` on a boolean parameter has to read ``[true|false]``.
    ``str(True)`` gives ``True``, and a model copying that produces the *string*
    ``"True"`` — a type error at the caller, for a field whose type the signature
    had just declared as boolean.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def _enum_hint(spec: Any) -> str:
    """``[a|b|c]`` for a closed set, or ``""`` when the field is open.

    Measured 2026-11-19: the signature used to name only the type, so a
    ``mode`` that accepts ``turbo|standard|thorough`` was rendered ``mode:string``
    and the model answered ``accurate``/``fast``/``quick`` — ten runs out of ten
    invalid on two prompts, while the full JSON schema got all ten right.
    Naming the members costs 59 characters on a 6.3K block and recovered 15/15.
    """
    if not isinstance(spec, dict):
        return ""
    values = spec.get("enum")
    if not isinstance(values, list) or not values:
        return ""
    shown: List[str] = []
    width = 0
    for value in values:
        text = _scalar_text(value)
        if not shown:
            if len(text) > _ENUM_WIDTH:
                return f"[{text[:_ENUM_WIDTH - 1]}…]"
            shown.append(text)
            width = len(text)
            continue
        if len(shown) >= _ENUM_SHOWN or width + 1 + len(text) > _ENUM_WIDTH:
            return "[" + "|".join(shown) + "|…]"
        shown.append(text)
        width += 1 + len(text)
    return "[" + "|".join(shown) + "]"

def ascii_alias(name: str, index: int) -> str:
    """A deterministic ASCII stand-in for a non-ASCII tool name.

    OpenAI requires ``^[a-zA-Z0-9_-]{1,64}$``, so a name like ``执行命令``
    cannot travel on the wire without upsetting strict clients. The alias is
    derived from a small table of the agent's own tools — which is what a
    caller mirroring them will usually declare — and falls back to a
    positional name.
    """
    known = {
        "执行命令": "run_command",
        "读取文件": "read_file",
        "写入文件": "write_file",
        "查询当前时间": "get_current_time",
        "电子工艺实习": "search_course_kb",
        "网页搜索": "web_search",
        "生成图片": "generate_image",
    }
    if name in known:
        return known[name]
    cleaned = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    if cleaned and _ASCII_NAME_RE.match(cleaned):
        return cleaned
    return f"tool_{index}"


def build_tool_prompt(tools: Sequence[Dict[str, Any]],
                      tool_choice: Any = None,
                      name_style: str = "verbatim",
                      schema_depth: int = SCHEMA_DEPTH,
                      ) -> Tuple[str, Dict[str, str]]:
    """Describe the caller's tools to the model.

    The framing matters: the assistant refuses to *claim* it is calling a
    tool, so it is asked to produce the parameters for someone else to
    execute.

    Returns ``(prompt, aliases)`` where ``aliases`` maps the name the model
    was told to use onto the name the caller declared. With
    ``name_style="verbatim"`` the two are the same and the map is empty;
    with ``"ascii"`` a non-ASCII name is replaced by an ASCII stand-in, so
    the wire format stays valid for strict OpenAI clients.

    Returns an empty prompt when there is nothing to say, so a request
    without tools is byte-identical to one made before this existed.
    """
    if _choice_mode(tool_choice) == "none":
        return "", {}
    specs = [_normalise_tool(tool) for tool in tools or []]
    specs = [spec for spec in specs if spec]
    if not specs:
        return "", {}

    # Decide what name the model is asked to emit.
    aliases: Dict[str, str] = {}
    for index, spec in enumerate(specs, start=1):
        declared = spec["name"]
        if name_style == "ascii" and not _ASCII_NAME_RE.match(declared):
            alias = ascii_alias(declared, index)
            spec["emit_name"] = alias
            aliases[alias] = declared
        else:
            spec["emit_name"] = declared

    # Render the tool list two ways and keep whichever fits the budget: a
    # 112-tool client produced a 110 KB block, and the assistant stopped
    # obeying it.
    def render(compact: bool) -> List[str]:
        out: List[str] = []
        for spec in specs:
            description = (spec["description"] or "（无说明）")
            if compact:
                out.append(f"- {spec['emit_name']}：{_one_line(description)}")
                out.append("  参数：" + _compact_parameters(
                    spec.get("parameters"), schema_depth))
            else:
                out.append(f"- {spec['emit_name']}：{description}")
                out.append("  参数：" + json.dumps(
                    spec.get("parameters") or {}, ensure_ascii=False,
                    separators=(",", ":")))
            out.append("")
        return out

    full = render(compact=False)

    lines: List[str] = [
        "[调用参数整理]",
        "你的回答会由一个外部程序读取。当用户的请求需要下面这些能力时，",
        "不要自己编造结果，也不要解释，而是在回复最前面单独用一行给出调用参数，",
        "由那个程序去真正执行：",
        "",
        "CALL 工具名 {\"参数名\": \"参数值\"}",
        "",
        "可用工具：",
    ]
    body_render = full if sum(len(x) for x in full) <= TOOL_BLOCK_BUDGET else render(True)
    lines.extend(body_render)

    lines.extend([
        "要求：",
        "1. 这一行以 CALL 加空格开头，后面是工具名和一段单行 JSON。",
        "2. 参数必须符合上面给出的结构，JSON 用双引号。",
        "3. 这一行必须是回复的最前面：它之前不能有任何文字、标题、空行或代码块标记；",
        "   这一行之外不需要代码块包裹，也不要加引号。写完这一行可以继续写正文说明。",
        # 并行能力早就具备（parse_tool_calls 返回列表、响应层逐个下发），
        # 是这条规则把它白关掉了。互不依赖时多写几行能省整轮往返；
        # 有依赖时仍必须串行，否则第二个调用的参数无从写起。
        "4. 多个调用互不依赖时，可以在最前面连续写多行 CALL（最多 4 行），",
        "   每行一个调用、各自带完整 JSON；后一个依赖前一个的结果时（例如先列目录",
        "   再读其中某个文件），只写第一个，等结果回来再继续。",
        "5. 不需要任何工具时，正常回答，不要输出 CALL 行。",
        "6. 调用参数由外部程序执行，你把参数整理正确即可。",
        "7. 工具名**必须逐字照抄**上面列出的那个名字：不要翻译、不要改写成别的",
        "   语言、不要加引号或尖括号。它会原样传给外部程序，写错就找不到工具。",
        _NATIVE_TOOL_BAN,
    ])
    number = 9
    search_tools = _search_tool_names(specs)
    if search_tools:
        # Measured 2026-11-18: without this the assistant either searched with
        # its own 内容搜索 or handed the caller a curl one-liner. Naming the
        # caller's tool explicitly is what makes it delegate instead.
        lines.append(
            f"{number}. **需要联网检索或抓取网页时，只能用上面列出的 "
            + "、".join(search_tools)
            + " 发 CALL**，由外部程序去联网。不要用你自己内置的搜索，"
            "也不要把网址、curl、wget、Invoke-WebRequest、Invoke-RestMethod "
            "写进任何命令里让外部程序代抓。")
        number += 1
    if name_style == "ascii" or all(
            _ASCII_NAME_RE.match(spec["emit_name"]) for spec in specs):
        lines.append(f"{number}. 上面的工具名是英文标识符，即使你的回答用中文，"
                     "工具名也保持英文。")
        number += 1
    if _choice_mode(tool_choice) == "required":
        lines.append(f"{number}. 这一轮必须给出一行 CALL，不允许只回答。")
    return "\n".join(lines), aliases

def _choice_mode(tool_choice: Any) -> str:
    if isinstance(tool_choice, str):
        lowered = tool_choice.lower()
        if lowered in ("auto", "none", "required"):
            return lowered
        return "auto"
    if isinstance(tool_choice, dict):
        return "required"
    return "auto"


def _normalise_tool(tool: Any) -> Optional[Dict[str, Any]]:
    """Accept both the ``function`` wrapper and the flattened shape."""
    if not isinstance(tool, dict):
        return None
    source = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    name = source.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    parameters = source.get("parameters")
    if parameters is None:
        parameters = source.get("input_schema")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return {
        "name": name.strip(),
        "description": str(source.get("description") or "").strip(),
        "parameters": parameters,
    }


def declared_names(tools: Sequence[Dict[str, Any]]) -> List[str]:
    """The tool names the caller declared, in order, deduplicated."""
    names: List[str] = []
    for tool in tools or []:
        spec = _normalise_tool(tool)
        if spec and spec["name"] not in names:
            names.append(spec["name"])
    return names


def align_call_names(calls: List[ToolCall],
                     names: Sequence[str]) -> List[ToolCall]:
    """Point each call at a name the caller actually declared.

    The model is told to copy the name verbatim, but it is a Chinese-first
    assistant: asked to emit "the English identifier" it once answered
    ``execute`` for a tool declared as ``执行命令`` (2026-11-16). A client
    matches names literally, so a near-miss is useless.

    Repair is deliberately conservative:

    * an exact (case-insensitive) match wins, and the caller's spelling is
      restored;
    * otherwise, **only when a single tool was declared**, the call is
      re-pointed at it — with one candidate there is no ambiguity.

    Anything else is left alone: guessing between several tools would be
    worse than passing the name through.
    """
    names = [name for name in names or [] if name]
    if not names:
        return calls
    lowered = {name.lower(): name for name in names}
    for call in calls:
        matched = lowered.get(call.name.lower())
        if matched:
            call.name = matched
        elif len(names) == 1:
            call.name = names[0]
    return calls

def render_tool_result(name: str, content: str, call_id: str = "") -> str:
    """Render a tool result the way the model is told to expect it."""
    label = f"（id {call_id}）" if call_id else ""
    return f"[工具结果] {name}{label}\n{content}".strip()
