"""Exact token counting, using the vendors' own vocabularies.

The platform reports no usage at all, so every number this project sends a
client is produced locally (see :mod:`polymas2api.tokens`). Until now that
number came from a character heuristic, which is wrong in both directions:
measured 2026-11-26 against the real vocabularies, a 112-tool block is
over-estimated by 11% and Chinese prose by 60-83%, while code and English are
under-estimated by up to 20%.

This module counts for real. ``tokenizers`` ships cp314 wheels, so the vendor's
own BPE can be loaded directly; the vocabularies themselves are data files kept
outside the repository (they are 7-30 MB in total) and fetched by
``tools/fetch_tokenizers.py``. Everything here degrades to ``None`` — meaning
"count it yourself" — rather than raising, because an optional dependency must
never be able to break a request.

Why one vocabulary is not enough: the three vendors tokenize the same Chinese
text differently (measured 2026-11-26, DeepSeek 128,815 entries against Qwen
151,669 and Doubao 155,121 — up to 27% apart on a short Chinese sentence).
A model therefore counts with its own vendor's vocabulary when that file is
present, and with DeepSeek's otherwise.

    >>> from polymas2api import tokencount
    >>> tokencount.configure("models/tokenizers")
    >>> tokencount.count("你好，世界", "deepseek-v4.1-flash")
    6
"""

from __future__ import annotations

import pathlib
import threading
from typing import Dict, Optional, Set

__all__ = ["VOCAB_FILES", "configure", "count", "model_vendor", "status"]

#: File name per vendor, resolved inside the configured directory.
VOCAB_FILES = {
    "deepseek": "deepseek.json",
    "qwen": "qwen.json",
    "doubao": "doubao.json",
}

#: Used when a model's own vendor has no vocabulary on disk. DeepSeek's is the
#: smallest of the three, so it is the one that under-counts least often.
FALLBACK = "deepseek"

_LOCK = threading.Lock()
_DIRECTORY: Optional[pathlib.Path] = None
_ENABLED = True
_LOADED: Dict[str, object] = {}
_FAILED: Set[str] = set()
_ERROR = ""


def model_vendor(model: str) -> str:
    """Which vendor's vocabulary counts this model.

    Matched on the name because the catalogue gives nothing better — the
    platform's own ``modelCode`` is the only identifier it publishes.
    """
    name = (model or "").lower()
    if "qwen" in name:
        return "qwen"
    if "doubao" in name or "seed" in name:
        return "doubao"
    return "deepseek"


def configure(directory: object = None, *, enabled: bool = True) -> str:
    """Point the counter at a vocabulary directory and report what it found.

    Call once at start-up. Returns a one-line status for the log: the caller
    should print it, because "counting exactly" versus "estimating" changes
    every ``prompt_tokens`` the gateway reports and there is no other signal.
    """
    global _DIRECTORY, _ENABLED, _ERROR
    with _LOCK:
        _ENABLED = bool(enabled)
        _ERROR = ""
        _LOADED.clear()
        _FAILED.clear()
        _DIRECTORY = pathlib.Path(directory) if directory else None
        if not _ENABLED:
            return "token counting: estimating (disabled in the config)"
        try:
            import tokenizers  # noqa: F401
        except ImportError:
            _ERROR = "the tokenizers package is not installed"
            return f"token counting: estimating ({_ERROR})"
        if _DIRECTORY is None or not _DIRECTORY.is_dir():
            _ERROR = f"no vocabulary directory at {_DIRECTORY}"
            return f"token counting: estimating ({_ERROR})"
        found = sorted(vendor for vendor, name in VOCAB_FILES.items()
                       if (_DIRECTORY / name).is_file())
        if not found:
            _ERROR = f"no vocabulary files in {_DIRECTORY}"
            return f"token counting: estimating ({_ERROR})"
        return ("token counting: exact for " + ", ".join(found)
                + (" (others fall back to " + FALLBACK + ")" if len(found) < 3 else ""))


def status() -> str:
    """The line :func:`configure` returned, without touching the disk again."""
    if not _ENABLED:
        return "estimating (disabled)"
    if _ERROR:
        return f"estimating ({_ERROR})"
    with _LOCK:
        loaded = sorted(_LOADED)
    return "exact for " + ", ".join(loaded) if loaded else "estimating (nothing loaded yet)"


def _tokenizer(vendor: str):
    """The loaded tokenizer for one vendor, or ``None`` when unavailable."""
    with _LOCK:
        if vendor in _LOADED:
            return _LOADED[vendor]
        if vendor in _FAILED or _DIRECTORY is None:
            return None
        name = VOCAB_FILES.get(vendor)
        path = _DIRECTORY / name if name else None
        if path is None or not path.is_file():
            _FAILED.add(vendor)
            return None
        try:
            from tokenizers import Tokenizer
            tokenizer = Tokenizer.from_file(str(path))
        except Exception:                          # noqa: BLE001 - optional
            _FAILED.add(vendor)
            return None
        _LOADED[vendor] = tokenizer
        return tokenizer


def count(text: str, model: str = "") -> Optional[int]:
    """Exact token count, or ``None`` when no vocabulary can do it.

    ``None`` is the contract the caller must honour: it means "fall back to the
    estimate", never "zero tokens".
    """
    if not text or not _ENABLED or _ERROR:
        return None
    vendor = model_vendor(model)
    tokenizer = _tokenizer(vendor)
    if tokenizer is None and vendor != FALLBACK:
        tokenizer = _tokenizer(FALLBACK)
    if tokenizer is None:
        return None
    try:
        return len(tokenizer.encode(text).ids)
    except Exception:                              # noqa: BLE001 - optional
        return None
