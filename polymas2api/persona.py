"""Remove the caller's own name from a reply.

The assistant does not *remember* the name. The platform looks the account up
(``getLoginUserInfo`` → ``userName``) and hands it to the agent, which then
opens with "林晓东，来了。". That value is not the client's to change, and it is
the account holder's real name, so it is removed on the way out.

The right fix is an account whose ``userName`` is not a real name — the
production account now uses one ("用户") and this module is therefore idle
with an empty list. It stays as a capability: fill in ``persona_scrub_names``
and the same rules apply again without touching code.

**Persona washing was removed on 2026-11-17.** It rewrote the campus-assistant
self-reference ("我是你的电子工艺实习小管家" → "我是 AI 助手"). Two reasons:

* switching accounts made it useless — the new agent says "通用助手" and every
  rule missed (5/5 self-descriptions untouched);
* it could destroy content. The role rule allowed an arbitrary 14-character gap
  in front of "小管家", so "你可以问问班级小管家。" was rewritten to
  "AI 助手。" — the whole sentence gone.

Design notes that still apply to the name rules:

* **Cheap pre-filter.** A literal ``in`` test runs first; only when it hits
  does the regex engine touch the string.
* **Whole units first.** The address forms ("林晓东同学", "林老师") are removed
  as units, otherwise they degrade into "同学" or "你老师".
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

__all__ = ["NameScrubber", "StreamWasher", "name_list"]


def _name_rules(names: List[str]) -> List[Tuple[str, str, str]]:
    """Rules that remove the *caller's own name* from a reply.

    Leaking is the worse failure, so the name is replaced wherever it appears.
    Two things need care:

    * the address forms are handled **as whole units** first — otherwise
      "林晓东同学" degrades into "同学" and "林老师" into "你老师";
    * every rule uses the **surname** as its probe. The probe is a cheap
      literal pre-filter, and the full name is not a substring of "林老师"
      or "林晓东同学", so a per-rule probe silently disabled the rule
      (measured 2026-11-16).
    """
    rules: List[Tuple[str, str, str]] = []
    for name in names:
        name = (name or "").strip()
        if len(name) < 2:
            continue  # a single character would match far too much
        escaped = re.escape(name)
        surname = name[0]
        probe = surname  # cheap, and always present when the name could be
        polite = (f"(?:{re.escape(name)}|{re.escape(surname)})"
                  r"(?:老师|同学|先生|女士)")
        # An address that opens the reply or a sentence goes with its
        # punctuation; leaving the punctuation behind yields "，我看一下。".
        rules.append((r"(?:^|(?<=[。！？!?\n\s]))" + polite
                      + r"[，,、！!。：:\s]*", "", probe))
        # The name glued to a pronoun: the model writes
        # "我是林晓东你的…" for "我是林晓东，你的…".
        rules.append((escaped + r"(?=你)", "", probe))
        # The full name opening the reply or a sentence.
        rules.append((r"(?:^|(?<=[。！？!?\n\s]))" + escaped
                      + r"[，,、！!。：:\s]*", "", probe))
        # The name closing a clause: "知道，林晓东。这名字我记着".
        rules.append((r"[，,、]\s*" + escaped + r"\s*([。！!?？])", r"\1", probe))
        # Everything left is still a form of address.
        rules.append((polite, "你", probe))
        rules.append((escaped, "你", probe))
    return rules


def _tidy_punctuation(text: str) -> str:
    """Clean up what removing an address leaves behind.

    ``知道，林晓东。`` becomes ``知道。`` and a leading comma is dropped.
    """
    result = text
    result = re.sub(r"[，,、]\s*([。！!?？])", r"\1", result)
    result = re.sub(r"[，,、]\s*[，,、]", "，", result)
    result = re.sub(r"^[，,、。\s]+", "", result)
    return result


def name_list(raw: Any) -> List[str]:
    """Parse the ``persona_scrub_names`` setting.

    Accepts a comma separated string (what the panel sends) or a sequence.
    """
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = re.split(r"[,，;；\s]+", str(raw))
    seen: List[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


class NameScrubber:
    """Rewrites the caller's own name out of an assistant reply."""

    def __init__(self, names: Any = None):
        self.names = name_list(names)
        self._rule_sources = _name_rules(self.names)
        self._compiled = [
            (re.compile(pattern), replacement, probe)
            for pattern, replacement, probe in self._rule_sources
        ]
        # Every literal a streamed cut must not land inside: the names
        # themselves, the bare surname, and the polite address forms.
        self.name_tokens: List[str] = []
        for name in self.names:
            self.name_tokens.append(name)
            self.name_tokens.append(name[0])
            for suffix in ("老师", "同学", "先生", "女士"):
                self.name_tokens.append(name[0] + suffix)

    @property
    def enabled(self) -> bool:
        """Nothing to do unless a name was configured."""
        return bool(self.names)

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _has_probe(text: str, probe: str) -> bool:
        """The cheap pre-filter: a literal test before any regex work."""
        return bool(probe) and probe in text

    # -- public API ---------------------------------------------------------
    def wash_text(self, text: str) -> str:
        """Return the text with any configured name removed."""
        if not text or not self._compiled:
            return text
        result = text
        for pattern, replacement, probe in self._compiled:
            if not self._has_probe(result, probe):
                continue
            result = pattern.sub(replacement, result)
        return _tidy_punctuation(result)

    def wash_reasoning(self, text: str) -> str:
        """Return the reasoning trace with any configured name removed.

        The trace is the assistant talking to itself ("用户是林晓东，应该称呼
        他…"), so a bare name there carries no reference risk worth protecting:
        after the address rules, the rest is replaced outright. The visible
        answer keeps the conservative address-only rules.
        """
        if not text or not self._compiled:
            return text
        result = text
        for pattern, replacement, probe in self._compiled:
            if not self._has_probe(result, probe):
                continue
            result = pattern.sub(replacement, result)
        for name in self.names:
            if name in result:
                result = result.replace(name, "你")
        return _tidy_punctuation(result)

    def wash(self, text: str, *, reasoning: bool = False) -> str:
        """Wash either the answer or the reasoning trace."""
        return self.wash_reasoning(text) if reasoning else self.wash_text(text)

    def describe(self) -> Dict[str, Any]:
        """A small dict for the panel."""
        return {"enabled": self.enabled, "names": list(self.names),
                "rules": len(self._compiled)}


class StreamWasher:
    """Washes the name out of a *streamed* reply.

    The upstream splits the answer at arbitrary boundaries, so washing each
    frame on its own misses a name that straddles two of them ("林晓" then
    "林"). A tail is held back until it can no longer start a match.

    A tail is **not** enough for the opening address, though. The rule that
    removes "林晓东，来了。…" is anchored at the start of the reply, and with
    only a tail held back the very first release hands over a fragment like
    "林晓" — the rest of the name arrives after it has already been emitted
    (measured 2026-11-16). So the opening is held until there is enough of
    it to wash in one piece, and the cut point is moved back whenever it
    would land inside a name or an address form.
    """

    #: How many characters to hold back.
    TAIL = 48

    #: How much must be released past the tail in one go, so that a name at
    #: the start of the reply is still whole when the prefix is washed.
    HEAD_MIN = 16

    #: ``"text"`` washes the answer, ``"reasoning"`` the thinking trace.
    def __init__(self, washer: NameScrubber, channel: str = "text"):
        self.washer = washer
        self.channel = channel
        self._pending = ""

    def _wash(self, text: str) -> str:
        if self.channel == "reasoning":
            return self.washer.wash_reasoning(text)
        return self.washer.wash_text(text)

    def feed(self, text: str) -> str:
        """Take a fragment and return the part that is safe to emit."""
        if not self.washer.enabled:
            return text
        buffer = self._pending + text
        # The opening stays buffered until the part before the tail is long
        # enough to contain a whole address.
        if len(buffer) < self.TAIL + self.HEAD_MIN:
            self._pending = buffer
            return ""
        cut = len(buffer) - self.TAIL
        # Never cut a name in half: the emitted head would end with "林晓" and
        # the rest of the name would arrive after it had already gone out.
        cut = self._avoid_name_prefix(buffer, cut)
        head = buffer[:cut]
        self._pending = buffer[cut:]
        return self._wash(head)

    def _avoid_name_prefix(self, buffer: str, cut: int) -> int:
        """Move the cut back so it does not land inside a name or address.

        Keeping only the names in mind is not enough: "林老师" is built from
        the bare surname, so the cut also has to avoid leaving a lone "林"
        at the end of the emitted part.
        """
        guard = sorted(getattr(self.washer, "name_tokens", []) or [],
                       key=len, reverse=True)
        for token in guard:
            for size in range(len(token) - 1, 0, -1):
                if cut - size < 0:
                    continue
                if buffer[cut - size:cut] == token[:size]:
                    cut -= size
                    break
        return max(1, cut)

    def flush(self) -> str:
        """Release the held-back tail at the end of the stream."""
        pending = self._pending
        self._pending = ""
        return self._wash(pending) if pending else ""
