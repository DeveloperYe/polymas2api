"""Probe output-side limits on deepseek-v4.1-flash.

Two questions, both asked the way a client actually asks them:

1. Does ``max_tokens`` do anything end-to-end? The gateway's upstream body has
   no such field, so the expectation is "ignored" — measure it, and measure it
   *through the gateway* because that is where a client sets it.
2. Is there an output ceiling? Ask for a long enumeration and see where the
   answer stops on its own.

    python tools/probe_maxtokens.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymas2api.config import load_config          # noqa: E402
from polymas2api.tokens import estimate_tokens      # noqa: E402

MODEL = "deepseek-v4.1-flash"
GW = "http://127.0.0.1:8787/v1/chat/completions"
ESSAY = "写一段约 800 字的散文，主题是春天，不要分条，不要标题。"


def key() -> str:
    return load_config(str(ROOT / "config.json")).api_key


def call(prompt: str, max_tokens=None) -> dict:
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    request = urllib.request.Request(
        GW, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key()}"}, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return {"seconds": round(time.time() - started, 1), "error":
                f"HTTP {error.code}: {error.read().decode('utf-8')[:160]}",
                "answer": "", "usage": {}}
    except Exception as error:  # noqa: BLE001
        return {"seconds": round(time.time() - started, 1),
                "error": f"{type(error).__name__}: {str(error)[:160]}",
                "answer": "", "usage": {}}
    message = (data.get("choices") or [{}])[0].get("message") or {}
    return {"seconds": round(time.time() - started, 1), "error": "",
            "answer": message.get("content") or "", "usage": data.get("usage") or {},
            "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason")}


def show(label: str, out: dict) -> None:
    if out["error"]:
        print(f"  {label:<24} {out['seconds']:>6}s  失败: {out['error']}")
        return
    usage = out.get("usage") or {}
    print(f"  {label:<24} {out['seconds']:>6}s  正文 {len(out['answer']):>5} 字"
          f"（≈{estimate_tokens(out['answer']):>5} tok）"
          f"  finish={out.get('finish_reason')}  usage={usage.get('completion_tokens')}")


def main() -> int:
    print(f"模型 {MODEL}，全部请求走网关 HTTP 接口（客户端视角）\n")

    print("=== 1. max_tokens 端到端是否有效 ===")
    for label, value in (("不传 max_tokens", None), ("max_tokens=1", 1),
                         ("max_tokens=50", 50), ("max_tokens=65536", 65536)):
        show(label, call(ESSAY, value))
    print()

    print("=== 2. 输出上限：让它连续数到 3000 ===")
    out = call("从 1 开始，每个数字单独一行，一直数到 3000。不要省略、不要解释、不要用省略号。")
    if out["error"]:
        print(f"  失败: {out['error']}")
        return 0
    lines = [line.strip().rstrip(".") for line in out["answer"].splitlines() if line.strip()]
    numbers = [int(line) for line in lines if line.isdigit()]
    print(f"  耗时 {out['seconds']}s，正文 {len(out['answer'])} 字"
          f"（≈{estimate_tokens(out['answer'])} tok），{len(lines)} 行，"
          f"usage={ (out.get('usage') or {}).get('completion_tokens') }")
    if numbers:
        contiguous = numbers == list(range(numbers[0], numbers[0] + len(numbers)))
        print(f"  数字 {min(numbers)} – {max(numbers)}，连续={contiguous}，"
              f"到 3000={3000 in numbers}")
    print(f"  结尾: …{out['answer'][-90:].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
