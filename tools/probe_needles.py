"""Three-needle context probe: does the platform keep the middle?

A start-only needle proves nothing about the middle: a platform that keeps the
first N and last N tokens (a "sandwich") would still answer it. This puts one
random token at the start, one in the middle and one at the end, then asks for
all three. Whatever comes back missing is what got dropped.

    python tools/probe_needles.py [sizes...]
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
START, MIDDLE, END = "QSTART-4C71Z", "QMIDDLE-8K23X", "QEND-2P95V"


def filler(chars: int, seed: int = 0) -> str:
    parts, total, line = [], 0, seed
    while total < chars:
        line += 1
        piece = f"第{line}行：此段仅用于占位填充，内容无意义，编号{line * 7919 % 100003}。"
        parts.append(piece)
        total += len(piece)
    return "".join(parts)[:chars]


def prompt_for(chars: int) -> str:
    half = chars // 2
    return (
        f"开头编号：{START}。\n"
        + filler(half, 0)
        + f"\n中间编号：{MIDDLE}。\n"
        + filler(chars - half, 500000)
        + f"\n结尾编号：{END}。\n"
        "问题：把上面出现的三个编号列出来，格式为「开头=…，中间=…，结尾=…」。"
        "哪一个没看到就写「看不到」。不要解释，不要复述填充内容。"
    )


def account():
    raw = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))["accounts"][0]
    return Credential(raw["uid"], user_nid=raw["user_nid"],
                      friend_nid=raw["friend_nid"],
                      app_code=raw.get("app_code") or raw["friend_nid"],
                      ai_poly=raw["ai_poly"])


def run(chars: int) -> dict:
    cfg = load_config(str(ROOT / "config.json"))
    cfg.retain_long_term_memory = False
    client = PolymasClient(cfg, account(), logger=lambda _m: None)
    started = time.time()
    reasoning = answer = error = ""
    try:
        for event, payload in client.chat(prompt_for(chars), model=MODEL,
                                          session_id=f"ndl-{uuid.uuid4().hex[:10]}"):
            if event == "REASONING":
                reasoning += payload.get("text") or ""
            elif event == "ANSWER":
                answer += payload.get("text") or ""
            elif event == "ERROR":
                error = str(payload)[:160]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
    blob = (answer + reasoning).upper()
    return {
        "chars": chars,
        "seconds": round(time.time() - started, 1),
        "error": error,
        "start": START.split("-")[1] in blob,
        "middle": MIDDLE.split("-")[1] in blob,
        "end": END.split("-")[1] in blob,
        "answer": answer.strip().replace("\n", " ")[:80],
    }


def main(argv: list[str]) -> int:
    sizes = [int(x) for x in argv[1:]] or [64000, 256000, 512000]
    print(f"模型 {MODEL}；三个随机编号分别放在开头/中间/结尾\n")
    print(f"{'输入字符':>10} {'耗时':>7}  {'开头':<5}{'中间':<5}{'结尾':<5} 回答")
    print("-" * 96)
    for size in sizes:
        r = run(size)
        if r["error"]:
            print(f"{size:>10} {r['seconds']:>6}s  {'失败':<15} {r['error']}")
            break
        mark = lambda ok: "看到 " if ok else "丢失 "
        print(f"{size:>10} {r['seconds']:>6}s  {mark(r['start'])}{mark(r['middle'])}{mark(r['end'])} {r['answer']}")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
