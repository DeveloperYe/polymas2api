"""Redact platform-vulnerability detail from the copy that gets published.

The working copy keeps the full record — the author is still investigating.
The public copy keeps only what a *user* needs: that the platform has a
credential problem, that this project does not proxy it, and that the gateway
can drive the upstream agent's shell so the key must be treated as a password.

Run by the build step; never copy this file into the published tree.

    python tools/redact_public.py <published-dir>
"""

from __future__ import annotations

import pathlib
import sys

NEW_SECTION_0 = """## 0. 平台侧存在一个与凭据有关的越权问题

调研过程中确认：平台侧存在一个**与令牌签发有关的越权问题**，任何知道某个用户
`userNid` 的人都有可能拿到该用户的凭据。**这是平台的问题，不是本项目引入的。**

> **细节不在此公开。** 端点、请求样例、验证过程一律不写 —— 这类内容公开没有任何好处，
> 只会让问题更容易被利用。作者会先与平台/学校沟通，之后再考虑是否公开细节。
> 如果你也在自行调研，请直接向平台报告；**不要把可复现步骤写进公开文档**。

本项目的对应责任：

1. **不在文档里写真实的 `userNid`**（对外发布的版本已全部脱敏）。面板与日志里会出现
   账号 uid（就是 `userNid`），面板需鉴权、日志只在本机 ——
   **分享日志或截图前请自行脱敏**，见 §3.6；
2. **不在反代里提供任何"代签"接口** —— 本项目不代理任何与令牌签发相关的平台接口，
   续期用的始终是**同一个用户自己的**凭据；
3. **不要把 `ai-poly` 外传**：它等同登录态。

---

"""

NEW_SECTION_02 = """### 0.2 这个反代能驱动上游执行命令 —— 所以 key 必须当密码看

上游 agent 自带一个「执行命令」工具。通过本反代的 `/v1/chat/completions` 就能触发它，
让它在上游 agent 的**执行环境**里跑 shell。这一点已实测确认。

> **环境探测结果不在此公开**（身份、内核、目录结构、能否出网都不写）。
> 那是对平台基础设施的侦察，公开没有好处，也不属于使用者需要知道的信息。

**这条风险的性质**：能挡住的只有 agent 自己的提示词约束 —— 那些限制是
**软的，不是硬的**，不要把它当隔离层。

**为什么严重**：一旦网关暴露在公网、而口令又能猜中，任何拿到口令的人就能借它执行命令。

**能做的缓解**（按有效性排序）：

1. **别把它暴露在公网** —— 绑 `127.0.0.1` 或上防火墙，这是唯一根治的做法；
2. 换成长随机口令，不要用短串；
3. 看审计 —— 每次调用用到哪些上游工具都记在请求日志的 `tools` 字段里
   （面板 →「近期活动」→「工具」列），出现「执行命令」就该警觉；
4. 不需要 agent 能力时把面板的「启用助手能力」关掉（`useAgent: 0`），
   知识库与课程操作会失效 —— 但实测**它自己的工具不一定会跟着关**，别当硬隔离。
"""


def redact_section(text: str, start_marker: str, end_marker: str, replacement: str) -> str:
    """Swap one section, or leave the text alone if it is already redacted.

    Idempotent on purpose: the published tree can be partially rebuilt by
    copying a single file out of the working copy, and that file arrives with
    the original text. Re-running must fix it without failing on the sections
    that were already handled.
    """
    try:
        start = text.index(start_marker)
    except ValueError:
        return text
    try:
        end = text.index(end_marker, start)
    except ValueError:
        return text
    return text[:start] + replacement + text[end:]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    root = pathlib.Path(argv[1])

    security = root / "docs" / "SECURITY.md"
    text = security.read_text(encoding="utf-8")
    text = redact_section(text, "## 0. 平台侧漏洞", "## 0.1 上游人格不可覆盖", NEW_SECTION_0)
    text = redact_section(text, "### 0.2 这个反代能驱动上游执行任意命令",
                          "**不建议**用「拦关键词」来防", NEW_SECTION_02)
    # 第 110 行那个「---该模块借鉴…」是历史遗留的分隔符粘连，顺手修掉
    text = text.replace("---该模块借鉴", "---\n\n该模块借鉴", 1)
    security.write_text(text, encoding="utf-8")

    protocol = root / "docs" / "PROTOCOL.md"
    text = protocol.read_text(encoding="utf-8")
    leaked = ("polymas-agent-assistant", "Alibaba Cloud Linux")
    if any(marker in text for marker in leaked):
        text = text.replace(
            "| `执行命令` 可执行、身份是 root | 仍 root；容器 "
            "`polymas-agent-assistant-77d56f545-6hl4k`<br>Alibaba Cloud Linux 5.10.134 | "
            "✅ 成立（容器名更新） |",
            "| `执行命令` 可执行 | 仍可执行（容器身份与宿主信息**不公开**） | ✅ 成立 |",
            1)
        text = text.replace("/app/working", "工作目录")
        protocol.write_text(text, encoding="utf-8")
        print("  PROTOCOL.md：容器身份/宿主信息已脱敏")

    print("  已脱敏 SECURITY.md §0 / §0.2 与 PROTOCOL.md 容器信息")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
