"""Token accounting — estimated, because the platform reports nothing.

Measured 2026-11-18: the upstream's only closing event is ``ALL_FINISHED``,
and it carries agent metadata alone::

    {"agentNid": null, "agentName": "通用助手", "agentAvatar": "",
     "avatarExt": "", "isAdditional": false, "event": "ALL_FINISHED",
     "text": null, "data": null, "isLast": null, "useTime": null}

There is no ``usage``, no prompt/completion counters, no ``useTime``. The
gateway used to fill ``usage`` with zeros, which is worse than useless: a
client cannot tell "not measured" from "cost nothing".

So this module estimates, and every consumer marks the numbers as estimates
instead of passing them off as measurements.

The ratio: CJK characters run close to one token each for the BPE vocabularies
these models use, while Latin text averages around four characters per token.
That is a rough model, not a tokenizer, and it should never be used to bill
anyone.

Where it is wrong, measured 2026-11-26 with the vendor's own tokenizer (the
`tokenizers` wheel has cp314 builds, so this is exact): a 112-tool block costs
5,791 tokens against 6,438 estimated (+11.2%), the same block with a Chinese
conversation +11.7%, and Chinese prose +60 to +83% — the vocabulary merges
common two-character words, so Chinese really runs ~1.8 characters per token,
not one. The two errors do *not* cancel as an earlier probe suggested: that
probe's own approximation over-counted ASCII by ~40%. The consequence is mild
and always in the same direction — a client that adopts this figure compacts
slightly early. See docs/LIMITS.md 8.6 for the per-sample table and the cost of
counting exactly (+32 MB, +0.7 s startup, 18–82 ms per request).

**These numbers are not decorative.** A client that manages its own context
window stores the reported usage as an anchor and prefers it over its own
heuristic whenever it is larger — ``dsh-token-meter`` does exactly that:
``advanceMeasurement`` keeps ``event.data.usage`` on the anchor, and ``measure``
takes the usage branch when ``usageTokens(usage) >= estimatedAnchorTokens``.
Verified 2026-11-18: three polymas sessions held anchor ``inputTokens`` of
24,875 / 46,674 / 28,976, matching this module's ``prompt_tokens`` for the same
requests digit for digit. So the accuracy here moves the client's compaction
threshold.

Measured on production-shaped prompts (``tools/probe_prompt_accuracy.py``,
vendor BPE as ground truth): +7% on a short session, +4% at 24 turns, +1% at 60,
-0.5% at 120. The ASCII-heavy tool block is overestimated and the Chinese
conversation is underestimated; they cancel as a session grows, which is the
regime where compaction matters.
"""

from __future__ import annotations

import re
from typing import Any, Dict

__all__ = ["estimate_tokens", "usage_for"]

#: CJK ideographs, kana and hangul — the ranges that average ~1 token/char.
_CJK_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\u3040-\u30ff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """Rough token count for one string."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = max(0, len(text) - cjk)
    return int(round(cjk + other / 4.0))


def usage_for(prompt: str, content: str, reasoning: str = "",
              tool_arguments: str = "") -> Dict[str, Any]:
    """An OpenAI ``usage`` block built from the texts we actually sent and got.

    ``reasoning`` counts toward ``completion_tokens``: the platform generates
    it, so it costs the same even though the client never sees it. The extra
    ``estimated`` flag is ours — standard clients ignore unknown keys.

    ``tool_arguments`` counts too, for the same reason: a CALL line is
    generated text. The non-streaming path has always measured before stripping
    it, but the streaming path measured only what reached the client, and
    ``ToolCallStream`` withholds the CALL line — so a file-write turn reported
    none of its arguments. Measured 2026-11-26: one 6,615-character SVG
    argument is 2,619 tokens, an entire turn's completion budget.
    """
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = (estimate_tokens(content) + estimate_tokens(reasoning)
                         + estimate_tokens(tool_arguments))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated": True,
    }
