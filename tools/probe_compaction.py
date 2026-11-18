"""Does the model ask for context compaction on its own?

Two things are being separated:

* **Client-side compaction** (DSH's /compact, dsh-compaction-basic) is the client
  counting tokens and sending a summarisation request. That is not the model
  asking for anything.
* **Model-initiated compaction** would have to show up as either a tool call
  (the client offers a "please compact" tool) or an explicit statement in the
  answer ("上下文太长了，建议压缩").

This probe looks for the second one only. A compaction tool is offered; nothing
in the prompt tells the model to use it unless a scenario says so.

    python tools/probe_compaction.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymas2api.config import load_config          # noqa: E402

MODEL = "deepseek-v4.1-flash"
GW = "http://127.0.0.1:8787/v1/chat/completions"

COMPACT_TOOL = {
    "type": "function",
    "function": {
        "name": "request_compaction",
        "description": ("请求客户端压缩当前对话上下文。当对话历史过长、"
                        "接近你的上下文上限、继续下去可能超出时调用。"),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "为什么要压缩"},
                "keep": {"type": "string", "description": "压缩时必须保留的要点"},
            },
            "required": ["reason"],
        },
    },
}


def record_line(n: int) -> str:
    return (f"第{n}条工作记录：任务编号 {n * 7919 % 100003}，状态已完成，"
            f"负责人小组{n % 17}，备注为常规记录无异常。")


def history(chars: int, turns: int = 4) -> list[dict]:
    """A plausible long multi-turn conversation, not one giant blob."""
    per_turn = chars // (turns * 2)
    messages = []
    for turn in range(turns):
        body = "\n".join(record_line(turn * 1000 + i) for i in range(per_turn // 40))
        messages.append({"role": "user", "content": f"这是第 {turn + 1} 批工作记录：\n{body}\n先记下，稍后我要汇总。"})
        messages.append({"role": "assistant", "content": f"第 {turn + 1} 批已记下，共约 {len(body)} 字，需要汇总时告诉我。"})
    return messages


def call(messages: list[dict], tools=None, max_tokens_note: str = "") -> dict:
    body = {"model": MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    request = urllib.request.Request(
        GW, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {load_config(str(ROOT / 'config.json')).api_key}"},
        method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001
        return {"error": f"{type(error).__name__}: {str(error)[:160]}",
                "seconds": round(time.time() - started, 1)}
    message = (data.get("choices") or [{}])[0].get("message") or {}
    return {"seconds": round(time.time() - started, 1), "error": "",
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
            "finish": (data.get("choices") or [{}])[0].get("finish_reason")}


COMPACT_WORDS = ("压缩", "总结一下", "上下文太长", "上下文太长", "开个新会话",
                 "新建会话", "超出上限", "窗口快满", "compact")


def mentions_compaction(text: str) -> bool:
    return any(word in text for word in COMPACT_WORDS)


def report(label: str, out: dict, prompt_chars: int) -> None:
    if out["error"]:
        print(f"  {label}\n    失败: {out['error']}")
        return
    calls = ", ".join((c.get("function") or {}).get("name", "?") for c in out["tool_calls"]) or "无"
    text = out["content"].replace("\n", " ")
    print(f"  {label}")
    print(f"    输入 {prompt_chars:>9} 字符 · 耗时 {out['seconds']:>6}s · "
          f"tool_calls={calls} · 正文 {len(out['content'])} 字 · finish={out['finish']}")
    print(f"    正文里提到压缩/会话：{'是' if mentions_compaction(out['content']) else '否'}")
    if text:
        print(f"    摘要: {text[:110]}")
    print()


def main() -> int:
    print(f"模型 {MODEL}；压缩由客户端（如 DSH 的 /compact）触发，"
          f"这里只查模型自己会不会提\n")

    # 1. 长对话，一个问题，没有压缩工具，也不提压缩
    for chars in (20000, 300000):
        messages = history(chars) + [{"role": "user", "content": "把第 3 批里最后一条工作记录的编号告诉我。"}]
        total = sum(len(m["content"]) for m in messages)
        report(f"① {chars} 字符长对话 · 不带工具 · 不提压缩", call(messages), total)

    # 2. 同样的长对话，但提供压缩工具，提示里只说「需要时可以调用」
    for chars in (20000, 300000, 1200000):
        messages = history(chars)
        messages.append({"role": "user",
                         "content": "继续。后面还有很多批要处理，如果你觉得需要压缩上下文才可以继续，可以调用工具。"})
        total = sum(len(m["content"]) for m in messages)
        out = call(messages, tools=[COMPACT_TOOL])
        calls = [c for c in out.get("tool_calls") or []]
        report(f"② {chars} 字符 + 压缩工具 + 软提示", out, total)
        if calls:
            print(f"    ⚠️ 模型主动请求了压缩：{json.dumps(calls, ensure_ascii=False)[:200]}\n")

    # 3. 直接问它自己的认知（看它是否知道自己的窗口）
    messages = [{"role": "user",
                 "content": "你自己的上下文窗口有多大？超过多少会被截断或需要压缩？"
                            "只回答你知道的，不知道就说不知道，不要猜。"}]
    report("③ 直接问它自身上下文上限", call(messages), len(messages[0]["content"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
