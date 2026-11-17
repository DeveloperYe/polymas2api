"""Keep the caller's images on *this* machine instead of shipping them upstream.

The platform's chat endpoint takes one plain-text ``userInput``, so an OpenAI
``image_url`` part cannot travel on the wire at all. Dropping it silently was
the old behaviour: the model answered "没有收到图片附件" and the user had no
idea why.

The agent that answers the request has its own tools — and those tools run on
the caller's machine, the same machine this gateway runs on. So the image does
not have to go anywhere: write it to a local file and tell the model the path,
exactly like a file the user mentioned by name. Verified 2026-11-17 — given a
local path, the assistant emits a real ``read`` / shell call instead of
pretending.

Files are content-addressed (SHA-256 of the bytes), so sending the same image
twice costs one file and nothing is ever overwritten.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: ``data:image/png;base64,AAAA`` — the shape both DSH and OpenAI clients send.
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]+)?(?:;[^,]*)?;base64,(?P<data>.*)$",
                          re.DOTALL | re.IGNORECASE)

#: How long a stored image is kept. The agent may read it several turns later,
#: so this is measured in hours rather than "until the request ends".
DEFAULT_KEEP_HOURS = 24.0

#: Timestamp of the last housekeeping sweep, so it runs at most hourly.
_last_sweep = 0.0

_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
}


def attachment_dir() -> Path:
    """Where stored images live. Overridable for tests and for a custom volume."""
    configured = os.environ.get("POLYMAS_ATTACHMENT_DIR", "")
    if configured:
        return Path(configured)
    # ``resolve()`` turns the 8.3 short form (``ADMINI~1``) into the real path,
    # which is what a human or a shell tool expects to see.
    return (Path(tempfile.gettempdir()) / "polymas2api-attachments").resolve()


def _extension(media_type: str, default: str = "png") -> str:
    return _EXTENSIONS.get((media_type or "").split(";")[0].strip().lower(), default)


def split_data_url(value: str) -> Tuple[str, str]:
    """Return ``(media_type, base64_payload)``; ``("", "")`` when not a data URL."""
    match = _DATA_URL_RE.match((value or "").strip())
    if not match:
        return "", ""
    return match.group("mime") or "", match.group("data") or ""


def _sweep_occasionally() -> None:
    """Cheap housekeeping: at most one sweep per hour per process."""
    global _last_sweep
    now = time.time()
    if now - _last_sweep < 3600:
        return
    _last_sweep = now
    try:
        sweep()
    except Exception:  # noqa: BLE001 - cleanup must never fail a turn
        pass


def store_bytes(data: bytes, media_type: str = "image/png") -> Path:
    """Write one image under its content hash and return the path.

    Idempotent: the same bytes always map to the same file, so a client that
    replays a conversation does not accumulate copies.
    """
    _sweep_occasionally()
    root = attachment_dir()
    digest = hashlib.sha256(data).hexdigest()
    target = root / digest[:2] / f"{digest}.{_extension(media_type)}"
    if target.exists() and target.stat().st_size == len(data):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


def store_data_url(value: str) -> Optional[Path]:
    """Decode an OpenAI ``data:`` URL and store it; ``None`` if it is not one."""
    media_type, payload = split_data_url(value)
    if not payload:
        return None
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception:  # noqa: BLE001 - a malformed part must not fail the turn
        return None
    if not data:
        return None
    return store_bytes(data, media_type or "image/png")


def sweep(keep_hours: float = DEFAULT_KEEP_HOURS) -> int:
    """Delete stored images older than ``keep_hours``. Returns how many went."""
    root = attachment_dir()
    if not root.exists():
        return 0
    cutoff = time.time() - keep_hours * 3600.0
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def describe_image(path: Path, label: str = "图片") -> str:
    """What the model is told about an attached image.

    This gateway does **not** provide image understanding, and the prompt says
    so plainly instead of nudging the model into workarounds. The reasoning,
    measured 2026-11-17:

    * The platform feeds the model **text extracted from an image**, never the
      image itself (its own agent states "I can only see the text content
      extracted, not full layout"). A screenshot is fine; a photograph yields
      nothing.
    * Our upstream takes one plain-text ``userInput`` and exposes no image
      field at all — eight placements were tried and ignored.
    * The official image path is Tencent IM, which the web client talks over a
      websocket; its REST API rejects a normal user's signature (error 60010)
      and the browser SDK cannot run headless without reimplementing its
      storage layer.
    * No OCR is available on this machine — Windows Server lacks the WinRT OCR
      types, and ``onnxruntime`` has no wheel for Python 3.14.

    So the honest position is: the model cannot see the picture. The path is
    still given, because a caller that *does* have its own tools may act on it.
    """
    return (
        f"[用户附加了一张{label}（存放在本机，未上传到任何服务器）]\n"
        f"本地路径：{path}\n"
        "重要：**本网关不支持图片理解** —— 你看不到图片内容，也无法通过读取"
        "文件字节还原画面。\n"
        "请不要猜测图片里有什么，也不要假装看过。\n"
        "如实告知用户你看不到；如果用户需要，请他直接描述内容，"
        "或让他用自己的工具处理这个路径。"
    )


def part_to_text(part: Dict[str, Any], enabled: bool = True) -> str:
    """Turn one OpenAI content part into prompt text.

    Text passes through untouched. An image becomes a local path plus an
    instruction; when the feature is off it becomes a short note so the model
    can at least tell the user why the picture is missing.
    """
    kind = str(part.get("type") or "")

    if kind in ("text", "input_text"):
        return str(part.get("text") or "")

    if kind in ("image_url", "input_image", "image"):
        source = part.get("image_url") or part.get("image") or part.get("url")
        url = ""
        if isinstance(source, dict):
            url = str(source.get("url") or "")
        elif isinstance(source, str):
            url = source
        if not url:
            url = str(part.get("data") or "")
        media_type = str(part.get("media_type") or part.get("mediaType") or "")

        if not enabled:
            return "[用户附加了一张图片，但本网关未开启图片转发，模型看不到它]"

        if url.startswith("data:"):
            path = store_data_url(url)
            if path is not None:
                return describe_image(path)
            return "[用户附加了一张图片，但解码失败，内容不可用]"

        if isinstance(part.get("data"), str) and part["data"] and not url.startswith("http"):
            # Some clients send bare base64 in ``data`` with a separate mime type.
            try:
                payload = base64.b64decode(part["data"], validate=False)
            except Exception:  # noqa: BLE001
                payload = b""
            if payload:
                return describe_image(store_bytes(payload, media_type or "image/png"))

        if url.startswith(("http://", "https://")):
            # A URL is not a path: ``Path()`` would rewrite the separators.
            return (
                "[用户附加的图片（链接，未上传到本机）]\n"
                f"链接：{url}\n"
                "如果需要查看其内容，请用本机工具下载或打开这个链接。"
            )
        return f"[用户附加了一张图片（{url[:60]}）]"

    if kind == "file":
        return "[用户附加了一个文件，但本网关拿不到文件内容]"

    return ""
