"""Translate the platform's SSE events into OpenAI chat-completion chunks.

The platform speaks its own dialect: ``event: ANSWER`` carries a ``text``
delta, ``event: REASONING`` carries the thinking trace, ``event: ALL_FINISHED``
closes the turn. There is no ``choices`` array, no ``usage`` and no ``model``
field, so the adapter has to build all three.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import constants as C
from .tokens import usage_for
from .attachments import part_to_text
from .persona import NameScrubber, StreamWasher
from .toolbridge import (ToolCallStream, align_call_names, declared_names,
                         has_unterminated_call, parse_tool_calls)

__all__ = ["OpenAIAdapter"]


class OpenAIAdapter:
    """Wraps :meth:`PolymasClient.chat` and emits OpenAI-shaped payloads."""

    def __init__(self, config, logger=None, washer=None, tool_names=None):
        self.cfg = config
        self.log = logger or (lambda message, **_: None)
        # Optional: strips the upstream assistant's persona from the reply.
        self.washer = washer
        #: The names the prompt actually asked the model to emit. With
        #: ``tool_name_style="ascii"`` these are the ASCII aliases, so a call
        #: must be matched against *them*, not against what the caller
        #: declared.
        self.emit_tool_names = list(tool_names or [])

    def _tool_names(self, tools: Any):
        return self.emit_tool_names or declared_names(tools)

    # -- chunk builders -----------------------------------------------------
    def _chunk(self, model: str, *, delta: Dict[str, Any],
               finish_reason: Optional[str] = None,
               usage: Optional[Dict[str, int]] = None,
               upstream_tool: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        chunk: Dict[str, Any] = {
            "id": "chatcmpl-polymas",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            chunk["usage"] = usage
        if upstream_tool is not None:
            # The agent used one of *its* tools. Never ask the client to run
            # it — the platform already did.
            #
            # ``_upstream_tool`` is internal: the gateway records the name for
            # the audit column and strips the key before the chunk leaves.
            # The public ``polymas`` block, which carries the agent's raw
            # input/output JSON, only goes out when the operator asked for it.
            chunk["_upstream_tool"] = upstream_tool
            if getattr(self.cfg, "expose_upstream_tools", True):
                chunk["polymas"] = {"upstream_tool": upstream_tool}
        return chunk

    # -- streaming ----------------------------------------------------------
    def stream(self, client, prompt: str, *, model: str = "",
               session_id: str = "", tools: Any = None,
               overrides: Optional[Dict[str, Any]] = None,
               ) -> Iterator[Dict[str, Any]]:
        """Yield OpenAI chunk dicts for one streamed turn."""
        model = model or self.cfg.model or C.DEFAULT_MODEL
        # A persona phrase can straddle two upstream frames, so the answer is
        # washed through a buffer that holds back a tail until it is safe to
        # emit.
        answer_washer = StreamWasher(self.washer, "text") if self.washer else None
        # The reasoning trace needs the same buffering: a name split across
        # two frames ("林晓" + "东") defeats a per-frame replace.
        reasoning_washer = StreamWasher(self.washer, "reasoning") if (
            self.washer and self.washer.names
        ) else None
        # Tool calls are carried inside the answer text, so they have to be
        # pulled out of the same stream the washer is holding.
        tool_stream = ToolCallStream(enabled=bool(tools))
        # What actually went out on the wire, for the usage estimate at the end.
        sent_content: List[str] = []
        sent_reasoning: List[str] = []
        upstream_tools: List[Dict[str, Any]] = []
        for event, payload in client.chat(prompt, model=model, session_id=session_id,
                                              **(overrides or {})):
            text = payload.get("text") or ""
            if event in (C.SSE_EVENT_TOOL_REQUEST, C.SSE_EVENT_TOOL_RESPONSE):
                tool = self._upstream_tool(event, payload)
                if tool:
                    upstream_tools.append(tool)
                    yield self._chunk(model, delta={}, upstream_tool=tool)
                continue
            if event == C.SSE_EVENT_REASONING:
                if reasoning_washer is not None:
                    text = reasoning_washer.feed(text)
                elif self.washer is not None:
                    text = self.washer.wash_reasoning(text)
            elif event == C.SSE_EVENT_ANSWER:
                if answer_washer is not None:
                    text = answer_washer.feed(text)
                elif self.washer is not None:
                    text = self.washer.wash_text(text)
                text = tool_stream.feed(text)
            if event == C.SSE_EVENT_REASONING and text:
                sent_reasoning.append(text)
                yield self._chunk(model, delta={"role": "assistant", "reasoning_content": text})
            elif event == C.SSE_EVENT_ANSWER and text:
                sent_content.append(text)
                yield self._chunk(model, delta={"content": text})
            elif event == C.SSE_EVENT_ERROR:
                message = text or payload.get("msg") or "the assistant reported an error"
                yield self._chunk(model, delta={}, finish_reason="error")
                yield {"error": {"message": str(message), "type": "upstream_error"}}
                return
            elif event == C.SSE_EVENT_FINISHED:
                break

        # Release whatever the buffers were holding back.
        if answer_washer is not None:
            tail = answer_washer.flush()
            if tail:
                flushed = tool_stream.feed(tail)
                sent_content.append(flushed)
                yield self._chunk(model, delta={"content": flushed})
        tail, calls = tool_stream.flush()
        if tail:
            sent_content.append(tail)
            yield self._chunk(model, delta={"content": tail})
        if calls and tools:
            calls = align_call_names(calls, self._tool_names(tools))
        if reasoning_washer is not None:
            tail = reasoning_washer.flush()
            if tail:
                sent_reasoning.append(tail)
                yield self._chunk(model, delta={"role": "assistant", "reasoning_content": tail})

        usage = usage_for(prompt, "".join(sent_content),
                          "".join(sent_reasoning))
        if calls:
            for index, call in enumerate(calls):
                yield self._chunk(model, delta={"tool_calls": [call.to_openai(index)]})
            yield self._chunk(model, delta={}, finish_reason="tool_calls", usage=usage)
            return
        yield self._chunk(model, delta={},
                          finish_reason=self._finish_reason(
                              tools, "".join(sent_content), calls,
                              bool(getattr(client, "last_stream_finished", True))),
                          usage=usage)

    def _finish_reason(self, tools: Any, content: str, calls: List[Any],
                       completed: bool = True) -> str:
        """``stop`` normally, ``length`` when the answer did not really finish.

        Two ways an answer arrives short of its own end: the platform closed the
        stream without its ``ALL_FINISHED`` marker, or the text dies inside a
        ``CALL``'s argument object. Both leave the client unable to tell "the
        model finished" from "the answer was cut", which is what let an agent
        believe the work was done and repeat it (seen 2026-11-18/19). ``length``
        is how OpenAI reports a truncated answer, so a client maps it to an
        output limit instead of a completion.
        """
        if calls:
            return "tool_calls"
        if not completed:
            return "length"
        if tools and has_unterminated_call(content):
            return "length"
        return "stop"

    def _upstream_tool(self, event: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Describe one of the *agent's own* tool calls.

        Those run on the platform, already done by the time we see them, so
        they are reported as metadata rather than as ``tool_calls``: a client
        must not be asked to execute a tool it has never heard of.
        """
        # Always parsed: the gateway records the name for the audit column and
        # must keep doing so when exposure is off. Whether the payload reaches
        # the client is decided by the caller.
        data = payload.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            data = {}
        return {
            "event": event,
            "id": data.get("id") or "",
            "name": data.get("name") or "",
            "input": data.get("input"),
            "output": data.get("output"),
        }

    # -- non streaming ------------------------------------------------------
    def complete(self, client, prompt: str, *, model: str = "",
                 session_id: str = "", tools: Any = None,
                 overrides: Optional[Dict[str, Any]] = None,
                 ) -> Dict[str, Any]:
        """Collect the whole turn into a single chat-completion object."""
        model = model or self.cfg.model or C.DEFAULT_MODEL
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        error_message = ""
        upstream_tools: List[Dict[str, Any]] = []
        for event, payload in client.chat(prompt, model=model, session_id=session_id,
                                              **(overrides or {})):
            text = payload.get("text") or ""
            if event in (C.SSE_EVENT_TOOL_REQUEST, C.SSE_EVENT_TOOL_RESPONSE):
                tool = self._upstream_tool(event, payload)
                if tool:
                    upstream_tools.append(tool)
            elif event == C.SSE_EVENT_REASONING:
                reasoning_parts.append(text)
            elif event == C.SSE_EVENT_ANSWER:
                content_parts.append(text)
            elif event == C.SSE_EVENT_ERROR:
                error_message = text or "the assistant reported an error"
                break
            elif event == C.SSE_EVENT_FINISHED:
                break

        # Wash the *joined* text: the upstream splits at arbitrary
        # boundaries, so a persona phrase can straddle two frames and a
        # per-frame wash would miss it.
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        if self.washer is not None:
            content = self.washer.wash_text(content)
            reasoning = self.washer.wash_reasoning(reasoning)

        if error_message:
            return {"error": {"message": error_message, "type": "upstream_error"}}

        # Count before the CALL line is stripped: it is generated text too.
        usage = usage_for(prompt, content, reasoning)

        calls: List[Any] = []
        # Measured on the text as it arrived: once a call parses, the line is
        # gone from ``content`` and the truncation would look like a clean end.
        raw_content = content
        if tools:
            # The call travels inside the answer text; take it out again.
            content, calls = parse_tool_calls(content)
            # The model sometimes anglifies a Chinese name; point it back at
            # what the caller actually declared.
            calls = align_call_names(calls, self._tool_names(tools))

        message: Dict[str, Any] = {"role": "assistant", "content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        finish_reason = self._finish_reason(
            tools, raw_content, calls,
            bool(getattr(client, "last_stream_finished", True)))
        if calls:
            message["tool_calls"] = [call.to_openai(index)
                                     for index, call in enumerate(calls)]
        result: Dict[str, Any] = {
            "id": "chatcmpl-polymas",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }
        if upstream_tools:
            # Same split as the streaming path: the audit list always travels
            # internally (`_upstream_tools`), the public block only when the
            # operator asked for it.
            result["_upstream_tools"] = upstream_tools
            if getattr(self.cfg, "expose_upstream_tools", True):
                result["polymas"] = {"upstream_tools": upstream_tools}
        return result

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def extract_prompt(messages: Any, extra_system: str = "",
                       local_images: bool = True) -> str:
        """Flatten an OpenAI ``messages`` array into one prompt string.

        The platform assistant is a course assistant with its own memory, so
        the whole conversation is folded into the last exchange: system
        messages become a leading instruction block, earlier turns become
        labelled context.

        Everything from the last user message onwards is kept, not just that
        message. A tool round-trip puts an ``assistant`` tool call and a
        ``tool`` result *after* the question, and dropping them would make
        the model answer the original question again instead of using the
        result it was given.
        """
        if not isinstance(messages, list) or not messages:
            return extra_system or ""
        system: List[str] = []
        turns: List[Tuple[str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content")
            if isinstance(content, list):  # content parts
                # Images cannot travel: the platform takes one plain-text
                # ``userInput``. They are written to a local file and handed
                # over as a path the agent's own tools can read (verified
                # 2026-11-17). Text parts pass through untouched.
                content = "".join(
                    part_to_text(part, local_images)
                    for part in content if isinstance(part, dict)
                )
            text = "" if content is None else str(content)
            if role == "system":
                system.append(text)
            elif role == "tool":
                name = str(message.get("name") or message.get("tool_call_id") or "tool")
                turns.append(("tool", f"[工具结果] {name}\n{text}"))
            elif role == "assistant":
                calls = message.get("tool_calls")
                if isinstance(calls, list) and calls:
                    text = _render_calls(text, calls)
                turns.append(("assistant", text))
            elif role == "user":
                turns.append(("user", text))

        if extra_system:
            system.append(extra_system)
        if not turns:
            return "\n\n".join(system)

        # The current exchange starts at the last user message and runs to
        # the end, so any tool call or tool result issued after it is kept.
        last_user = None
        for index in range(len(turns) - 1, -1, -1):
            if turns[index][0] == "user":
                last_user = index
                break
        if last_user is None:
            last_user = 0

        parts: List[str] = []
        if system:
            parts.append("[系统指令]\n" + "\n".join(system))
        if last_user > 0:
            history = []
            for role, content in turns[:last_user]:
                label = {"user": "用户", "assistant": "助手", "tool": "工具"}.get(role, role)
                history.append(f"{label}：{content}")
            parts.append("[上文]\n" + "\n".join(history))

        current = []
        for role, content in turns[last_user:]:
            if role == "user":
                current.append(content)
            elif role == "assistant":
                current.append("[你上一轮的回答]\n" + content)
            else:
                current.append(content)
        parts.append("\n\n".join(current))
        return "\n\n".join(parts)


def _render_calls(content: str, calls: List[Any]) -> str:
    """Render the assistant's earlier tool calls back into the protocol."""
    blocks = [content] if content else []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = function.get("name") or call.get("name") or ""
        arguments = function.get("arguments") or call.get("arguments") or "{}"
        if not name:
            continue
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {"value": arguments}
        # Same wire format the prompt asks for, so the model sees one
        # consistent convention across the conversation.
        blocks.append("CALL " + name + " "
                      + json.dumps(arguments, ensure_ascii=False))
    return "\n\n".join(blocks)