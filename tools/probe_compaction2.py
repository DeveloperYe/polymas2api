"""Does the model notice real context pressure, or only conversational cues?

The first probe showed it calls a compaction tool at 11.8K characters - nowhere
near any limit - as soon as the prompt says "后面还有很多批要处理". That looks
like cue-following rather than measurement.

This one separates the two: a genuinely huge context (near the measured 1.5M
ceiling) with **no** hint that more work is coming, and no compaction tool at
all, so the only way it can raise the subject is its own judgement.

Talks to the platform directly: the gateway caps request bodies at 1 MB, so a
1.2M-character probe cannot go through it.

    python tools/probe_compaction2.py
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
COMPACT_WORDS = ("压缩", "上下文太长", "开个新会话", "新建会话", "超出上限",
                 "窗口快满", "快要满", "compact", "摘要历史")


def account():
    raw = json.loads((ROOT / "accounts.json").read_text(encoding="utf-8"))["accounts"][0]
    return Credential(raw["uid"], user_nid=raw["user_nid"], friend_nid=raw["friend_nid"],
                      app_code=raw.get("app_code") or raw["friend_nid"], ai_poly=raw["ai_poly"])


def record_line(n: int) -> str:
    return (f"第{n}条工作记录：任务编号 {n * 7919 % 100003}，状态已完成，"
            f"负责人小组{n % 17}，备注为常规记录无异常。")


def history_prompt(chars: int, turns: int = 4, closing: str = "") -> str:
    """One prompt that looks like accumulated history, ending with the real ask."""
    per_turn_chars = max(200, (chars - len(closing)) // turns)
    lines_per_turn = max(1, per_turn_chars // len(record_line(0)))
    parts = []
    for turn in range(turns):
        body = "\n".join(record_line(turn * 10000 + i) for i in range(lines_per_turn))
        parts.append(f"[第 {turn + 1} 批工作记录]\n{body}")
        parts.append(f"助手：第 {turn + 1} 批已记下。")
    parts.append("用户：" + closing)
    return "\n\n".join(parts)


def ask(prompt: str) -> dict:
    cfg = load_config(str(ROOT / "config.json"))
    cfg.retain_long_term_memory = False
    client = PolymasClient(cfg, account(), logger=lambda _m: None)
    started = time.time()
    reasoning = answer = error = ""
    try:
        for event, payload in client.chat(prompt, model=MODEL,
                                          session_id=f"cp2-{uuid.uuid4().hex[:10]}"):
            if event == "REASONING":
                reasoning += payload.get("text") or ""
            elif event == "ANSWER":
                answer += payload.get("text") or ""
            elif event == "ERROR":
                error = str(payload)[:160]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:160]}"
    blob = answer + reasoning
    return {"seconds": round(time.time() - started, 1), "error": error,
            "answer": answer.strip(), "chars": len(prompt),
            "mentions": [w for w in COMPACT_WORDS if w in blob]}


def show(label: str, out: dict) -> None:
    if out["error"]:
        print(f"  {label}\n    失败: {out['error']}\n")
        return
    print(f"  {label}")
    print(f"    输入 {out['chars']:>9} 字符 · 耗时 {out['seconds']:>6}s · 正文 {len(out['answer'])} 字")
    print(f"    提到压缩/新会话: {out['mentions'] or '否'}")
    print(f"    回答: {out['answer'][:120].replace(chr(10), ' ')}\n")


def main() -> int:
    print(f"模型 {MODEL}；不给「后面还有很多」这类暗示，看它自己会不会提上下文\n")

    ask_plain = "把第 2 批工作记录的最后一条编号告诉我。"
    show("① 20 万字符 · 普通提问（不给暗示）",
         ask(history_prompt(200000, closing=ask_plain)))
    show("② 120 万字符 · 普通提问（不给暗示）",
         ask(history_prompt(1200000, closing=ask_plain)))

    # 同样的巨大上下文，但这次把「是否该压缩」作为一个真实的决策问题抛给它
    show("③ 120 万字符 · 直接问它要不要压缩",
         ask(history_prompt(1200000,
                            closing="这批记录我还要继续增加很多轮。你觉得现在应该先压缩一下上下文吗？"
                                    "只回答 该 或 不该，再用一句话说明理由。")))

    # 对照：极小上下文，同样的「还会继续增加」暗示
    show("④ 2 千字符 · 同样暗示「还会继续增加」",
         ask(history_prompt(2000,
                            closing="这批记录我还要继续增加很多轮。你觉得现在应该先压缩一下上下文吗？"
                                    "只回答 该 或 不该，再用一句话说明理由。")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
