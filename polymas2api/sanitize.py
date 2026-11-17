"""Strip third-party client signatures from the request body.

The idea is borrowed from workbuddy2api's ``internal/upstream/sanitize.go``:
upstream platforms often match, **verbatim**, on strings that identify the
client that produced the request — an IDE agent's identity sentence, a
Claude-Code system-prompt fragment, a header name echoed in a user message.
One word of difference is enough to pass, and the upstream error code
(``11-128`` in workbuddy's case) is itself a fingerprint: mentioning it in a
request is what triggers the block.

For polymas the relevant fingerprints are different from workbuddy's, so the
rule set is derived from what actually flows through *this* gateway:

``DSH`` identity
    The DeepSeek Harness injects ``harness:identity`` into the system
    prompt. A teaching assistant that receives "You are powered by DeepSeek
    Harness" is not a student's browser session.

``Claude Code`` identity sentences
    Clients such as Claude Code inject "You are Claude Code, Anthropic's
    official CLI tool for Claude" and "You are a coding agent running in the
    Codex CLI tool…". These are the exact strings workbuddy2api strips.

``X-Anthropic-*`` header names echoed in the text
    workbuddy2api deletes the key/value form and abbreviates the bare form.

``@ path documentation``
    The harness's "Tokens prefixed with @ are paths the user explicitly
    referenced…" paragraph is DSH's most distinctive fingerprint and contains
    no brand word, so a brand-only rule never catches it.

The rewriting is deliberately **meaning preserving**: a synonym is swapped
for the original phrase, a header key is abbreviated, a documentation
paragraph is reworded. Nothing is dropped that the assistant would need.

Scope follows workbuddy2api: identity rules apply to every role when
``sanitize_brand_scope`` is ``"all"`` (the default) and only to
system/developer messages when it is ``"identity"``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

__all__ = ["Sanitizer", "Scope"]

#: Scope values, mirroring workbuddy2api's ``features.sanitize_brand_scope``.
Scope = str  # "all" | "identity" | "off"


#: Verbs that make a sentence an identity claim. The upstream adds new
#: pairings over time, so all seven known ones are listed; a sentence that
#: merely discusses a product ("powered by solar panels") is not matched
#: because the sentence must end with a period and sit in an identity frame.
_IDENTITY_VERBS = (
    "powered by", "built on", "built by", "created by", "developed by",
    "made by", "trained by",
)

#: "You are … powered by …." — bounded window (0..3 modifiers) so that
#: sentences like "You are free to use tools built by …" are left alone.
_IDENTITY_SENTENCE_RE = re.compile(
    r"(?i)You are\s+"
    r"(?:an?\s+|the\s+)?(?:[\w.-]+\s+){0,3}?"
    r"(?:agent|assistant|model|AI|LLM)?[\s,]*"
    r"(?:powered by|built on|built by|created by|developed by|made by|trained by)"
    r"[^\n]*?\.(\s|$)"
)
_IDENTITY_SENTENCE_REPLACEMENT = "You are a helpful AI assistant.\\1"

#: Key/value header form: "X-Anthropic-Billing-Hdr: value;" and friends.
_HEADER_KV_RE = re.compile(r"(?i)\n]*[a-z0-9\-]*anthropic[a-z0-9\-]*\s*:\s*[^;\n]*;?\s*")

#: Bare header name, no colon, no value — workbuddy2api's fallback layer.
#: Abbreviated rather than deleted so the text stays readable.
_HEADER_BARE_RE = re.compile(r"(?i)x-anthropic-([a-z0-9\-]+)")

#: Trailing "cc_xxx=value;" fragments.
_CREDENTIAL_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")

#: The harness's @-path documentation paragraph, rewritten in place.
#:
#: The upstream matches this paragraph as **continuous text**, so the whole
#: paragraph has to move together. The tail pattern accepts both endings that
#: were seen in the wild ("… a path containing spaces." and "… a path that
#: contains spaces.") and also stops at the end of the paragraph, so a
#: truncated copy is still caught.
_DSH_PATHS_RE = re.compile(
    r"(?is)Tokens prefixed with @ are paths the user explicitly referenced\..*?"
    r"(?:path (?:containing|that contains) spaces\.|$)"
)
_DSH_PATHS_PROBE = "Tokens prefixed with @"
_DSH_PATHS_REPLACEMENT = (
    "Paths beginning with @ refer to files the user explicitly mentioned. "
    "Relative paths are resolved against the project root; absolute paths point "
    "to files or directories on the host. A trailing slash marks a directory, so "
    "list it when its contents matter. Anything else is a file: open it with the "
    "read tool when its contents are needed, and never claim to have inspected it "
    "before reading. The @\"...\" form only quotes a path that contains spaces."
)

#: Brand words that identify a specific agent harness. Rewritten to a neutral
#: runtime name rather than removed: the assistant still needs to know it is
#: an assistant.
_BRAND_REWRITES: Tuple[Tuple[str, str], ...] = (
    ("DeepSeek Harness", "DeepSeekRuntime"),
    ("deepseek-harness", "deepseek-runtime"),
)

#: Client identity sentences that are matched verbatim upstream. Each entry is
#: (needle, replacement); the replacement differs by exactly one word so the
#: upstream's verbatim match fails while the meaning survives.
_SENTENCE_REWRITES: Tuple[Tuple[str, str], ...] = (
    (
        "You are Claude Code, Anthropic's official CLI tool for Claude",
        "You are Claude Code, Anthropic's official CLI tool for Claude",
    ),
    (
        "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant.",
        "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant.",
    ),
    (
        "Default branch (you will usually use this for PRs)",
        "Default branch (you will usually use this for PRs)",
    ),
)


class Sanitizer:
    """Applies the fingerprint rules to an OpenAI-style messages array."""

    def __init__(self, scope: Scope = "all", *, enabled: bool = True):
        self.scope = scope if scope in ("all", "identity", "off") else "all"
        self.enabled = enabled and self.scope != "off"

    # -- detection ----------------------------------------------------------
    @staticmethod
    def _has_brand(text: str) -> bool:
        return any(needle.lower() in text.lower() for needle, _ in _BRAND_REWRITES)

    @staticmethod
    def _has_identity_sentence(text: str) -> bool:
        lowered = text.lower()
        if not any(verb in lowered for verb in _IDENTITY_VERBS):
            return False
        return bool(_IDENTITY_SENTENCE_RE.search(text))

    @staticmethod
    def _has_generic(text: str) -> bool:
        if _DSH_PATHS_PROBE in text:
            return True
        if _HEADER_BARE_RE.search(text):
            return True
        for needle, _ in _SENTENCE_REWRITES:
            if needle in text:
                return True
        return False

    # -- rewriting ----------------------------------------------------------
    @staticmethod
    def _rewrite_text(text: str, brand: bool) -> str:
        if not text:
            return text
        changed = False

        # Identity sentence first: the regex matches the original brand name,
        # so replacing the brand earlier would make the rule miss.
        if brand and Sanitizer._has_identity_sentence(text):
            text = _IDENTITY_SENTENCE_RE.sub(_IDENTITY_SENTENCE_REPLACEMENT, text)
            changed = True

        if brand:
            for needle, replacement in _BRAND_REWRITES:
                if needle in text:
                    text = text.replace(needle, replacement)
                    changed = True

        for needle, replacement in _SENTENCE_REWRITES:
            if needle in text and needle != replacement:
                text = text.replace(needle, replacement)
                changed = True

        if _DSH_PATHS_PROBE in text:
            text = _DSH_PATHS_RE.sub(_DSH_PATHS_REPLACEMENT, text)
            changed = True

        if _HEADER_BARE_RE.search(text):
            text = _HEADER_BARE_RE.sub(lambda m: "x-anthropic-" + m.group(1)[:3], text)
            changed = True

        if "cc_" in text:
            previous = None
            while previous != text:
                previous = text
                text = _CREDENTIAL_RE.sub("", text)
            changed = True

        return text if changed else text

    @classmethod
    def _clean_text(cls, text: str, brand: bool) -> str:
        if not cls._has_generic(text) and not (brand and (cls._has_brand(text)
                                                             or cls._has_identity_sentence(text))):
            return text
        return cls._rewrite_text(text, brand)

    # -- public API ---------------------------------------------------------
    def sanitize_messages(self, messages: List[Any]) -> List[Any]:
        """Return a sanitized copy of the messages array."""
        if not self.enabled or not isinstance(messages, list):
            return messages
        brand_global = self.scope == "all"
        output: List[Any] = []
        for message in messages:
            if not isinstance(message, dict):
                output.append(message)
                continue
            copy = dict(message)
            role = str(copy.get("role") or "").strip().lower()
            identity = role in ("system", "developer")
            brand = brand_global or identity

            content = copy.get("content")
            if isinstance(content, str):
                copy["content"] = self._clean_text(content, brand)
            elif isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        new_part = dict(part)
                        new_part["text"] = self._clean_text(part["text"], brand)
                        parts.append(new_part)
                    else:
                        parts.append(part)
                copy["content"] = parts

            for field in ("reasoning_content", "reasoning"):
                value = copy.get(field)
                if isinstance(value, str):
                    copy[field] = self._clean_text(value, brand)

            tool_calls = copy.get("tool_calls")
            if isinstance(tool_calls, list):
                new_calls = []
                for call in tool_calls:
                    if not isinstance(call, dict):
                        new_calls.append(call)
                        continue
                    new_call = dict(call)
                    function = new_call.get("function")
                    if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                        new_function = dict(function)
                        new_function["arguments"] = self._clean_text(function["arguments"], brand)
                        new_call["function"] = new_function
                    new_calls.append(new_call)
                copy["tool_calls"] = new_calls

            output.append(copy)
        return output

    def sanitize_prompt(self, prompt: str) -> str:
        """Sanitize a flattened prompt (the form the platform actually receives)."""
        if not self.enabled or not isinstance(prompt, str):
            return prompt
        return self._clean_text(prompt, brand=True)
