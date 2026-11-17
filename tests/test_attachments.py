"""The caller's images stay on this machine."""

import base64
import os

import pytest

from polymas2api import attachments


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cf0000000301000130c1eb5e0000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYMAS_ATTACHMENT_DIR", str(tmp_path))
    attachments._last_sweep = 0.0
    return tmp_path


def _data_url(data: bytes = PNG) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode()


def test_an_image_is_written_locally_and_named_in_the_prompt():
    text = attachments.part_to_text({"type": "image_url",
                                     "image_url": {"url": _data_url()}})
    assert "本地路径：" in text
    assert "未上传" in text
    path = text.split("本地路径：", 1)[1].splitlines()[0]
    assert os.path.isfile(path)
    with open(path, "rb") as handle:
        assert handle.read() == PNG


def test_the_same_image_maps_to_one_file():
    first = attachments.store_data_url(_data_url())
    second = attachments.store_data_url(_data_url())
    assert first == second
    assert len(list(first.parent.parent.rglob("*.png"))) == 1


def test_a_disabled_switch_says_the_image_cannot_be_seen():
    text = attachments.part_to_text({"type": "image_url",
                                     "image_url": {"url": _data_url()}}, False)
    assert "未开启" in text
    assert "本地路径" not in text


def test_text_parts_pass_through_untouched():
    assert attachments.part_to_text({"type": "text", "text": "你好"}) == "你好"


def test_an_http_image_url_is_handed_over_as_a_link():
    text = attachments.part_to_text({"type": "image_url",
                                     "image_url": {"url": "https://x/y.png"}})
    assert "https://x/y.png" in text


def test_the_openai_string_shape_is_accepted():
    text = attachments.part_to_text({"type": "image_url",
                                     "image_url": _data_url()})
    assert "本地路径：" in text


def test_a_broken_base64_part_does_not_raise():
    text = attachments.part_to_text({"type": "image_url", "image_url": "data:image/png;base64,!!!!"})
    assert text


def test_sweep_removes_old_files_only():
    fresh = attachments.store_bytes(PNG)
    stale = attachments.store_bytes(b"older bytes")
    os.utime(stale, (0, 0))
    removed = attachments.sweep(keep_hours=1)
    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_the_prompt_says_plainly_that_image_understanding_is_unavailable():
    """The model must not pretend to see the picture.

    Measured 2026-11-17: with steering to "compute with a tool" the model wrote
    elaborate scripts for content it could never recover; with a plain refusal
    it answers honestly in ~6s. Being explicit is the feature.
    """
    text = attachments.part_to_text({"type": "image_url",
                                     "image_url": {"url": _data_url()}})
    assert "本网关不支持图片理解" in text
    assert "不要猜测" in text


def test_the_path_is_still_offered_for_callers_with_their_own_tools():
    text = attachments.part_to_text({"type": "image_url",
                                     "image_url": {"url": _data_url()}})
    path = text.split("本地路径：", 1)[1].splitlines()[0]
    assert os.path.isfile(path)
