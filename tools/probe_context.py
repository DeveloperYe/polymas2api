"""Probe the platform's context-window limit on deepseek-v4.1-flash.

Bypasses the gateway's own 1 MB body cap by talking to the platform directly
(faster to reason about: any limit found here is the platform's, not ours).

A needle sits at the very start of the prompt. If the answer still names it,
the whole prompt was visible; if the model says it cannot see it, the platform
truncated the input somewhere.

    python tools/probe_context.py [sizes...]
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymas2api.config import load_config          # noqa: E402
from polymas2api.pool import Credential             # noqa: E402
from polymas2api.upstream import PolymasClient      # noqa: E402

MODEL = "deepseek-v4.1-flash"
NEEDLE = "NEEDLE-7F3A9"


def filler(chars: int) -> str:
    """Varied, low-cost Chinese filler so nothing can be deduplicated away."""
    parts = []
    total = 0
    line = 0
    while total < chars:
        line += 1
        piece = f"第{line}行：此段仅用于占位填充，内容无意义，编号{line * 7919 % 100003}。"
        parts.append(piece)
        total += len(piece)
    return "".join(parts)[:chars]


def prompt_for(chars: int) -> str:
    tail = ("问题：本消息最开头写的那个「编号」是什么？"
            "只回答编号本身，不要解释。如果你看不到编号，就只回答：看不到。")
    return f"编号：{NEEDLE}。\n" + filler(chars) + "\n" + tail


def account():
    raw = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))["accounts"][0]
    return Credential(raw["uid"], user_nid=raw["user_nid"],
                      friend_nid=raw["friend_nid"],
                      app_code=raw.get("app_code") or raw["friend_nid"],
                      ai_poly=raw["ai_poly"])


def run(chars: int, timeout: float = 300.0) -> dict:
    cfg = load_config(str(ROOT / "config.json"))
    cfg.retain_long_term_memory = False
    client = PolymasClient(cfg, account(), logger=lambda _m: None)
    prompt = prompt_for(chars)
    started = time.time()
    reasoning = answer = error = ""
    try:
        for event, payload in client.chat(prompt, model=MODEL,
                                          session_id=f"ctx-{uuid.uuid4().hex[:10]}"):
            if event == "REASONING":
                reasoning += payload.get("text") or ""
            elif event == "ANSWER":
                answer += payload.get("text") or ""
            elif event == "ERROR":
                error = str(payload)[:200]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    return {
        "chars": chars,
        "seconds": round(time.time() - started, 1),
        "error": error,
        "answer": answer.strip()[:120],
        "reasoning_chars": len(reasoning),
        "saw_needle": NEEDLE.lower() in (answer + reasoning).lower(),
    }


def main(argv: list[str]) -> int:
    sizes = [int(x) for x in argv[1:]] or [2000, 16000, 32000, 64000, 128000, 256000]
    print(f"模型 {MODEL}，输入按字符数递增；中文约 1 token/字\n")
    print(f"{'输入字符':>10} {'≈token':>9} {'耗时':>7}  {'结果':<6} {'看到针':<6} 回答/错误")
    print("-" * 100)
    for size in sizes:
        result = run(size)
        status = "失败" if result["error"] else "成功"
        seen = "是" if result["saw_needle"] else "否"
        detail = result["error"] or result["answer"].replace("\n", " ")[:46]
        print(f"{size:>10} {size:>9} {result['seconds']:>6}s  {status:<6} {seen:<6} {detail}")
        sys.stdout.flush()
        if result["error"]:
            print(f"\n  在 {size} 字符处失败，停止继续放大。")
            break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
