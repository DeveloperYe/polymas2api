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
That is a rough model, not a tokenizer — it is good for "roughly how big is
this prompt" and for spotting a sudden doubling, and it should never be used
to bill anyone.
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


def usage_for(prompt: str, content: str, reasoning: str = "") -> Dict[str, Any]:
    """An OpenAI ``usage`` block built from the texts we actually sent and got.

    ``reasoning`` counts toward ``completion_tokens``: the platform generates
    it, so it costs the same even though the client never sees it. The extra
    ``estimated`` flag is ours — standard clients ignore unknown keys.
    """
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(content) + estimate_tokens(reasoning)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated": True,
    }
