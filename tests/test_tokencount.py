"""Exact token counting: the vendor vocabularies, and the fallback when absent."""

from __future__ import annotations

import pathlib

import pytest

from polymas2api import tokencount
from polymas2api.tokens import usage_for

ROOT = pathlib.Path(__file__).resolve().parent.parent
VOCAB_DIR = ROOT / "models" / "tokenizers"
HAVE_VOCABS = all((VOCAB_DIR / name).is_file()
                  for name in tokencount.VOCAB_FILES.values())


@pytest.fixture(autouse=True)
def _reset():
    """Every test starts from "counting off" and leaves it that way."""
    tokencount.configure(None, enabled=False)
    try:
        yield
    finally:
        tokencount.configure(None, enabled=False)


def test_model_vendor_follows_the_catalogue_names():
    assert tokencount.model_vendor("deepseek-v4.1-flash") == "deepseek"
    assert tokencount.model_vendor("deepseek-v3") == "deepseek"
    assert tokencount.model_vendor("qwen3.8-max") == "qwen"
    assert tokencount.model_vendor("qwen3.6-plus") == "qwen"
    assert tokencount.model_vendor("Doubao-Seed-2.0-mini") == "doubao"
    assert tokencount.model_vendor("doubao-seed-evolving") == "doubao"
    # Anything unrecognised counts with the default vendor rather than refusing.
    assert tokencount.model_vendor("") == "deepseek"
    assert tokencount.model_vendor("Auto") == "deepseek"


def test_without_a_vocabulary_directory_it_estimates():
    line = tokencount.configure("no-such-directory-anywhere")
    assert "estimating" in line
    assert tokencount.count("你好，世界", "deepseek-v4.1-flash") is None
    usage = usage_for("x" * 400, "hello", model="deepseek-v4.1-flash")
    assert usage["estimated"] is True
    assert usage["prompt_tokens"] == 100        # 400 / 4, the heuristic


def test_counting_can_be_switched_off_entirely():
    line = tokencount.configure(str(VOCAB_DIR), enabled=False)
    assert "estimating" in line
    assert tokencount.count("你好，世界", "deepseek-v4.1-flash") is None


@pytest.mark.skipif(not HAVE_VOCABS, reason="vocabularies not downloaded")
def test_counting_matches_the_vocabulary_and_flags_it_as_measured():
    line = tokencount.configure(str(VOCAB_DIR))
    assert "exact" in line
    zh = "这个网关把平台的对话接口包装成 OpenAI 兼容格式。"
    counted = tokencount.count(zh, "deepseek-v4.1-flash")
    assert isinstance(counted, int) and counted > 0

    usage = usage_for(zh, "回复", model="deepseek-v4.1-flash")
    assert usage["estimated"] is False
    assert usage["prompt_tokens"] == counted


@pytest.mark.skipif(not HAVE_VOCABS, reason="vocabularies not downloaded")
def test_each_vendor_counts_the_same_sentence_differently():
    """If this ever passes with equal numbers the mapping has stopped working."""
    tokencount.configure(str(VOCAB_DIR))
    zh = "昨天下午我们把网关的凭证续期逻辑改了一遍，遇到平台提前作废凭证就只能等下一次定时器。"
    counts = {
        vendor: tokencount.count(zh, model)
        for vendor, model in (("deepseek", "deepseek-v4.1-flash"),
                              ("qwen", "qwen3.8-max"),
                              ("doubao", "Doubao-Seed-2.0-mini"))
    }
    assert all(isinstance(value, int) and value > 0 for value in counts.values())
    assert len(set(counts.values())) > 1, counts


@pytest.mark.skipif(not HAVE_VOCABS, reason="vocabularies not downloaded")
def test_a_model_without_its_own_vocabulary_borrows_the_default():
    directory = VOCAB_DIR
    if not (directory / tokencount.VOCAB_FILES["qwen"]).is_file():
        pytest.skip("qwen vocabulary missing")
    # Configure a directory that holds only one vendor's file.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        only = pathlib.Path(tmp) / tokencount.VOCAB_FILES["deepseek"]
        only.write_bytes((directory / tokencount.VOCAB_FILES["deepseek"]).read_bytes())
        tokencount.configure(tmp)
        borrowed = tokencount.count("你好，世界", "qwen3.8-max")
        assert isinstance(borrowed, int) and borrowed > 0
