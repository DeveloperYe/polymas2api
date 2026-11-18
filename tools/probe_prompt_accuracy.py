"""How accurate is our token estimate on the prompts we actually send upstream?

`usage.prompt_tokens` is not decorative: DSH stores it as the anchor and uses it
as the base of every context-window percentage (dsh-token-meter stores
`event.data.usage` into the anchor, then prefers it over its own four-chars-per-
token heuristic when it is larger). A biased estimate therefore moves the
compaction threshold.

This builds a prompt through the same code path production uses
(`toolbridge.build_tool_prompt` + `OpenAIAdapter.extract_prompt`), then compares
`tokens.estimate_tokens` against DeepSeek's real BPE.

    python tools/probe_prompt_accuracy.py [tokenizer.json]
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from polymas2api.adapter import OpenAIAdapter                # noqa: E402
from polymas2api.tokens import estimate_tokens               # noqa: E402
from polymas2api.toolbridge import build_tool_prompt         # noqa: E402
from probe_tokenizer import Tokenizer                        # noqa: E402

# A dozen schemas shaped like the ones a coding agent declares: descriptions in
# Chinese, parameter names and enums in ASCII.
TOOLS = [
    ("bash", "在持久化 shell 里执行命令，返回 stdout 与 stderr。", {
        "command": {"type": "string", "description": "要执行的命令"},
        "timeout": {"type": "integer", "description": "超时毫秒数"}}),
    ("read", "读取文件内容，按行返回并带行号。", {
        "path": {"type": "string", "description": "文件绝对路径"},
        "limit": {"type": "integer", "description": "最多返回多少行"}}),
    ("write", "写入文件，会覆盖原有内容。", {
        "path": {"type": "string", "description": "目标路径"},
        "content": {"type": "string", "description": "完整文件内容"}}),
    ("edit", "按字面量替换文件里的一段文本。", {
        "path": {"type": "string"},
        "old_string": {"type": "string"},
        "new_string": {"type": "string"}}),
    ("glob", "按路径模式查找文件。", {
        "pattern": {"type": "string", "description": "例如 **/*.ts"},
        "path": {"type": "string"}}),
    ("grep", "用正则搜索文件内容。", {
        "pattern": {"type": "string"},
        "include": {"type": "string"},
        "path": {"type": "string"}}),
    ("web_search", "联网搜索，返回标题与摘要。", {
        "query": {"type": "string", "description": "查询词，可多条"}}),
    ("web_fetch", "抓取一个网页并转成文本。", {
        "url": {"type": "string"}}),
    ("subagent", "把独立任务交给子代理执行。", {
        "description": {"type": "string"},
        "prompt": {"type": "string"}}),
    ("todo_write", "记录并更新任务清单。", {
        "todos": {"type": "array", "items": {"type": "object"}}}),
    ("browser_act", "在页面上执行点击、填表等动作。", {
        "snapshot": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "object"}}}),
    ("send_message", "给团队成员发送一条消息。", {
        "target": {"type": "string"},
        "message": {"type": "string"}}),
]


def tools_payload() -> list[dict]:
    return [{"type": "function",
             "function": {"name": name, "description": desc,
                          "parameters": {"type": "object", "properties": props,
                                         "required": list(props)}}}
            for name, desc, props in TOOLS]


CONVERSATION = [
    ("user", "帮我看看 polymas2api 里那个请求体上限是多大，我记得好像是一兆？"),
    ("assistant", "现在是 8 MB。`MAX_BODY_BYTES = 8 << 20`，定义在 `server.py` 第 65 行。"
                  "之前是 1 MiB，因为发现它会卡死客户端的自动压缩才抬上去的。"),
    ("user", "为什么会卡死压缩？"),
    ("assistant", "因为两把尺子不一样。DSH 的 token 计数器按固定 4 字符/token 估算，"
                  "中文实际约 1.6 字符/token，于是它把 1M 窗口的 80% 算成 320 万字符，"
                  "而平台在 100 万 token（中文约 160 万字符）就报错了。"),
    ("user", "那现在呢？"),
    ("assistant", "现在网关把 `usage.prompt_tokens` 报给 DSH，DSH 在它大于自身估算时"
                  "会优先采用，所以压缩基线就是我们的数字。阈值 800,000，"
                  "大约在真实窗口 72%~96% 处触发。"),
    ("user", "估算准吗？"),
    ("assistant", "混排内容实测偏差在 −7% 到 +21% 之间，中文单语种会高估，"
                  "纯代码基本准确。具体数字见 docs/LIMITS.md §8。"),
]


def conversation_messages(turns: int = 8) -> list[dict]:
    out = []
    for i in range(turns):
        role, text = CONVERSATION[i % len(CONVERSATION)]
        out.append({"role": role, "content": text})
    return out


def measure(label: str, prompt: str) -> tuple[int, int]:
    real = TOKENIZER.count(prompt)
    ours = estimate_tokens(prompt)
    bias = (ours / real - 1) * 100 if real else 0.0
    print(f"  {label:<26}{len(prompt):>9,}{real:>12,}{ours:>12,}{bias:>9.1f}%")
    return real, ours


def main(argv: list[str]) -> int:
    global TOKENIZER
    path = pathlib.Path(argv[1]) if len(argv) > 1 else \
        pathlib.Path.home() / "AppData/Local/Temp/tokprobe/tokenizer.json"
    if not path.exists():
        print(f"找不到 tokenizer.json: {path}")
        return 2
    TOKENIZER = Tokenizer(path)

    tools = tools_payload()
    extra, _ = build_tool_prompt(tools, None, "verbatim", 2)

    print(f"  工具 schema {len(tools)} 个，渲染成 {len(extra):,} 字符\n")
    print(f"  {'提示词构成':<26}{'字符':>9}{'真实 BPE':>12}{'我们估算':>12}{'偏差':>10}")
    print("  " + "-" * 70)

    tool_only = extra
    conv_only = OpenAIAdapter.extract_prompt(conversation_messages(), extra_system="")
    full = OpenAIAdapter.extract_prompt(conversation_messages(), extra_system=extra)

    measure("仅工具说明（ASCII 为主）", tool_only)
    measure("仅对话（中文为主）", conv_only)
    measure("完整提示词（生产形态）", full)
    print()

    tool_chars = len(extra)
    print(f"  构成：工具说明 {tool_chars / len(full) * 100:.0f}%，对话 {100 - tool_chars / len(full) * 100:.0f}%")
    print()

    # 更长的对话，看偏差是否随长度漂移
    print("  对话拉长后的偏差：")
    print(f"  {'轮数':<26}{'字符':>9}{'真实 BPE':>12}{'我们估算':>12}{'偏差':>10}")
    print("  " + "-" * 70)
    for turns in (8, 24, 60, 120):
        prompt = OpenAIAdapter.extract_prompt(conversation_messages(turns), extra_system=extra)
        measure(f"{turns} 轮（约 {turns * 2} 条消息）", prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
